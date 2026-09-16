# Entrevistador APX local

## Arranque recomendado

Com o Ollama instalado em `C:\Users\iliei\AppData\Local\Programs\Ollama`:

```powershell
cd C:\Users\iliei\Desktop\EntrevistadorAPX\Mistral-7B-GGUF
$env:OLLAMA_MODEL = 'qwen2.5:1.5b-instruct'
$env:LLM_THREADS = '4'
$env:LLM_CONTEXT = '2048'
$env:LLM_MAX_TOKENS = '160'
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Abrir `http://127.0.0.1:8000`. O primeiro clique em `Escuta ativa` pede permissão ao microfone. `Microfone` é o modo explícito: grava até clicares novamente e preenche a resposta sem a enviar automaticamente.

## Modelos

- `qwen2.5:1.5b-instruct`: predefinido; melhor equilíbrio entre compreensão e latência para esta conversa.
- `qwen2.5:0.5b-instruct`: fallback para baixa RAM/latência, com mais respostas genéricas.
- `qwen2.5:7b`: usar quando a prioridade é qualidade e existe RAM/CPU suficiente; não é o modo de baixa latência.

O backend pode ser trocado para `llama-server` com:

```powershell
$env:LLM_BACKEND = 'llama-server'
$env:LLAMA_SERVER_URL = 'http://127.0.0.1:8080/v1/chat/completions'
```

## Escuta ativa

Os valores iniciais são pausa curta de 1000 ms para o STT browser, pausa longa de 3500 ms para o VAD local, timeout máximo de 90 s e confirmação STT abaixo de 0.60. A interface permite alterar pausa longa, timeout, limiar e número máximo de follow-ups por tema.

O pipeline guarda duração de escuta, duração do áudio, latências STT/LLM/TTS, confiança STT, confirmações, follow-ups e interrupções em SQLite. No fim da sessão recolhe NPS opcional.

## Testes

```powershell
python -m unittest discover -s tests -v
python scripts/benchmark_local.py --repeat 1 --output .\benchmark-result.json
```

O processamento de voz é local por padrão: Whisper para STT e Piper para TTS. Os dados da sessão ficam no `sessions.db`; para partilha ou produção, proteger o ficheiro e a origem HTTPS do serviço.

## Browser/GitHub Pages

`browser-local` é o modo sem servidor. A inferência corre num Web Worker com Qwen 0.5B ONNX; o Service Worker guarda a aplicação e o Transformers.js guarda os pesos no cache do browser. O workflow `.github/workflows/deploy-pages.yml` publica essa pasta na branch `gh-pages` após um push para `main` ou `master`.
