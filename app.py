"""Minimal live interview coach API."""
from __future__ import annotations

import base64
import asyncio
import json
import os
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from session_store import SessionStore

try:
    from langchain_core.prompts import ChatPromptTemplate
except ImportError:  # pragma: no cover - requirements.txt installs it
    ChatPromptTemplate = None

app = FastAPI(title="Live Interview Coach", version="0.2.0")
app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parent / "static"), name="static")


@dataclass
class Session:
    persona: str
    instructions: str
    avoid: str
    candidate_context: str
    questions: list[str]
    current_index: int = 0
    history: list[dict[str, str]] = field(default_factory=list)


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


class StopRequest(BaseModel):
    session_id: str


PROMPT = """SYSTEM:
You are an expert interview coach for Machine Learning Platform Engineer roles. Be natural, curious, and concise. Ask follow-ups, give constructive feedback, and score answers from 0 to 5. Keep the JSON response under 120 tokens. Return valid JSON only.

USER:
Persona: {persona}
Instructions: {instructions}
Avoid: {avoid}
Additional candidate information: {candidate_context}
Current question: {current_question}
Candidate answer: {candidate_text}

Produce JSON with exactly these keys:
{{"feedback": ["bullet 1", "bullet 2"], "followups": ["question"], "next_question": "question", "score": 0, "notes": "one-sentence rationale"}}
"""


def project_path(filename: str) -> Path:
    return Path(__file__).resolve().parent / filename


session_store = SessionStore(project_path("sessions.db"))


def build_prompt(session: Session, candidate_text: str) -> str:
    question = session.questions[session.current_index]
    if ChatPromptTemplate is not None:
        template = ChatPromptTemplate.from_template(PROMPT)
        return template.format(
            persona=session.persona,
            instructions=session.instructions,
            avoid=session.avoid,
            candidate_context=session.candidate_context,
            current_question=question,
            candidate_text=candidate_text,
        )
    return PROMPT.format(
        persona=session.persona,
        instructions=session.instructions,
        avoid=session.avoid,
        candidate_context=session.candidate_context,
        current_question=question,
        candidate_text=candidate_text,
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
    server_root = chat_url.split("/v1/", 1)[0]
    response = client.post(
        f"{server_root}/completion",
        json={
            "prompt": f"[INST] {prompt} [/INST]",
            "temperature": 0.2,
            "n_predict": 120,
        },
        timeout=180,
    )
    response.raise_for_status()
    return response.json()["content"]


def call_ollama(prompt: str) -> dict[str, Any]:
    if os.getenv("LIVE_COACH_FAKE_LLM") == "1":
        raise RuntimeError("fake mode is handled by the endpoint")
    model = os.getenv("OLLAMA_MODEL", "mistral:7b")
    backend = os.getenv("LLM_BACKEND", "auto").lower()
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
            json={"model": model, "prompt": prompt, "stream": False, "format": "json"},
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
    return result


def transcribe_audio(encoded: str, audio_format: str) -> str:
    try:
        import whisper
    except ImportError as exc:
        raise HTTPException(400, "Whisper não está instalado; envia text_answer ou instala openai-whisper.") from exc
    try:
        audio_bytes = base64.b64decode(encoded)
    except ValueError as exc:
        raise HTTPException(400, "audio_base64 inválido") from exc
    suffix = "." + audio_format.lstrip(".")
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as audio_file:
        audio_file.write(audio_bytes)
        audio_path = Path(audio_file.name)
    try:
        model = whisper.load_model(os.getenv("WHISPER_MODEL", "base"))
        return str(model.transcribe(str(audio_path))["text"]).strip()
    finally:
        audio_path.unlink(missing_ok=True)


def backend_status() -> dict[str, Any]:
    configured_llama_url = os.getenv("LLAMA_SERVER_URL", "http://127.0.0.1:8080/v1/chat/completions")
    llama_url = configured_llama_url.split("/v1/", 1)[0] + "/health"
    ollama_url = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/tags")
    if os.getenv("LIVE_COACH_FAKE_LLM") == "1":
        return {"backend": "demo", "available": True, "label": "Demo mode"}
    try:
        requests.get(llama_url, timeout=1).raise_for_status()
        return {"backend": "llama-server", "available": True, "label": "llama.cpp local"}
    except requests.RequestException:
        pass
    try:
        requests.get(ollama_url, timeout=1).raise_for_status()
        return {"backend": "ollama", "available": True, "label": "Ollama local"}
    except requests.RequestException:
        return {"backend": "offline", "available": False, "label": "No model connected"}


@app.get("/api/status")
def status() -> dict[str, Any]:
    return backend_status()


@app.get("/health")
def health() -> dict[str, Any]:
    model = backend_status()
    return {
        "ok": True,
        "model": model,
        "configured_backend": os.getenv("LLM_BACKEND", "auto"),
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
        payload.persona,
        payload.instructions,
        payload.avoid,
        payload.candidate_context,
        payload.questions,
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
    current_question = session.questions[session.current_index]
    try:
        result = fake_coach(session, candidate_text) if os.getenv("LIVE_COACH_FAKE_LLM") == "1" else call_ollama(build_prompt(session, candidate_text))
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise HTTPException(503, f"LLM indisponível: {exc}") from exc
    session.history.append({"question": current_question, "answer": candidate_text})
    session.current_index = min(session.current_index + 1, len(session.questions) - 1)
    session_store.save(payload.session_id, session)
    if not result.get("next_question"):
        next_index = session.current_index
        result["next_question"] = session.questions[next_index] if next_index < len(session.questions) else "Obrigado. Que perguntas tens para mim?"
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
