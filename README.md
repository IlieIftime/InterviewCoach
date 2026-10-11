# Browser Local

## Iteracao de performance e integridade

`inference.worker.js` executa a inferencia fora da thread principal. Assim, o download, o WebGPU/WASM e a geracao nao bloqueiam os controlos de voz nem a renderizacao da conversa. O service worker guarda o shell, o worker e o manifesto; os pesos continuam a ser guardados pelo cache do Transformers.js.

O `model-manifest.json` identifica a versao do modelo e do runtime. Quando forem disponibilizados shards locais em `browser-local/model-artifacts`, gerar hashes SHA-256 com:

```powershell
..\scripts\build_browser_manifest.ps1
```

O browser local e um complemento: para qualidade de conversa superior, usar o servidor local com Ollama, Whisper e Piper. Uma página GitHub Pages não pode chamar automaticamente `http://127.0.0.1:8000` a partir de outro dispositivo; para esse caso publicar o modo browser ou servir a API através de uma origem HTTPS controlada.

Variante do coach que corre diretamente no browser, sem FastAPI, Ollama ou llama-server. Depois da instalação inicial, funciona como uma PWA independente do PC que serviu os ficheiros.

## Arrancar

A partir desta pasta:

```powershell
cd C:\Users\iliei\Desktop\EntrevistadorAPX\Mistral-7B-GGUF\browser-local
py -m http.server 4173
```

Abrir `http://127.0.0.1:4173` e instalar a aplicação pelo menu do browser (`Instalar The Hand of Zeus`). Num dispositivo externo, serve esta pasta por HTTPS ou por um hosting estático e abre o URL nesse dispositivo.

## Publicar no GitHub Pages

O workflow em `../.github/workflows/deploy-pages.yml` publica automaticamente esta pasta na branch `gh-pages`.

No PowerShell, a partir da raiz do projeto:

```powershell
cd C:\Users\iliei\Desktop\EntrevistadorAPX\Mistral-7B-GGUF
git init
git add .
git commit -m "Add browser-local interview coach"
git branch -M main
git remote add origin https://github.com/UTILIZADOR/NOME-DO-REPOSITORIO.git
git push -u origin main
```

No GitHub, abrir `Settings > Pages`, escolher `Deploy from a branch`, selecionar a branch `gh-pages` e a pasta `/ (root)`, e carregar em `Save`. Depois do workflow terminar, o URL será:

```text
https://UTILIZADOR.github.io/NOME-DO-REPOSITORIO/
```

Abrir esse URL no dispositivo X, clicar em `Preparar modelo local`, instalar a PWA e esperar o download terminar. A partir daí, o dispositivo X funciona sem o PC e sem o terminal.

Nao abrir com `file://`: ES modules, WebGPU e o download do modelo precisam de HTTP/HTTPS.

## Modelo e cache

O frontend usa `onnx-community/Qwen2.5-0.5B-Instruct-ONNX` atraves de Transformers.js. O browser descarrega os artefactos quantizados na primeira sessao e usa `env.useBrowserCache = true` nas seguintes. O dispositivo tenta WebGPU e cai para WASM.

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
