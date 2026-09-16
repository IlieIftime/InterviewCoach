"""Minimal live interview coach API."""
from __future__ import annotations

import base64
import asyncio
import json
import os
import subprocess
import tempfile
import threading
import time
import uuid
import wave
from io import BytesIO
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

app = FastAPI(title="Live Interview Coach", version="0.3.0")
app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parent / "static"), name="static")

DEFAULT_OLLAMA_MODEL = "qwen2.5:1.5b-instruct"
DEFAULT_BACKEND = "ollama"
MAX_PROMPT_HISTORY = 3600
DEFAULT_OLLAMA_GENERATE_URL = "http://127.0.0.1:11434/api/generate"
PIPER_VOICE_DIR = Path(__file__).resolve().parent / "models" / "piper"
DEFAULT_PIPER_VOICE = PIPER_VOICE_DIR / "pt_PT-tugao-medium.onnx"
DEFAULT_LANGUAGE = "pt-PT"
DEFAULT_MIN_PAUSE_SECONDS = 1.5
DEFAULT_DURATION_MINUTES = 20
DEFAULT_MODEL_OPTIONS = ["qwen2.5:0.5b-instruct", "qwen2.5:1.5b-instruct", "qwen2.5:7b"]


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
    must: str = ""
    dont: str = ""
    questions_to_ask: list[str] = field(default_factory=list)
    questions_to_avoid: list[str] = field(default_factory=list)
    script_fidelity: str = "adaptive"
    duration_minutes: int = DEFAULT_DURATION_MINUTES
    min_pause_seconds: float = DEFAULT_MIN_PAUSE_SECONDS
    model: str = ""
    started_at: float = field(default_factory=time.time)


class StartRequest(BaseModel):
    persona: str = Field(min_length=1)
    instructions: str = "Be concise and curious."
    avoid: str = "Do not invent facts or make promises."
    candidate_context: str = ""
    questions: list[str] = Field(default_factory=lambda: ["Fala-me sobre ti e sobre o teu percurso profissional."])
    language: str = Field(default=DEFAULT_LANGUAGE, min_length=2, max_length=12)
    must: str = ""
    dont: str = ""
    questions_to_ask: list[str] = Field(default_factory=list)
    questions_to_avoid: list[str] = Field(default_factory=list)
    script_fidelity: str = Field(default="adaptive", pattern="^(strict|adaptive|free)$")
    duration_minutes: int = Field(default=DEFAULT_DURATION_MINUTES, ge=5, le=120)
    min_pause_seconds: float = Field(default=DEFAULT_MIN_PAUSE_SECONDS, ge=0.8, le=4.0)
    model: str = Field(default="", max_length=80)


class AskRequest(BaseModel):
    session_id: str
    text_answer: str | None = None
    audio_base64: str | None = None
    audio_format: str = "wav"
    stt_confidence: float | None = Field(default=None, ge=0, le=1)
    capture_mode: str = "text"
    auto_submitted: bool = False


class TranscribeRequest(BaseModel):
    audio_base64: str = Field(min_length=1)
    audio_format: str = "webm"
    language: str = DEFAULT_LANGUAGE


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1, max_length=1600)
    language: str = DEFAULT_LANGUAGE
    session_id: str | None = None


class StopRequest(BaseModel):
    session_id: str


PROMPT = """SYSTEM:
És um entrevistador e coach experiente: caloroso, perspicaz, natural e conciso. A conversa é em português de Portugal. Analisa a resposta do candidato no contexto da conversa. Se estiver vaga ou incompleta, faz uma pergunta de follow-up concreta; só avança para a próxima pergunta planeada quando a resposta estiver suficientemente coberta. Todas as frases em feedback, followups, next_question e notes devem ser em português de Portugal. Devolve apenas JSON válido, sem markdown nem texto adicional.

USER:
Persona: {persona}
Instruções: {instructions}
Evitar: {avoid}
Informação adicional sobre o candidato: {candidate_context}
Conversa recente:
{history}
Pergunta atual: {current_question}
Resposta do candidato: {candidate_text}
Próxima pergunta planeada (usa-a apenas se não for necessário follow-up): {next_question_hint}

Produz JSON exatamente com estas chaves. Mantém feedback com no máximo dois itens curtos, followups com no máximo uma pergunta e notes numa frase curta:
{{"feedback": ["ponto 1", "ponto 2"], "followups": ["pergunta"], "next_question": "follow-up ou próxima pergunta planeada", "score": 0, "notes": "justificação numa frase"}}
"""


PROMPT = """SYSTEM:
És um entrevistador de Portugal: caloroso, atento e conciso. Analisa a resposta no contexto da conversa. Se faltar detalhe importante, faz apenas um follow-up concreto; caso contrário avança para a próxima pergunta. Responde sempre em português de Portugal. Sê extremamente conciso: feedback com uma frase curta, follow-up com uma pergunta curta e notes com uma frase curta. Devolve apenas JSON válido.

DADOS:
Persona: {persona}
Instruções: {instructions}
Evitar: {avoid}
Contexto do candidato: {candidate_context}
Histórico recente:
{history}
Pergunta atual: {current_question}
Resposta: {candidate_text}
Próxima pergunta planeada: {next_question_hint}

Campos JSON obrigatórios: feedback (0-2 observações concretas sobre a resposta), followups (0-1 pergunta concreta ou lista vazia), next_question (a próxima pergunta real), score (0-5), notes (uma observação concreta). Não uses texto de exemplo nem os nomes dos campos como conteúdo.
"""


def project_path(filename: str) -> Path:
    return Path(__file__).resolve().parent / filename


session_store = SessionStore(project_path("sessions.db"))
WHISPER_MODEL_CACHE: dict[str, Any] = {}
WHISPER_MODEL_LOCK = threading.Lock()
PIPER_MODEL_CACHE: dict[str, Any] = {}
PIPER_MODEL_LOCK = threading.Lock()
FFMPEG_DIR = Path(r"C:\tools")
if (FFMPEG_DIR / "ffmpeg.exe").exists():
    os.environ["PATH"] = f"{FFMPEG_DIR}{os.pathsep}{os.environ.get('PATH', '')}"


def build_prompt(session: Session, candidate_text: str) -> str:
    question = session.pending_question or session.questions[session.current_index]
    next_index = session.current_index + 1
    next_question = session.questions[next_index] if next_index < len(session.questions) else "Que perguntas tens para mim?"
    history = "\n".join(
        f"Turn {index}: Question: {turn.get('question', '')}\nAnswer: {turn.get('answer', '')}"
        for index, turn in enumerate(session.history[-6:], start=max(1, len(session.history) - 5))
    ) or "No previous answers; this is the first turn."
    prompt = PROMPT.format(
        persona=session.persona,
        instructions=session.instructions,
        avoid=session.avoid,
        candidate_context=session.candidate_context[:1600],
        history=history[:MAX_PROMPT_HISTORY],
        current_question=question,
        candidate_text=candidate_text[:2200],
        next_question_hint=next_question,
    )
    extra_rules = f"""
REGRAS ESTRUTURADAS DA SESSAO:
Idioma de resposta: {session.language}
criterios obrigatorios a avaliar: {session.must or 'Nenhum criterio adicional.'}
temas/comportamentos a evitar: {session.dont or session.avoid or 'Nenhum adicional.'}
perguntas permitidas: {json.dumps(session.questions_to_ask or session.questions, ensure_ascii=False)}
perguntas a evitar: {json.dumps(session.questions_to_avoid, ensure_ascii=False)}
fidelidade ao guiao: {session.script_fidelity}
tempo total alvo: {session.duration_minutes} minutos
tempo decorrido aproximado: {max(0, int((time.time() - session.started_at) / 60))} minutos

Se a fidelidade for strict, respeita a ordem das perguntas e usa follow-up apenas para esclarecer.
Se for adaptive, escolhe follow-ups curtos e naturais com base na ultima resposta.
Se for free, prioriza uma conversa coerente e pode alterar a ordem.
Nunca repitas uma pergunta ja respondida. Faz uma pergunta de cada vez.
Fala diretamente com o candidato usando "tu"; nao escrevas "o candidato" como se estivesses a produzir um relatorio.
"""
    return prompt + extra_rules


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


def call_openai_compatible(prompt: str, model: str | None = None) -> str:
    chat_url = os.getenv("LLAMA_SERVER_URL", "http://127.0.0.1:8080/v1/chat/completions")
    client = requests.Session()
    client.trust_env = False
    response = client.post(
        chat_url,
        json={
            "model": model or os.getenv("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL),
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": int(os.getenv("LLM_MAX_TOKENS", "128")),
        },
        timeout=180,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("choices"):
        return body["choices"][0]["message"]["content"]
    return body["content"]


def call_ollama(prompt: str, model_override: str = "") -> dict[str, Any]:
    if os.getenv("LIVE_COACH_FAKE_LLM") == "1":
        raise RuntimeError("fake mode is handled by the endpoint")
    model = model_override or os.getenv("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)
    backend = os.getenv("LLM_BACKEND", DEFAULT_BACKEND).lower()
    if backend in {"llama", "llama-server", "llamacpp"}:
        return parse_json(call_openai_compatible(prompt, model))

    if backend == "auto":
        try:
            return parse_json(call_openai_compatible(prompt, model))
        except (requests.RequestException, ValueError, KeyError, TypeError):
            pass

    try:
        response = requests.post(
            os.getenv("OLLAMA_URL", DEFAULT_OLLAMA_GENERATE_URL),
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "keep_alive": os.getenv("OLLAMA_KEEP_ALIVE", "10m"),
                "options": {
                    "temperature": 0.1,
                    "num_ctx": int(os.getenv("LLM_CONTEXT", "2048")),
                    "num_predict": int(os.getenv("LLM_MAX_TOKENS", "128")),
                    "num_thread": int(os.getenv("LLM_THREADS", "4")),
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
    feedback = [str(item).strip() for item in result["feedback"] if str(item).strip()] if isinstance(result["feedback"], list) else [str(result["feedback"]).strip()]
    followups = [str(item).strip() for item in result["followups"] if str(item).strip()] if isinstance(result["followups"], list) else [str(result["followups"] or "").strip()]
    placeholders = {"ponto curto", "ponto 1", "ponto 2", "pergunta", "pergunta ou vazio", "follow-up ou vazio", "followup ou vazio", "frase curta", "vazio"}
    result["feedback"] = [item for item in feedback if item.casefold() not in placeholders][:2]
    result["followups"] = [item for item in followups if item and item.casefold() not in placeholders][:1]
    try:
        result["score"] = max(0, min(5, int(float(result["score"]))))
    except (TypeError, ValueError):
        result["score"] = 0
    result["next_question"] = str(result["next_question"] or "").strip()
    result["notes"] = str(result["notes"] or "").strip()
    if result["next_question"].casefold() in placeholders:
        result["next_question"] = ""
    if result["notes"].casefold() in placeholders:
        result["notes"] = "Resposta analisada no contexto da entrevista."
    return result


def usable_question(value: Any) -> str:
    question = str(value or "").strip()
    if len(question) < 12 or "?" not in question or question.casefold() in {"pergunta", "question", "follow-up", "followup"}:
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


async def warm_whisper_model() -> None:
    if os.getenv("WARMUP_WHISPER", "1") == "1":
        try:
            await asyncio.to_thread(get_whisper_model)
        except Exception:
            pass


async def warm_piper_model() -> None:
    if os.getenv("WARMUP_PIPER", "1") == "1" and DEFAULT_PIPER_VOICE.exists():
        try:
            await asyncio.to_thread(get_piper_voice, DEFAULT_PIPER_VOICE)
        except Exception:
            pass


@app.on_event("startup")
async def preload_local_voice() -> None:
    asyncio.create_task(warm_whisper_model())
    asyncio.create_task(warm_piper_model())


def transcribe_audio(encoded: str, audio_format: str, language: str = DEFAULT_LANGUAGE) -> dict[str, Any]:
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
            language=os.getenv("WHISPER_LANGUAGE", language.split("-", 1)[0]),
            fp16=False,
            temperature=0,
            condition_on_previous_text=False,
        )
        segments = result.get("segments") or []
        no_speech = [float(segment.get("no_speech_prob", 0.0)) for segment in segments if segment.get("no_speech_prob") is not None]
        confidence = max(0.0, min(1.0, 1.0 - (sum(no_speech) / len(no_speech)))) if no_speech else None
        return {
            "text": str(result.get("text", "")).strip(),
            "confidence": round(confidence, 3) if confidence is not None else None,
            "language": result.get("language") or language,
        }
    finally:
        audio_path.unlink(missing_ok=True)


def piper_voice_for(language: str) -> Path:
    configured_voice = os.getenv("PIPER_VOICE")
    if configured_voice:
        return Path(configured_voice)
    normalized = language.lower().replace("_", "-")
    if normalized.startswith("en"):
        english_voice = PIPER_VOICE_DIR / "en_US-lessac-medium.onnx"
        return english_voice if english_voice.exists() else DEFAULT_PIPER_VOICE
    return DEFAULT_PIPER_VOICE


def get_piper_voice(voice_path: Path) -> Any:
    from piper.voice import PiperVoice

    key = str(voice_path.resolve())
    with PIPER_MODEL_LOCK:
        voice = PIPER_MODEL_CACHE.get(key)
        if voice is None:
            voice = PiperVoice.load(voice_path)
            PIPER_MODEL_CACHE[key] = voice
    return voice


def synthesize_speech(text: str, language: str) -> bytes:
    voice_path = piper_voice_for(language)
    if not voice_path.exists():
        raise HTTPException(
            503,
            f"Voz Piper não encontrada: {voice_path}. Executa .\\scripts\\download_piper_voice.ps1",
        )
    try:
        voice = get_piper_voice(voice_path)
        output = BytesIO()
        with wave.open(output, "wb") as wav_file:
            voice.synthesize_wav(text.strip(), wav_file)
        return output.getvalue()
    except (ImportError, OSError, RuntimeError, ValueError):
        # Keep the CLI fallback for installations where piper.voice is not available.
        pass
    piper_executable = os.getenv("PIPER_EXE", "piper")
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as audio_file:
        output_path = Path(audio_file.name)
    try:
        completed = subprocess.run(
            [
                piper_executable,
                "--model",
                str(voice_path),
                "--output_file",
                str(output_path),
            ],
            input=text.strip(),
            capture_output=True,
            text=True,
            timeout=45,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "Piper falhou sem detalhes.").strip()
            raise HTTPException(503, f"Piper indisponível: {detail[:500]}")
        return output_path.read_bytes()
    except FileNotFoundError as exc:
        raise HTTPException(503, "Executável Piper não encontrado. Confirma que piper-tts está instalado.") from exc
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(503, "Piper demorou demasiado a gerar a voz.") from exc
    finally:
        output_path.unlink(missing_ok=True)


def backend_status() -> dict[str, Any]:
    configured_llama_url = os.getenv("LLAMA_SERVER_URL", "http://127.0.0.1:8080/v1/chat/completions")
    llama_url = configured_llama_url.split("/v1/", 1)[0] + "/health"
    ollama_url = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/tags")
    backend = os.getenv("LLM_BACKEND", DEFAULT_BACKEND).lower()
    model = os.getenv("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)
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
        return {"backend": "ollama", "available": True, "model": model, "label": f"Ollama - {model}"}
    except requests.RequestException:
        return {"backend": "offline", "available": False, "model": model, "label": "Nenhum modelo ligado"}


@app.get("/api/status")
def status() -> dict[str, Any]:
    return backend_status()


@app.get("/api/models")
def models() -> dict[str, Any]:
    current = os.getenv("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)
    names: list[str] = []
    try:
        response = requests.get(os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/tags"), timeout=1)
        response.raise_for_status()
        names = [str(item.get("name")) for item in response.json().get("models", []) if item.get("name")]
    except (requests.RequestException, ValueError, TypeError):
        pass
    if not names:
        names = list(DEFAULT_MODEL_OPTIONS)
    if current and current not in names:
        names.append(current)
    return {"models": names, "current": current}


@app.post("/transcribe")
def transcribe(payload: TranscribeRequest) -> dict[str, Any]:
    result = transcribe_audio(payload.audio_base64, payload.audio_format, payload.language)
    return {**result, "model": os.getenv("WHISPER_MODEL", "tiny")}


@app.post("/speak")
def speak(payload: SpeakRequest) -> Response:
    started = time.perf_counter()
    audio = synthesize_speech(payload.text, payload.language)
    if payload.session_id:
        session = session_store.get(payload.session_id, Session)
        session_store.record_metric(
            payload.session_id,
            "tts",
            turn_index=session.current_index if session else 0,
            tts_ms=round((time.perf_counter() - started) * 1000, 1),
        )
    return Response(content=audio, media_type="audio/wav")


@app.get("/health")
def health() -> dict[str, Any]:
    model = backend_status()
    return {
        "ok": True,
        "model": model,
        "configured_backend": os.getenv("LLM_BACKEND", DEFAULT_BACKEND),
        "voice": {
            "backend": "whisper-local",
            "model": os.getenv("WHISPER_MODEL", "tiny"),
            "ready": os.getenv("WHISPER_MODEL", "tiny") in WHISPER_MODEL_CACHE,
        },
        "tts": {"backend": "piper-local", "ready": bool(PIPER_MODEL_CACHE) or DEFAULT_PIPER_VOICE.exists()},
        "active_sessions": session_store.count(),
    }


@app.get("/")
def home() -> FileResponse:
    return FileResponse(project_path("static/index.html"))


@app.post("/start")
def start_session(payload: StartRequest) -> dict[str, Any]:
    if not payload.questions:
        raise HTTPException(422, "A sessão precisa de pelo menos uma pergunta")
    planned_questions = [question.strip() for question in (payload.questions_to_ask or payload.questions) if question.strip()]
    if not planned_questions:
        raise HTTPException(422, "A sessao precisa de pelo menos uma pergunta")
    session_id = str(uuid.uuid4())
    session = Session(
        persona=payload.persona,
        instructions=payload.instructions,
        avoid=payload.avoid,
        candidate_context=payload.candidate_context,
        questions=planned_questions,
        language=payload.language,
        must=payload.must,
        dont=payload.dont or payload.avoid,
        questions_to_ask=planned_questions,
        questions_to_avoid=payload.questions_to_avoid,
        script_fidelity=payload.script_fidelity,
        duration_minutes=payload.duration_minutes,
        min_pause_seconds=payload.min_pause_seconds,
        model=payload.model,
    )
    session_store.save(session_id, session)
    return {
        "session_id": session_id,
        "current_question": planned_questions[0],
        "current_index": 0,
        "config": {
            "language": session.language,
            "min_pause_seconds": session.min_pause_seconds,
            "duration_minutes": session.duration_minutes,
            "script_fidelity": session.script_fidelity,
            "model": session.model or os.getenv("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL),
        },
    }


@app.post("/ask")
def ask(payload: AskRequest) -> dict[str, Any]:
    session = session_store.get(payload.session_id, Session)
    if session is None:
        raise HTTPException(404, "session_id desconhecido")
    if not payload.text_answer and not payload.audio_base64:
        raise HTTPException(400, "Envia text_answer ou audio_base64")
    stt_ms = None
    stt_confidence = payload.stt_confidence
    if payload.text_answer:
        candidate_text = payload.text_answer.strip()
    else:
        stt_started = time.perf_counter()
        transcription = transcribe_audio(payload.audio_base64 or "", payload.audio_format, session.language)
        stt_ms = round((time.perf_counter() - stt_started) * 1000, 1)
        candidate_text = transcription["text"]
        stt_confidence = transcription.get("confidence")
    current_question = session.pending_question or session.questions[session.current_index]
    llm_started = time.perf_counter()
    try:
        result = fake_coach(session, candidate_text) if os.getenv("LIVE_COACH_FAKE_LLM") == "1" else call_ollama(build_prompt(session, candidate_text), session.model)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise HTTPException(503, f"LLM indisponível: {exc}") from exc
    llm_ms = round((time.perf_counter() - llm_started) * 1000, 1)
    followup = usable_question(result.get("followups", [""])[0] if result.get("followups") else "")
    if followup.casefold() == current_question.casefold():
        followup = ""
    next_planned_index = min(session.current_index + 1, len(session.questions) - 1)
    next_planned = session.questions[next_planned_index] if session.questions and session.current_index + 1 < len(session.questions) else "Obrigado. Que perguntas tens para mim?"
    if followup:
        result["next_question"] = followup
        session.pending_question = followup
        result["is_followup"] = True
    else:
        session.pending_question = None
        session.current_index = min(session.current_index + 1, len(session.questions) - 1)
        result["next_question"] = next_planned
        result["is_followup"] = False
    session.history.append({"question": current_question, "answer": candidate_text})
    session_store.save(payload.session_id, session)
    session_store.record_metric(
        payload.session_id,
        "ask",
        turn_index=len(session.history),
        stt_ms=stt_ms,
        llm_ms=llm_ms,
        answer_chars=len(candidate_text),
        stt_confidence=stt_confidence,
        capture_mode=payload.capture_mode,
        auto_submitted=payload.auto_submitted,
    )
    return {"candidate_text": candidate_text, "stt_confidence": stt_confidence, "llm_ms": llm_ms, **result}


@app.get("/api/session/{session_id}/metrics")
def session_metrics(session_id: str) -> dict[str, Any]:
    session = session_store.get(session_id, Session)
    if session is None:
        raise HTTPException(404, "session_id desconhecido")
    return {"session_id": session_id, "metrics": session_store.metrics(session_id)}


@app.post("/stop")
def stop_session(payload: StopRequest) -> dict[str, Any]:
    session = session_store.get(payload.session_id, Session)
    if session is None:
        raise HTTPException(404, "session_id desconhecido")
    session_store.delete(payload.session_id)
    return {"summary": f"Sessão terminada após {len(session.history)} resposta(s).", "history": session.history}


@app.websocket("/ws/{session_id}")
async def interview_socket(websocket: WebSocket, session_id: str) -> None:
    await websocket.accept()
    try:
        while True:
            message = await websocket.receive_json()
            result = await asyncio.to_thread(
                ask,
                AskRequest(
                    session_id=session_id,
                    text_answer=message.get("text_answer"),
                    stt_confidence=message.get("stt_confidence"),
                    capture_mode=message.get("capture_mode", "text"),
                    auto_submitted=bool(message.get("auto_submitted", False)),
                ),
            )
            await websocket.send_json(result)
    except WebSocketDisconnect:
        return
