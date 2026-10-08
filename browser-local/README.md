# Northstar Browser Local

Variante do coach que corre diretamente no browser, sem FastAPI, Ollama ou llama-server. Depois da instalação inicial, funciona como uma PWA independente do PC que serviu os ficheiros.

## Arrancar

A partir desta pasta:

```powershell
cd C:\Users\iliei\Desktop\EntrevistadorAPX\Mistral-7B-GGUF\browser-local
py -m http.server 4173
```

Abrir `http://127.0.0.1:4173` e instalar a aplicação pelo menu do browser (`Instalar Northstar Local Coach`). Num dispositivo externo, serve esta pasta por HTTPS ou por um hosting estático e abre o URL nesse dispositivo.

## Publicar no GitHub Pages

O workflow em `../.github/workflows/deploy-pages.yml` publica automaticamente esta pasta.

**Importante — histórico de falhas anteriores**: o histórico de commits deste repositório mostra 5 tentativas anteriores de configurar este deploy, todas falhadas (`Fix Pages deploy workflow failure`, `Fix Pages workflow permissions`, `Allow protected GitHub Pages branch updates`, até `Remove failing protected Pages workflow`). Essas tentativas publicavam diretamente para a branch `gh-pages` (via `peaceiris/actions-gh-pages`), e o padrão de erros ("permissões", "branch protegida") indica que isso esbarrava numa proteção de branch ou numa permissão do token `GITHUB_TOKEN` ao nível do repositório — algo que não se resolve só ajustando o YAML.

O workflow atual usa o fluxo **oficial** do GitHub (`actions/configure-pages` + `actions/upload-pages-artifact` + `actions/deploy-pages`), que publica através de um "ambiente" de deployment gerido pelo GitHub em vez de escrever numa branch — por isso não é afetado por proteção de branch.

**Passo manual obrigatório (uma vez só, no GitHub, não pode ser feito por automação):**

1. `Settings > Pages`
2. Em `Build and deployment > Source`, escolher **`GitHub Actions`** (não `Deploy from a branch`)
3. Guardar. Não é preciso escolher branch nem pasta — o workflow trata disso.

No PowerShell, a partir da raiz do projeto:

```powershell
cd C:\Users\iliei\Desktop\EntrevistadorAPX\Mistral-7B-GGUF
git add .
git commit -m "Fase 2: escuta dinâmica, idioma, seleção de modelo no browser-local"
git push mainsource master
```

Depois do workflow terminar (separador `Actions` no GitHub), o URL fica:

```text
https://ilieiftime.github.io/InterviewCoach/
```

Abrir esse URL no dispositivo X, clicar em `Preparar modelo local`, instalar a PWA e esperar o download terminar. A partir daí, o dispositivo X funciona sem o PC e sem o terminal.

Nao abrir com `file://`: ES modules, WebGPU e o download do modelo precisam de HTTP/HTTPS.

## Modelo e cache

O ecrã de configuração deixa escolher entre dois modelos via Transformers.js: `onnx-community/Qwen2.5-0.5B-Instruct-ONNX` (mais leve/rápido) e `onnx-community/Qwen2.5-1.5B-Instruct-ONNX` (mais qualidade), ambos dentro do limite de ~2-3GB pedido. O browser descarrega os artefactos quantizados na primeira sessao para o modelo escolhido e usa `env.useBrowserCache = true` nas seguintes. O dispositivo tenta WebGPU e cai para WASM; se nenhum dos dois estiver disponível, a app mostra um aviso claro em vez de falhar silenciosamente.

O tamanho exato depende dos artefactos publicados e da variante quantizada selecionada pelo runtime. A primeira inicializacao pode demorar; o progresso aparece no botao e o modelo nao e descarregado novamente enquanto o cache do browser existir.

Depois de abrir uma vez e instalar a PWA, os ficheiros da aplicação ficam no service worker e o modelo fica no storage do browser. Pode fechar o terminal e desligar o PC original. O dispositivo continua a funcionar offline, desde que não limpe os dados do site/browser.

Importante: a primeira instalação ainda precisa de uma origem web. Para uso em qualquer dispositivo, publique esta pasta num hosting estático HTTPS, por exemplo GitHub Pages, Netlify ou Cloudflare Pages. O servidor apenas entrega os ficheiros; a inferência corre no dispositivo.

## O que melhora a qualidade

- O modelo recebe apenas as ultimas tres trocas e um contexto candidato truncado.
- A instrucao pede respostas curtas, uma melhoria concreta e uma pergunta seguinte.
- O score local usa sinais de exemplo, acao, resultado e reflexao, em vez de confiar cegamente no modelo pequeno.
- Se a saida do modelo nao for bem formada, a conversa continua com a proxima pergunta.

## Limites

Este modo privilegia privacidade e disponibilidade, nao qualidade maxima. Um modelo pequeno pode perder nuances, misturar idiomas ou produzir feedback generico. Para respostas mais profundas, usa o modo servidor com Mistral 7B.

O browser SpeechSynthesis pode ler a pergunta, mas o reconhecimento de voz depende do suporte do navegador. No modo `Escuta ativa`, a app continua a ouvir e envia a resposta depois de uma pausa; o botão `Microfone` faz uma gravação explícita para preencher a caixa sem enviar automaticamente. O modelo local nao e usado para Whisper nesta variante.
