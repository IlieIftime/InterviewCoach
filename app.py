"""Minimal live interview coach API."""
from __future__ import annotations

import base64
import asyncio
import json
import os
import re
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
DEFAULT_MIN_PAUSE_SECONDS = 1.0
DEFAULT_DURATION_MINUTES = 20
DEFAULT_SHORT_PAUSE_MS = 1000
DEFAULT_LONG_PAUSE_MS = 3500
DEFAULT_MAX_ANSWER_SECONDS = 90
DEFAULT_CONFIRM_THRESHOLD = 0.60
DEFAULT_MAX_FOLLOWUPS = 2
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
    covered_topics: list[str] = field(default_factory=list)
    missing_topics: list[str] = field(default_factory=list)
    followup_count: int = 0
    interruption_count: int = 0
    pending_transcript: str | None = None
    pending_confidence: float | None = None
    confirmation_pending: bool = False
    short_pause_ms: int = DEFAULT_SHORT_PAUSE_MS
    long_pause_ms: int = DEFAULT_LONG_PAUSE_MS
    max_answer_seconds: int = DEFAULT_MAX_ANSWER_SECONDS
    confirm_threshold: float = DEFAULT_CONFIRM_THRESHOLD
    max_followups: int = DEFAULT_MAX_FOLLOWUPS


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
    short_pause_ms: int = Field(default=DEFAULT_SHORT_PAUSE_MS, ge=600, le=5000)
    long_pause_ms: int = Field(default=DEFAULT_LONG_PAUSE_MS, ge=1500, le=8000)
    max_answer_seconds: int = Field(default=DEFAULT_MAX_ANSWER_SECONDS, ge=15, le=180)
    confirm_threshold: float = Field(default=DEFAULT_CONFIRM_THRESHOLD, ge=0.2, le=0.95)
    max_followups: int = Field(default=DEFAULT_MAX_FOLLOWUPS, ge=0, le=5)


class AskRequest(BaseModel):
    session_id: str
    text_answer: str | None = None
    audio_base64: str | None = None
    audio_format: str = "wav"
    stt_confidence: float | None = Field(default=None, ge=0, le=1)
    capture_mode: str = "text"
    auto_submitted: bool = False
    listen_ms: float | None = Field(default=None, ge=0)
    audio_ms: float | None = Field(default=None, ge=0)
    stt_confirmed: bool = False
    interrupted: bool = False


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
    nps_score: int | None = Field(default=None, ge=0, le=10)
    naturalness: int | None = Field(default=None, ge=0, le=10)
    listening_quality: int | None = Field(default=None, ge=0, le=10)
    followup_relevance: int | None = Field(default=None, ge=0, le=10)
    notes: str = ""


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


PROMPT = """SYSTEM:
És o entrevistador e coach da conversa. Desempenha a persona indicada e fala diretamente com o candidato, usando português de Portugal. Não és apenas um gerador de perguntas: primeiro reconhece brevemente o que foi dito, depois decide se falta informação e só então escolhe a pergunta seguinte.

Regras:
- sê humano, caloroso e conciso;
- nunca inventes experiência ou resultados;
- nunca repitas uma pergunta já respondida;
- faz uma pergunta de cada vez;
- se faltar uma evidência importante, faz um follow-up específico;
- se a resposta cobrir a pergunta, avança no guião;
- respeita perguntas e temas proibidos;
- fala com "tu" e nunca escrevas "o candidato" como narrador;
- o campo reply será lido em voz alta; feedback e notes são privados.

Contexto da sessão:
Persona: {persona}
Instruções: {instructions}
Evitar: {avoid}
Critérios obrigatórios: {must}
Contexto do candidato: {candidate_context}
Modo do guião: {script_fidelity}
Perguntas permitidas: {questions_to_ask}
Perguntas a evitar: {questions_to_avoid}
Tópicos já cobertos: {covered_topics}
Pontos ainda em falta: {missing_topics}

Histórico recente:
{history}

Pergunta atual: {current_question}
Resposta transcrita: {candidate_text}
Próxima pergunta planeada: {next_question_hint}

Devolve apenas JSON válido com estes campos:
reply (uma ou duas frases naturais para dizer em voz alta),
question (uma pergunta real ou string vazia),
question_kind (followup, planned, clarification ou close),
should_advance (boolean),
covered (lista curta),
missing (lista curta),
feedback (até duas observações privadas),
score (0 a 5),
notes (uma frase privada).
Não uses nomes de campos como conteúdo e não devolvas markdown.
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
        f"Turn {index}: Question: {turn.get('question', '')}\nAnswer: {turn.get('answer', '')}\nAgent reply: {turn.get('reply', '')}\nCovered: {turn.get('covered', [])}\nMissing: {turn.get('missing', [])}"
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
        must=session.must or "Nenhum critério adicional.",
        script_fidelity=session.script_fidelity,
        questions_to_ask=json.dumps(session.questions_to_ask or session.questions, ensure_ascii=False),
        questions_to_avoid=json.dumps(session.questions_to_avoid, ensure_ascii=False),
        covered_topics=json.dumps(session.covered_topics[-12:], ensure_ascii=False),
        missing_topics=json.dumps(session.missing_topics[-12:], ensure_ascii=False),
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
Mantem o JSON compacto: reply com no maximo 160 caracteres, feedback com um item, covered e missing com no maximo dois itens curtos, e notes com no maximo 100 caracteres.
"""
    return prompt + extra_rules


def fake_coach(session: Session, candidate_text: str) -> dict[str, Any]:
    next_index = session.current_index + 1
    next_question = session.questions[next_index] if next_index < len(session.questions) else "Obrigado. Que perguntas tens para mim?"
    return {
        "reply": "Percebi. Há um ponto interessante aí; vou pedir-te um exemplo concreto para perceber melhor o impacto.",
        "question": "Que decisão técnica teve maior impacto no resultado?",
        "question_kind": "followup",
        "should_advance": False,
        "covered": ["experiência ou contexto apresentado"],
        "missing": ["exemplo concreto", "impacto mensurável"],
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
            "max_tokens": int(os.getenv("LLM_MAX_TOKENS", "160")),
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
                    "num_predict": int(os.getenv("LLM_MAX_TOKENS", "160")),
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


def _clean_list(value: Any, limit: int = 4, max_chars: int = 220) -> list[str]:
    values = value if isinstance(value, list) else [value]
    cleaned: list[str] = []
    for item in values:
        text = str(item or "").strip()
        if text and text not in cleaned:
            cleaned.append(text[:max_chars])
    return cleaned[:limit]


def _extract_json_string(raw: str, key: str) -> str:
    match = re.search(rf'"{re.escape(key)}"\s*:\s*"((?:\\.|[^"\\])*)', raw, re.DOTALL)
    if not match:
        return ""
    try:
        return str(json.loads('"' + match.group(1) + '"')).strip()
    except json.JSONDecodeError:
        return match.group(1).strip()


def parse_json(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            result = {
                "reply": _extract_json_string(raw, "reply"),
                "question": _extract_json_string(raw, "question"),
                "question_kind": _extract_json_string(raw, "question_kind"),
                "notes": "O modelo devolveu JSON incompleto; foram recuperados os campos disponíveis.",
            }
        try:
            if start >= 0 and end > start:
                result = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            result = {
                "reply": _extract_json_string(raw, "reply"),
                "question": _extract_json_string(raw, "question"),
                "question_kind": _extract_json_string(raw, "question_kind"),
                "notes": "O modelo devolveu JSON incompleto; foram recuperados os campos disponíveis.",
            }
    if not isinstance(result, dict):
        raise ValueError("Resposta do modelo deve ser um objeto JSON")
    placeholders = {
        "ponto curto", "ponto 1", "ponto 2", "pergunta", "pergunta ou vazio",
        "follow-up ou vazio", "followup ou vazio", "frase curta", "vazio", "string vazia",
    }
    result["feedback"] = [item for item in _clean_list(result.get("feedback", []), 2) if item.casefold() not in placeholders]
    legacy_followups = result.get("followups", result.get("follow_up", []))
    result["followups"] = [item for item in _clean_list(legacy_followups, 1) if item.casefold() not in placeholders]
    result["question"] = str(result.get("question") or "").strip()
    if not result["question"] and result["followups"]:
        result["question"] = result["followups"][0]
    if not result["question"]:
        result["question"] = str(result.get("next_question") or "").strip()
    if result["question"].casefold() in placeholders:
        result["question"] = ""
    result["question_kind"] = str(result.get("question_kind") or "").strip().lower()
    if result["question_kind"] not in {"followup", "planned", "clarification", "close"}:
        result["question_kind"] = "followup" if result["question"] else "planned"
    result["should_advance"] = bool(result.get("should_advance", result["question_kind"] in {"planned", "close"}))
    result["covered"] = _clean_list(result.get("covered", []), 6)
    result["missing"] = _clean_list(result.get("missing", []), 6)
    try:
        result["score"] = max(0, min(5, int(float(result.get("score", 0)))))
    except (TypeError, ValueError):
        result["score"] = 0
    result["reply"] = str(result.get("reply") or result.get("response") or "").strip()
    result["next_question"] = str(result.get("next_question") or result["question"] or "").strip()
    result["notes"] = str(result.get("notes") or "").strip()
    if result["next_question"].casefold() in placeholders:
        result["next_question"] = ""
    if result["notes"].casefold() in placeholders:
        result["notes"] = "Resposta analisada no contexto da entrevista."
    if not result["reply"]:
        result["reply"] = "Percebi. Vou usar o que disseste para orientar a próxima pergunta."
    if result["question"] and not result["followups"] and result["question_kind"] in {"followup", "clarification"}:
        result["followups"] = [result["question"]]
    return result


def usable_question(value: Any) -> str:
    question = str(value or "").strip()
    if len(question) < 12 or "?" not in question or question.casefold() in {"pergunta", "question", "follow-up", "followup"}:
        return ""
    return question


def is_confirmation_yes(value: str) -> bool:
    normalized = " ".join(value.casefold().strip().split()).strip(".!?,;:")
    return normalized in {"sim", "sim está", "sim esta", "correto", "correcto", "certo", "isso", "é isso", "e isso", "confirmo"}


def is_confirmation_no(value: str) -> bool:
    normalized = " ".join(value.casefold().strip().split()).strip(".!?,;:")
    return normalized == "não" or normalized == "nao" or normalized.startswith("não, ") or normalized.startswith("nao, ")


def forbidden_question(question: str, session: Session) -> bool:
    normalized = question.casefold().strip()
    return any(item.casefold().strip() and item.casefold().strip() in normalized for item in session.questions_to_avoid)


def fallback_reply(question_kind: str) -> str:
    if question_kind in {"followup", "clarification"}:
        return "Percebi. Vou aprofundar esse ponto."
    return "Obrigado pela resposta. Vamos continuar."


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
        short_pause_ms=payload.short_pause_ms,
        long_pause_ms=payload.long_pause_ms,
        max_answer_seconds=payload.max_answer_seconds,
        confirm_threshold=payload.confirm_threshold,
        max_followups=payload.max_followups,
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
            "short_pause_ms": session.short_pause_ms,
            "long_pause_ms": session.long_pause_ms,
            "max_answer_seconds": session.max_answer_seconds,
            "confirm_threshold": session.confirm_threshold,
            "max_followups": session.max_followups,
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

    if session.confirmation_pending:
        if is_confirmation_yes(candidate_text):
            candidate_text = (session.pending_transcript or "").strip()
            stt_confidence = session.pending_confidence
            session.pending_transcript = None
            session.pending_confidence = None
            session.confirmation_pending = False
            payload.stt_confirmed = True
        elif is_confirmation_no(candidate_text):
            session.pending_transcript = None
            session.pending_confidence = None
            session.confirmation_pending = False
            session_store.save(payload.session_id, session)
            return {
                "candidate_text": "",
                "reply": "Está bem. Repete a resposta com calma, por favor.",
                "question": "",
                "next_question": "",
                "needs_confirmation": False,
                "stt_confidence": stt_confidence,
                "llm_ms": None,
                "feedback": [],
                "followups": [],
            }
        else:
            session.pending_transcript = None
            session.pending_confidence = None
            session.confirmation_pending = False

    if (
        payload.capture_mode != "text"
        and stt_confidence is not None
        and stt_confidence < session.confirm_threshold
        and not payload.stt_confirmed
    ):
        session.pending_transcript = candidate_text
        session.pending_confidence = stt_confidence
        session.confirmation_pending = True
        session_store.save(payload.session_id, session)
        quoted = candidate_text[:180] if candidate_text else "(sem texto)"
        return {
            "candidate_text": candidate_text,
            "reply": f"Ouvi: «{quoted}». Está correto?",
            "question": "",
            "next_question": "",
            "needs_confirmation": True,
            "stt_confidence": stt_confidence,
            "llm_ms": None,
            "feedback": [],
            "followups": [],
        }

    if not candidate_text:
        raise HTTPException(400, "A transcrição não contém texto suficiente")
    if payload.interrupted:
        session.interruption_count += 1
    current_question = session.pending_question or session.questions[session.current_index]
    llm_started = time.perf_counter()
    try:
        result = fake_coach(session, candidate_text) if os.getenv("LIVE_COACH_FAKE_LLM") == "1" else call_ollama(build_prompt(session, candidate_text), session.model)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise HTTPException(503, f"LLM indisponível: {exc}") from exc
    llm_ms = round((time.perf_counter() - llm_started) * 1000, 1)
    result["reply"] = str(result.get("reply") or fallback_reply(str(result.get("question_kind", ""))))
    result["covered"] = _clean_list(result.get("covered", []), 6)
    result["missing"] = _clean_list(result.get("missing", []), 6)
    candidate_question = usable_question(result.get("question"))
    if not candidate_question:
        candidate_question = usable_question(result.get("followups", [""])[0] if result.get("followups") else "")
    if candidate_question.casefold() == current_question.casefold() or forbidden_question(candidate_question, session):
        candidate_question = ""
    kind = str(result.get("question_kind") or "").lower()
    wants_followup = bool(candidate_question) and (kind in {"followup", "clarification"} or not result.get("should_advance", True))
    if wants_followup and session.followup_count >= session.max_followups:
        wants_followup = False
        candidate_question = ""
    next_planned_index = min(session.current_index + 1, len(session.questions) - 1)
    next_planned = session.questions[next_planned_index] if session.questions and session.current_index + 1 < len(session.questions) else "Obrigado. Que perguntas tens para mim?"
    if wants_followup:
        result["question"] = candidate_question
        result["next_question"] = candidate_question
        result["question_kind"] = kind or "followup"
        result["should_advance"] = False
        session.pending_question = candidate_question
        session.followup_count += 1
        result["is_followup"] = True
    else:
        if forbidden_question(next_planned, session):
            next_planned = "Podes dar um exemplo concreto dessa experiência?"
        session.pending_question = None
        session.current_index = min(session.current_index + 1, len(session.questions) - 1)
        session.followup_count = 0
        result["question"] = next_planned
        result["question_kind"] = "planned" if "Obrigado" not in next_planned else "close"
        result["should_advance"] = True
        result["next_question"] = next_planned
        result["is_followup"] = False
    session.covered_topics = list(dict.fromkeys((session.covered_topics + result["covered"])[-24:]))
    session.missing_topics = list(dict.fromkeys((session.missing_topics + result["missing"])[-24:]))
    session.history.append({
        "question": current_question,
        "answer": candidate_text,
        "reply": result["reply"],
        "covered": result["covered"],
        "missing": result["missing"],
        "question_kind": result["question_kind"],
        "stt_confidence": stt_confidence,
    })
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
        listen_ms=payload.listen_ms,
        audio_ms=payload.audio_ms,
        interruptions=session.interruption_count,
        followup_count=session.followup_count,
        stt_confirmed=payload.stt_confirmed,
    )
    return {
        "candidate_text": candidate_text,
        "stt_confidence": stt_confidence,
        "llm_ms": llm_ms,
        "needs_confirmation": False,
        **result,
    }


@app.get("/api/session/{session_id}/metrics")
def session_metrics(session_id: str) -> dict[str, Any]:
    session = session_store.get(session_id, Session)
    metrics = session_store.metrics(session_id)
    if session is None and not metrics:
        raise HTTPException(404, "session_id desconhecido")
    return {"session_id": session_id, "metrics": metrics, "feedback": session_store.feedback(session_id)}


@app.post("/stop")
def stop_session(payload: StopRequest) -> dict[str, Any]:
    session = session_store.get(payload.session_id, Session)
    if session is None:
        raise HTTPException(404, "session_id desconhecido")
    feedback_values = payload.model_dump(exclude={"session_id"}, exclude_none=True)
    if feedback_values:
        session_store.save_feedback(payload.session_id, **feedback_values)
    session_store.delete(payload.session_id)
    return {
        "summary": f"Sessão terminada após {len(session.history)} resposta(s).",
        "history": session.history,
        "metrics": session_store.metrics(payload.session_id),
        "feedback": session_store.feedback(payload.session_id),
    }


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
                    listen_ms=message.get("listen_ms"),
                    audio_ms=message.get("audio_ms"),
                    stt_confirmed=bool(message.get("stt_confirmed", False)),
                    interrupted=bool(message.get("interrupted", False)),
                ),
            )
            await websocket.send_json(result)
    except WebSocketDisconnect:
        return
