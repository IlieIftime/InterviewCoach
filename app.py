"""Minimal live interview coach API."""
from __future__ import annotations

import base64
import asyncio
import json
import os
import subprocess
import tempfile
import threading
import uuid
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


class StartRequest(BaseModel):
    persona: str = Field(min_length=1)
    instructions: str = "Be concise and curious."
    avoid: str = "Do not invent facts or make promises."
    candidate_context: str = ""
    questions: list[str] = Field(default_factory=lambda: ["Fala-me sobre ti e sobre o teu percurso profissional."])


class AskRequest(BaseModel):
    session_id: str
    text_answer: str | None = None
    audio_base64: str | None = None
    audio_format: str = "wav"


class TranscribeRequest(BaseModel):
    audio_base64: str = Field(min_length=1)
    audio_format: str = "webm"


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1, max_length=1600)
    language: str = "pt-PT"


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


def project_path(filename: str) -> Path:
    return Path(__file__).resolve().parent / filename


session_store = SessionStore(project_path("sessions.db"))
WHISPER_MODEL_CACHE: dict[str, Any] = {}
WHISPER_MODEL_LOCK = threading.Lock()
FFMPEG_DIR = Path(r"C:\tools")
if (FFMPEG_DIR / "ffmpeg.exe").exists():
    os.environ["PATH"] = f"{FFMPEG_DIR}{os.pathsep}{os.environ.get('PATH', '')}"


def build_prompt(session: Session, candidate_text: str) -> str:
    question = session.pending_question or session.questions[session.current_index]
    next_index = min(session.current_index + 1, len(session.questions) - 1)
    next_question = session.questions[next_index] if session.questions and next_index < len(session.questions) else "Que perguntas tens para mim?"
    history = "\n".join(
        f"Turn {index}: Question: {turn.get('question', '')}\nAnswer: {turn.get('answer', '')}"
        for index, turn in enumerate(session.history[-6:], start=max(1, len(session.history) - 5))
    ) or "No previous answers; this is the first turn."
    return PROMPT.format(
        persona=session.persona,
        instructions=session.instructions,
        avoid=session.avoid,
        candidate_context=session.candidate_context[:1600],
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


def call_ollama(prompt: str) -> dict[str, Any]:
    if os.getenv("LIVE_COACH_FAKE_LLM") == "1":
        raise RuntimeError("fake mode is handled by the endpoint")
    model = os.getenv("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)
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
            os.getenv("OLLAMA_URL", DEFAULT_OLLAMA_GENERATE_URL),
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


async def warm_whisper_model() -> None:
    if os.getenv("WARMUP_WHISPER", "1") == "1":
        try:
            await asyncio.to_thread(get_whisper_model)
        except Exception:
            pass


@app.on_event("startup")
async def preload_local_voice() -> None:
    asyncio.create_task(warm_whisper_model())


def transcribe_audio(encoded: str, audio_format: str) -> str:
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
        return str(result.get("text", "")).strip()
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


def synthesize_speech(text: str, language: str) -> bytes:
    voice_path = piper_voice_for(language)
    if not voice_path.exists():
        raise HTTPException(
            503,
            f"Voz Piper não encontrada: {voice_path}. Executa .\\scripts\\download_piper_voice.ps1",
        )
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


@app.post("/transcribe")
def transcribe(payload: TranscribeRequest) -> dict[str, str]:
    text = transcribe_audio(payload.audio_base64, payload.audio_format)
    return {"text": text, "model": os.getenv("WHISPER_MODEL", "tiny")}


@app.post("/speak")
def speak(payload: SpeakRequest) -> Response:
    audio = synthesize_speech(payload.text, payload.language)
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
        "active_sessions": session_store.count(),
    }


@app.get("/")
def home() -> FileResponse:
    return FileResponse(project_path("static/index.html"))


@app.post("/start")
def start_session(payload: StartRequest) -> dict[str, str]:
    if not payload.questions:
        raise HTTPException(422, "A sessão precisa de pelo menos uma pergunta")
    session_id = str(uuid.uuid4())
    session = Session(
        persona=payload.persona,
        instructions=payload.instructions,
        avoid=payload.avoid,
        candidate_context=payload.candidate_context,
        questions=payload.questions,
    )
    session_store.save(session_id, session)
    return {"session_id": session_id, "current_question": payload.questions[0]}


@app.post("/ask")
def ask(payload: AskRequest) -> dict[str, Any]:
    session = session_store.get(payload.session_id, Session)
    if session is None:
        raise HTTPException(404, "session_id desconhecido")
    if not payload.text_answer and not payload.audio_base64:
        raise HTTPException(400, "Envia text_answer ou audio_base64")
    candidate_text = payload.text_answer or transcribe_audio(payload.audio_base64 or "", payload.audio_format)
    current_question = session.pending_question or session.questions[session.current_index]
    try:
        result = fake_coach(session, candidate_text) if os.getenv("LIVE_COACH_FAKE_LLM") == "1" else call_ollama(build_prompt(session, candidate_text))
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise HTTPException(503, f"LLM indisponível: {exc}") from exc
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
    return {"candidate_text": candidate_text, **result}


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
            result = await asyncio.to_thread(ask, AskRequest(session_id=session_id, text_answer=message.get("text_answer")))
            await websocket.send_json(result)
    except WebSocketDisconnect:
        return
