"""Minimal live interview coach API."""
from __future__ import annotations

import base64
import asyncio
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import uuid
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from session_store import SessionStore

try:
    from langchain_core.prompts import ChatPromptTemplate
except ImportError:  # pragma: no cover - requirements.txt installs it
    ChatPromptTemplate = None

app = FastAPI(title="Live Interview Coach", version="0.4.0")
app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parent / "static"), name="static")

DEFAULT_OLLAMA_MODEL = "qwen2.5:1.5b-instruct"
DEFAULT_BACKEND = "ollama"
MAX_PROMPT_HISTORY = 3600
DEFAULT_LANGUAGE = "pt-PT"

# Modelos disponíveis para o seletor da UI (nome amigável -> tag Ollama).
AVAILABLE_MODELS = [
    {"id": "qwen2.5:0.5b-instruct", "label": "Qwen2.5 0.5B · mais rápido"},
    {"id": "qwen2.5:1.5b-instruct", "label": "Qwen2.5 1.5B · equilíbrio (default)"},
    {"id": "qwen2.5:7b", "label": "Qwen2.5 7B · mais qualidade"},
]

LANGUAGE_LABELS = {"pt-PT": "português de Portugal", "en": "English"}

# Vozes Piper por idioma. Correr scripts/download_piper_voice.ps1 (pt) e o equivalente
# para inglês antes de usar; ver README para instruções de download.
PIPER_VOICES = {
    "pt-PT": "pt_PT-tugao-medium.onnx",
    "en": "en_US-lessac-medium.onnx",
}

# Threshold de confiança STT (Whisper no_speech_prob): acima disto, o agente pede
# clarificação em vez de avançar com uma transcrição pouco fiável.
STT_LOW_CONFIDENCE_THRESHOLD = float(os.getenv("STT_LOW_CONFIDENCE_THRESHOLD", "0.55"))

# Runtime override para o modelo Ollama, definido via POST /api/model (seletor na UI).
# Tem prioridade sobre a variável de ambiente OLLAMA_MODEL quando definido.
RUNTIME_MODEL_OVERRIDE: str | None = None


@dataclass
class Session:
    persona: str
    instructions: str
    avoid: str
    candidate_context: str
    questions: list[str]
    current_index: int = 0
    history: list[dict[str, str]] = field(default_factory=list)
    pending_question: str | None = None
    language: str = DEFAULT_LANGUAGE
    must: list[str] = field(default_factory=list)
    dont: list[str] = field(default_factory=list)
    questions_to_avoid: list[str] = field(default_factory=list)
    script_fidelity: float = 0.5
    duration_minutes: int = 30
    min_pause_seconds: float = 0.8
    started_at: float = field(default_factory=time.time)


class StartRequest(BaseModel):
    persona: str = Field(min_length=1)
    instructions: str = "Be concise and curious."
    avoid: str = "Do not invent facts or make promises."
    candidate_context: str = ""
    questions: list[str] = Field(default_factory=lambda: ["Fala-me sobre ti e sobre o teu percurso profissional."])
    language: str = DEFAULT_LANGUAGE
    must: list[str] = Field(default_factory=list)
    dont: list[str] = Field(default_factory=list)
    questions_to_avoid: list[str] = Field(default_factory=list)
    script_fidelity: float = Field(default=0.5, ge=0.0, le=1.0)
    duration_minutes: int = Field(default=30, gt=0, le=180)
    min_pause_seconds: float = Field(default=0.8, ge=0.2, le=5.0)


class AskRequest(BaseModel):
    session_id: str
    text_answer: str | None = None
    audio_base64: str | None = None
    audio_format: str = "wav"


class TranscribeRequest(BaseModel):
    audio_base64: str = Field(min_length=1)
    audio_format: str = "webm"


class StopRequest(BaseModel):
    session_id: str


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1)
    language: str = DEFAULT_LANGUAGE


class ModelSelectRequest(BaseModel):
    model: str = Field(min_length=1)


PROMPT = """SYSTEM:
És um entrevistador e coach experiente: caloroso, perspicaz, natural e conciso. A conversa é em {language_label}. Analisa a resposta do candidato no contexto da conversa. Se estiver vaga ou incompleta, faz uma pergunta de follow-up concreta; só avança para a próxima pergunta planeada quando a resposta estiver suficientemente coberta. {fidelity_instruction} Todas as frases em feedback, followups, next_question e notes devem ser em {language_label}. Devolve apenas JSON válido, sem markdown nem texto adicional.

USER:
Persona: {persona}
Instruções: {instructions}
Deve (must): {must_list}
Não deve (dont): {dont_list}
Evitar: {avoid}
Perguntas a evitar: {questions_to_avoid_list}
Informação adicional sobre o candidato: {candidate_context}
{time_hint}
Conversa recente:
{history}
Pergunta atual: {current_question}
Resposta do candidato: {candidate_text}
Próxima pergunta planeada (usa-a apenas se não for necessário follow-up): {next_question_hint}

Produz JSON exatamente com estas chaves. Mantém feedback com no máximo dois itens curtos, followups com no máximo uma pergunta e notes numa frase curta:
{{"feedback": ["ponto 1", "ponto 2"], "followups": ["pergunta"], "next_question": "follow-up ou próxima pergunta planeada", "score": 0, "notes": "justificação numa frase"}}
"""


def project_path(filename: str) -> Path:
    return Path(__file__).resolve().parent / filename


session_store = SessionStore(project_path("sessions.db"))
WHISPER_MODEL_CACHE: dict[str, Any] = {}
WHISPER_MODEL_LOCK = threading.Lock()
PIPER_VOICE_CACHE: dict[str, Any] = {}
PIPER_VOICE_LOCK = threading.Lock()
FFMPEG_DIR = Path(r"C:\tools")
if (FFMPEG_DIR / "ffmpeg.exe").exists():
    os.environ["PATH"] = f"{FFMPEG_DIR}{os.pathsep}{os.environ.get('PATH', '')}"


def fidelity_instruction(score: float) -> str:
    if score >= 0.75:
        return "Mantém-te muito fiel ao roteiro: segue as perguntas planeadas de perto e evita desviares-te para tangentes."
    if score <= 0.25:
        return "Sente-te livre para divergires do roteiro: explora tangentes relevantes e adapta a ordem das perguntas ao que fizer sentido na conversa."
    return "Podes divergir moderadamente do roteiro quando a conversa o justificar, mas regressa às perguntas planeadas em pouco tempo."


def time_hint_for(session: Session) -> str:
    elapsed_minutes = (time.time() - session.started_at) / 60
    remaining = session.duration_minutes - elapsed_minutes
    if remaining <= 5:
        return f"A entrevista está perto do tempo limite (~{max(0, round(remaining))} min restantes); começa a fechar tópicos em aberto e conduz para o encerramento."
    return ""


def build_prompt(session: Session, candidate_text: str) -> str:
    question = session.pending_question or session.questions[session.current_index]
    next_index = min(session.current_index + 1, len(session.questions) - 1)
    next_question = session.questions[next_index] if session.questions and next_index < len(session.questions) else "Que perguntas tens para mim?"
    history = "\n".join(
        f"Turn {index}: Question: {turn.get('question', '')}\nAnswer: {turn.get('answer', '')}"
        for index, turn in enumerate(session.history[-6:], start=max(1, len(session.history) - 5))
    ) or "No previous answers; this is the first turn."
    language_label = LANGUAGE_LABELS.get(session.language, LANGUAGE_LABELS[DEFAULT_LANGUAGE])
    return PROMPT.format(
        language_label=language_label,
        fidelity_instruction=fidelity_instruction(session.script_fidelity),
        persona=session.persona,
        instructions=session.instructions,
        must_list="; ".join(session.must) or "(nenhum)",
        dont_list="; ".join(session.dont) or "(nenhum)",
        avoid=session.avoid,
        questions_to_avoid_list="; ".join(session.questions_to_avoid) or "(nenhuma)",
        candidate_context=session.candidate_context[:1600],
        time_hint=time_hint_for(session),
        history=history[:MAX_PROMPT_HISTORY],
        current_question=question,
        candidate_text=candidate_text[:2200],
        next_question_hint=next_question,
    )


def fake_coach(session: Session, candidate_text: str) -> dict[str, Any]:
    next_index = session.current_index + 1
    next_question = session.questions[next_index] if next_index < len(session.questions) else "Obrigado. Que perguntas tens para mim?"
    return {
        "feedback": ["Resposta clara e relevante.", "Acrescenta um resultado mensurável ou um trade-off técnico."],
        "followups": ["Que decisão técnica teve maior impacto no resultado?"] if candidate_text else [],
        "next_question": next_question,
        "score": 3,
        "notes": "A resposta cobre o tema, mas pode ganhar profundidade com evidência concreta.",
    }


def call_openai_compatible(prompt: str) -> str:
    chat_url = os.getenv("LLAMA_SERVER_URL", "http://127.0.0.1:8080/v1/chat/completions")
    client = requests.Session()
    client.trust_env = False
    response = client.post(
        chat_url,
        json={
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": int(os.getenv("LLM_MAX_TOKENS", "320")),
        },
        timeout=180,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("choices"):
        return body["choices"][0]["message"]["content"]
    return body["content"]


def current_ollama_model() -> str:
    return RUNTIME_MODEL_OVERRIDE or os.getenv("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)


def call_ollama(prompt: str) -> dict[str, Any]:
    if os.getenv("LIVE_COACH_FAKE_LLM") == "1":
        raise RuntimeError("fake mode is handled by the endpoint")
    model = current_ollama_model()
    backend = os.getenv("LLM_BACKEND", DEFAULT_BACKEND).lower()
    if backend in {"llama", "llama-server", "llamacpp"}:
        return parse_json(call_openai_compatible(prompt))

    if backend == "auto":
        try:
            return parse_json(call_openai_compatible(prompt))
        except (requests.RequestException, ValueError, KeyError, TypeError):
            pass

    try:
        response = requests.post(
            os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate"),
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "options": {
                    "temperature": 0.2,
                    "num_ctx": int(os.getenv("LLM_CONTEXT", "4096")),
                    "num_predict": int(os.getenv("LLM_MAX_TOKENS", "320")),
                },
            },
            timeout=180,
        )
        response.raise_for_status()
        raw = response.json().get("response", "")
    except (requests.RequestException, ValueError):
        configured_ollama = os.getenv("OLLAMA_EXE")
        default_ollama = Path(r"C:\Users\iliei\AppData\Local\Programs\Ollama\ollama.exe")
        ollama_executable = configured_ollama or (str(default_ollama) if default_ollama.exists() else "ollama")
        completed = subprocess.run(
            [ollama_executable, "run", model, prompt],
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )
        raw = completed.stdout
    return parse_json(raw)


def parse_json(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return {
                "feedback": ["O modelo respondeu sem estrutura JSON; revê a resposta e acrescenta um exemplo concreto."],
                "followups": [],
                "next_question": "",
                "score": 0,
                "notes": raw[:280] or "Resposta do modelo vazia.",
            }
        try:
            result = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return {
                "feedback": ["A resposta precisa de mais estrutura e clareza."],
                "followups": [],
                "next_question": "",
                "score": 0,
                "notes": raw[:280],
            }
    if not isinstance(result, dict):
        raise ValueError("Resposta do modelo deve ser um objeto JSON")
    result.setdefault("feedback", [])
    result.setdefault("followups", [])
    result.setdefault("next_question", "")
    result.setdefault("score", 0)
    result.setdefault("notes", "")
    result["feedback"] = [str(item).strip() for item in result["feedback"] if str(item).strip()][:2] if isinstance(result["feedback"], list) else [str(result["feedback"]).strip()]
    result["followups"] = [str(item).strip() for item in result["followups"] if str(item).strip()][:1] if isinstance(result["followups"], list) else [str(result["followups"]).strip()]
    try:
        result["score"] = max(0, min(5, int(float(result["score"]))))
    except (TypeError, ValueError):
        result["score"] = 0
    result["next_question"] = str(result["next_question"] or "").strip()
    result["notes"] = str(result["notes"] or "").strip()
    return result


def usable_question(value: Any) -> str:
    question = str(value or "").strip()
    if len(question) < 12 or question.casefold() in {"pergunta", "question", "follow-up", "followup"}:
        return ""
    return question


def get_whisper_model() -> Any:
    import whisper

    model_name = os.getenv("WHISPER_MODEL", "tiny")
    with WHISPER_MODEL_LOCK:
        model = WHISPER_MODEL_CACHE.get(model_name)
        if model is None:
            model = whisper.load_model(model_name)
            WHISPER_MODEL_CACHE[model_name] = model
    return model


def piper_voice_path(language: str) -> Path:
    voice_file = PIPER_VOICES.get(language, PIPER_VOICES[DEFAULT_LANGUAGE])
    return project_path("models/piper") / voice_file


def get_piper_voice(language: str) -> Any:
    try:
        from piper.voice import PiperVoice
    except ImportError as exc:  # pragma: no cover - requirements.txt installs it
        raise ImportError("piper-tts não está instalado. Corre: pip install piper-tts") from exc
    model_path = piper_voice_path(language)
    if not model_path.exists():
        raise FileNotFoundError(
            f"Voz Piper em falta para '{language}': {model_path}. "
            "Corre scripts/download_piper_voice.ps1 (ou o script equivalente para inglês)."
        )
    with PIPER_VOICE_LOCK:
        voice = PIPER_VOICE_CACHE.get(str(model_path))
        if voice is None:
            voice = PiperVoice.load(str(model_path))
            PIPER_VOICE_CACHE[str(model_path)] = voice
    return voice


async def warm_whisper_model() -> None:
    if os.getenv("WARMUP_WHISPER", "1") == "1":
        try:
            await asyncio.to_thread(get_whisper_model)
        except Exception:
            pass


async def warm_piper_voice() -> None:
    if os.getenv("WARMUP_PIPER", "1") == "1":
        try:
            await asyncio.to_thread(get_piper_voice, DEFAULT_LANGUAGE)
        except Exception:
            pass


@app.on_event("startup")
async def preload_local_voice() -> None:
    asyncio.create_task(warm_whisper_model())
    asyncio.create_task(warm_piper_voice())


def transcribe_audio(encoded: str, audio_format: str) -> tuple[str, float | None]:
    """Devolve (texto, confiança). Confiança é None quando não é possível calcular
    (por exemplo, Whisper indisponível), e um valor 0-1 (maior é melhor) caso contrário."""
    try:
        import whisper
    except ImportError as exc:
        raise HTTPException(400, "Whisper não está instalado; envia text_answer ou instala openai-whisper.") from exc
    try:
        audio_bytes = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise HTTPException(400, "audio_base64 inválido") from exc
    safe_format = "".join(character for character in audio_format.lower() if character.isalnum()) or "wav"
    suffix = "." + safe_format
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as audio_file:
        audio_file.write(audio_bytes)
        audio_path = Path(audio_file.name)
    try:
        model = get_whisper_model()
        result = model.transcribe(
            str(audio_path),
            language=os.getenv("WHISPER_LANGUAGE", "pt"),
            fp16=False,
            temperature=0,
            condition_on_previous_text=False,
        )
        text = str(result.get("text", "")).strip()
        segments = result.get("segments") or []
        if segments:
            no_speech_probs = [float(segment.get("no_speech_prob", 0.0)) for segment in segments]
            confidence = 1.0 - (sum(no_speech_probs) / len(no_speech_probs))
        else:
            confidence = None
        return text, confidence
    finally:
        audio_path.unlink(missing_ok=True)


def backend_status() -> dict[str, Any]:
    configured_llama_url = os.getenv("LLAMA_SERVER_URL", "http://127.0.0.1:8080/v1/chat/completions")
    llama_url = configured_llama_url.split("/v1/", 1)[0] + "/health"
    ollama_url = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/tags")
    backend = os.getenv("LLM_BACKEND", DEFAULT_BACKEND).lower()
    model = current_ollama_model()
    if os.getenv("LIVE_COACH_FAKE_LLM") == "1":
        return {"backend": "demo", "available": True, "label": "Demo mode"}
    if backend in {"auto", "llama", "llama-server", "llamacpp"}:
        try:
            requests.get(llama_url, timeout=1).raise_for_status()
            return {"backend": "llama-server", "available": True, "model": model, "label": "llama.cpp local"}
        except requests.RequestException:
            if backend != "auto":
                return {"backend": "offline", "available": False, "model": model, "label": "llama.cpp indisponível"}
    try:
        requests.get(ollama_url, timeout=1).raise_for_status()
        return {"backend": "ollama", "available": True, "model": model, "label": f"Ollama · {model}"}
    except requests.RequestException:
        return {"backend": "offline", "available": False, "model": model, "label": "Nenhum modelo ligado"}


@app.get("/api/status")
def status() -> dict[str, Any]:
    return backend_status()


@app.get("/api/models")
def list_models() -> dict[str, Any]:
    return {"models": AVAILABLE_MODELS, "current": current_ollama_model()}


@app.post("/api/model")
def select_model(payload: ModelSelectRequest) -> dict[str, Any]:
    global RUNTIME_MODEL_OVERRIDE
    valid_ids = {entry["id"] for entry in AVAILABLE_MODELS}
    if payload.model not in valid_ids:
        raise HTTPException(422, f"Modelo desconhecido. Opções: {', '.join(sorted(valid_ids))}")
    RUNTIME_MODEL_OVERRIDE = payload.model
    return {"current": RUNTIME_MODEL_OVERRIDE}


@app.post("/transcribe")
def transcribe(payload: TranscribeRequest) -> dict[str, Any]:
    text, confidence = transcribe_audio(payload.audio_base64, payload.audio_format)
    return {
        "text": text,
        "model": os.getenv("WHISPER_MODEL", "tiny"),
        "confidence": confidence,
        "low_confidence": confidence is not None and confidence < STT_LOW_CONFIDENCE_THRESHOLD,
    }


@app.post("/speak")
def speak(payload: SpeakRequest) -> Response:
    text = payload.text.strip()
    if not text:
        raise HTTPException(400, "text vazio")
    started = time.perf_counter()
    try:
        voice = get_piper_voice(payload.language)
    except FileNotFoundError as exc:
        raise HTTPException(503, str(exc)) from exc
    except ImportError as exc:
        raise HTTPException(503, str(exc)) from exc
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        voice.synthesize(text, wav_file)
    buffer.seek(0)
    latency_ms = (time.perf_counter() - started) * 1000
    return Response(content=buffer.read(), media_type="audio/wav", headers={"X-TTS-Latency-Ms": f"{latency_ms:.0f}"})


@app.get("/health")
def health() -> dict[str, Any]:
    model = backend_status()
    piper_ready = any(Path(path).exists() for path in [str(piper_voice_path(DEFAULT_LANGUAGE))])
    return {
        "ok": True,
        "model": model,
        "configured_backend": os.getenv("LLM_BACKEND", DEFAULT_BACKEND),
        "voice": {
            "backend": "whisper-local",
            "model": os.getenv("WHISPER_MODEL", "tiny"),
            "ready": os.getenv("WHISPER_MODEL", "tiny") in WHISPER_MODEL_CACHE,
        },
        "tts": {
            "backend": "piper",
            "voice_file_present": piper_ready,
            "ready": str(piper_voice_path(DEFAULT_LANGUAGE)) in PIPER_VOICE_CACHE,
        },
        "active_sessions": session_store.count(),
    }


@app.get("/")
def home() -> FileResponse:
    return FileResponse(project_path("static/index.html"))


@app.post("/start")
def start_session(payload: StartRequest) -> dict[str, Any]:
    if not payload.questions:
        raise HTTPException(422, "A sessão precisa de pelo menos uma pergunta")
    if payload.language not in LANGUAGE_LABELS:
        raise HTTPException(422, f"Idioma desconhecido. Opções: {', '.join(LANGUAGE_LABELS)}")
    session_id = str(uuid.uuid4())
    session = Session(
        persona=payload.persona,
        instructions=payload.instructions,
        avoid=payload.avoid,
        candidate_context=payload.candidate_context,
        questions=payload.questions,
        language=payload.language,
        must=payload.must,
        dont=payload.dont,
        questions_to_avoid=payload.questions_to_avoid,
        script_fidelity=payload.script_fidelity,
        duration_minutes=payload.duration_minutes,
        min_pause_seconds=payload.min_pause_seconds,
        started_at=time.time(),
    )
    session_store.save(session_id, session)
    return {
        "session_id": session_id,
        "current_question": payload.questions[0],
        "language": session.language,
        "duration_minutes": session.duration_minutes,
        "min_pause_seconds": session.min_pause_seconds,
    }


@app.post("/ask")
def ask(payload: AskRequest) -> dict[str, Any]:
    session = session_store.get(payload.session_id, Session)
    if session is None:
        raise HTTPException(404, "session_id desconhecido")
    if not payload.text_answer and not payload.audio_base64:
        raise HTTPException(400, "Envia text_answer ou audio_base64")

    stt_confidence: float | None = None
    if payload.text_answer:
        candidate_text = payload.text_answer
    else:
        stt_started = time.perf_counter()
        candidate_text, stt_confidence = transcribe_audio(payload.audio_base64 or "", payload.audio_format)
        session_store.log_metric(payload.session_id, "stt", (time.perf_counter() - stt_started) * 1000, {"confidence": stt_confidence})

    current_question = session.pending_question or session.questions[session.current_index]

    # Confiança STT baixa: pede clarificação em vez de avançar com uma transcrição pouco fiável.
    if stt_confidence is not None and stt_confidence < STT_LOW_CONFIDENCE_THRESHOLD:
        return {
            "candidate_text": candidate_text,
            "feedback": [],
            "followups": [],
            "next_question": "Desculpa, não tenho a certeza que percebi bem. Podes repetir a última parte?",
            "score": 0,
            "notes": "Confiança de reconhecimento de voz abaixo do limiar configurado.",
            "is_followup": True,
            "stt_confidence": stt_confidence,
        }

    llm_started = time.perf_counter()
    try:
        result = fake_coach(session, candidate_text) if os.getenv("LIVE_COACH_FAKE_LLM") == "1" else call_ollama(build_prompt(session, candidate_text))
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise HTTPException(503, f"LLM indisponível: {exc}") from exc
    session_store.log_metric(payload.session_id, "llm", (time.perf_counter() - llm_started) * 1000, {"model": current_ollama_model()})

    followup = usable_question(result.get("followups", [""])[0] if result.get("followups") else "")
    next_planned_index = min(session.current_index + 1, len(session.questions) - 1)
    next_planned = session.questions[next_planned_index] if session.questions and session.current_index + 1 < len(session.questions) else "Obrigado. Que perguntas tens para mim?"
    if followup:
        result["next_question"] = followup
        session.pending_question = followup
        result["is_followup"] = True
    else:
        session.pending_question = None
        session.current_index = min(session.current_index + 1, len(session.questions) - 1)
        result["next_question"] = usable_question(result.get("next_question")) or next_planned
        result["is_followup"] = False
    session.history.append({"question": current_question, "answer": candidate_text})
    session_store.save(payload.session_id, session)
    if stt_confidence is not None:
        result["stt_confidence"] = stt_confidence
    return {"candidate_text": candidate_text, **result}


@app.post("/stop")
def stop_session(payload: StopRequest) -> dict[str, Any]:
    session = session_store.get(payload.session_id, Session)
    if session is None:
        raise HTTPException(404, "session_id desconhecido")
    session_store.delete(payload.session_id)
    return {"summary": f"Sessão terminada após {len(session.history)} resposta(s).", "history": session.history}


@app.get("/api/session/{session_id}/metrics")
def session_metrics(session_id: str) -> dict[str, Any]:
    return {"session_id": session_id, "metrics": session_store.get_metrics(session_id)}


@app.websocket("/ws/{session_id}")
async def interview_socket(websocket: WebSocket, session_id: str) -> None:
    await websocket.accept()
    try:
        while True:
            message = await websocket.receive_json()
            result = await asyncio.to_thread(ask, AskRequest(session_id=session_id, text_answer=message.get("text_answer")))
            await websocket.send_json(result)
    except WebSocketDisconnect:
        return
