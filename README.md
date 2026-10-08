# InterviewCoach: Live AI Interview Preparation System

## Abstract
This repository contains the implementation of **InterviewCoach**, a low-latency, full-stack application designed to simulate HR and technical interviews for AI-related positions. The system integrates active listening, real-time speech-to-text (STT), and neural text-to-speech (TTS) to provide a realistic, interactive interview preparation environment. It supports multiple deployment architectures, ranging from local server-based LLMs (Ollama, llama.cpp) to fully client-side execution via WebAssembly (Transformers.js) suitable for static hosting (GitHub Pages).

**Live Demonstration:** [https://ilieiftime.github.io/InterviewCoach/](https://ilieiftime.github.io/InterviewCoach/)

---

## 1. Core Features

*   **Dynamic Persona Configuration:** Configure the interviewer's persona, behavioral constraints (`must`/`dont`), topics to avoid, script fidelity (0-1), and expected duration.
*   **Active Listening & Dynamic Timing:** Maximum listening time per question is calculated dynamically based on the expected answer length (15s to 90s), respecting a configurable `min_pause_seconds` (800-1200ms).
*   **Dual STT Modes:** 
    *   *STT Browser:* Utilizes Web Speech API for lower latency and better live dictation.
    *   *STT Local:* Utilizes OpenAI Whisper for offline, high-accuracy transcription. Includes a confidence threshold (`STT_LOW_CONFIDENCE_THRESHOLD`, default 0.55) that triggers clarification requests.
*   **Neural TTS:** Local neural text-to-speech using Piper, with automatic fallback to browser speech synthesis.
*   **Multi-Backend LLM Support:** Seamlessly switch between Ollama, `llama-server` (llama.cpp), or a browser-local small model (Qwen 0.5B/1.5B via Transformers.js).
*   **Session Persistence & Analytics:** Sessions and per-turn latency metrics (STT and LLM) are stored in a local SQLite database (`sessions.db`) and exposed via API.

---

## 2. System Architecture

```text
Browser (Desktop / Tablet / Phone)
  | HTTP / WebSocket
FastAPI Session + Prompt Orchestration (app.py)
  | Local Backend Selection
Ollama (qwen2.5:1.5b-instruct) OR llama.cpp (llama-server GGUF)
```

*   **Default Backend:** Ollama with `qwen2.5:1.5b-instruct` (selected for low-latency CPU-only environments). The live loop uses a 2048-token context, 96-token response budget, and 30-minute model keep-alive.
*   **Alternative Backend:** `llama-server` (llama.cpp) for direct GGUF execution.
*   **Client-Side Backend:** Transformers.js with WebGPU/WASM for browser-local execution.

---

## 3. Project Structure

```text
InterviewCoach/
├── .github/
│   └── workflows/
│       └── deploy-pages.yml       # CI/CD for GitHub Pages deployment
├── browser-local/                 # Client-side deployment (Transformers.js)
│   ├── index.html
│   ├── sw.js                      # Service Worker for offline caching
│   └── README.md
├── scripts/
│   ├── download_piper_voice.ps1   # PT voice downloader
│   └── download_piper_voice_en.ps1# EN voice downloader
├── app.py                         # FastAPI backend & WebSocket server
├── cli.py                         # Interactive CLI for testing
├── agent_prompt.txt               # LLM prompt template
├── config.json                    # Application configuration
├── requirements.txt               # Python dependencies
├── test_session.ps1               # PowerShell smoke test script
├── sessions.db                    # SQLite session & metrics database
├── .gitattributes                 # Git LFS rules (Note: check filename)
└── README.md                      # This file
```

---

## 4. Installation & Setup (Windows PowerShell)

### 4.1 Prerequisites
*   Python 3.10+
*   Ollama (for local backend)
*   FFmpeg (Optional, automatically detected at `C:\tools\ffmpeg.exe`)

### 4.2 Environment Setup
```powershell
cd C:\Users\iliei\Desktop\EntrevistadorAPX\Mistral-7B-GGUF
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

### 4.3 Core Dependencies (`requirements.txt`)
```text
fastapi>=0.115,<1
uvicorn[standard]>=0.30,<1
langchain>=0.3,<1
langchain-core>=0.3,<1
requests>=2.32,<3
python-dotenv>=1,<2
pydantic>=2,<3
# Local STT: the first voice request downloads the selected Whisper model.
openai-whisper>=20250625
piper-tts>=1.8,<2
```

*(Optional) Install Whisper for local STT:*
```powershell
python -m pip install openai-whisper soundfile
```

---

## 5. Execution Modes

### 5.1 Fast Smoke Test (No LLM Required)
Validates the session flow, WebSocket transport, and frontend using deterministic fake feedback.
```powershell
# Terminal 1
.\.venv\Scripts\Activate.ps1
$env:LIVE_COACH_FAKE_LLM = "1"
uvicorn app:app --port 8000

# Terminal 2
.\test_session.ps1
# Or run the interactive CLI:
python cli.py
```

### 5.2 Option A: Ollama Backend (Recommended for Local)
Start Ollama and pull the default model:
```powershell
ollama pull qwen2.5:1.5b-instruct
uvicorn app:app --reload --host 0.0.0.0 --port 8000
```
*Note: The API preloads Whisper and Piper in the background. First start downloads `WHISPER_MODEL=tiny` by default. Use `WHISPER_MODEL=base` for better quality or `WARMUP_WHISPER=0` to disable preloading.*

Download the Portuguese neural voice if `models\piper` is empty:
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\download_piper_voice.ps1
```

### 5.3 Option B: Local llama.cpp Server
Start the server with the Qwen GGUF model:
```powershell
& "C:\tools\llama.cpp\llama-server.exe" `
  --hf-repo "Qwen/Qwen2.5-1.5B-Instruct-GGUF:Q4_K_M" `
  --host 127.0.0.1 --port 8080 --ctx-size 4096 --threads 12 --parallel 1
```
Start the API configured for llama.cpp:
```powershell
$env:LLM_BACKEND = "llama-server"
$env:LLAMA_SERVER_URL = "http://127.0.0.1:8080/v1/chat/completions"
uvicorn app:app --reload --port 8000
```

### 5.4 Option C: Browser-Local (GitHub Pages)
Runs entirely without FastAPI, Ollama, or `llama-server`. Uses Transformers.js with a quantized Qwen 0.5B ONNX model.
```powershell
cd browser-local
py -m http.server 4173
```
Open `http://127.0.0.1:4173`. The first visit downloads the model; future visits reuse the browser cache.

---

## 6. API Endpoints

*   `GET /health` - Health check.
*   `POST /start` - Initialize a session. Accepts persona, instructions, and questions.
*   `POST /ask` - Send text or base64 audio for transcription and coaching.
*   `POST /transcribe` - Local STT endpoint.
*   `POST /speak` - TTS endpoint (returns 503 if Piper voice is missing).
*   `GET /api/models` - List available Ollama models.
*   `POST /api/model` - Switch Ollama model without restarting.
*   `GET /api/session/{id}/metrics` - Retrieve per-turn latency metrics.
*   `WS /ws/{session_id}` - WebSocket for live text exchange.

---

## 7. Known Issues & Notes

*   **Git LFS:** The file `gitattributes` in the root lacks the leading dot (should be `.gitattributes`). Git is currently not applying LFS rules. It is recommended to rename this before the next commit.
*   **Network Access:** For access from another device, start Uvicorn with `--host 0.0.0.0` and use the LAN address (e.g., `http://192.168.1.174:8000`). Keep behind a trusted local network until authentication and HTTPS are added.
*   **Model Limitations:** The browser-local Qwen 0.5B model is less capable than Mistral 7B. It compensates with a deterministic interview rubric, short context windows, and immediate local feedback.
