import { pipeline, env } from 'https://cdn.jsdelivr.net/npm/@huggingface/transformers@3.7.2';

const MODEL_ID = 'onnx-community/Qwen2.5-0.5B-Instruct-ONNX';
env.allowLocalModels = false;
env.useBrowserCache = true;
const state = { generator: null, config: null, questions: [], index: 0, turns: [], summary: '', recognition: null, listeningRequested: false, silenceTimer: null };
const $ = (id) => document.getElementById(id);
const setup = $('setup');
const coach = $('coach');

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('./sw.js').catch(() => {});
}

function setProgress(value, label) {
  $('progress').style.width = `${value}%`;
  $('download-label').textContent = label;
}

async function loadModel() {
  if (state.generator) return true;
  $('runtime').textContent = 'A descarregar modelo local';
  try {
    if (navigator.storage?.persist) await navigator.storage.persist();
    const cached = localStorage.getItem(`northstar-model:${MODEL_ID}`) === 'ready';
    setProgress(cached ? 35 : 8, cached ? 'A reutilizar o modelo guardado neste browser...' : 'A preparar o modelo quantizado...');
    const device = navigator.gpu ? 'webgpu' : 'wasm';
    state.generator = await pipeline('text-generation', MODEL_ID, {
      device,
      dtype: device === 'webgpu' ? 'q4' : 'q8',
      progress_callback: (event) => {
        if (event.status === 'progress') setProgress(Math.max(8, Math.round(event.progress)), `A preparar modelo local: ${Math.round(event.progress)}%`);
        if (event.status === 'done') setProgress(92, 'Modelo em cache. A iniciar sessão...');
      },
    });
    setProgress(100, device === 'webgpu' ? 'Modelo pronto com WebGPU.' : 'Modelo pronto com WASM.');
    localStorage.setItem(`northstar-model:${MODEL_ID}`, 'ready');
    $('runtime').textContent = device === 'webgpu' ? 'Modelo local · WebGPU' : 'Modelo local · WASM';
    $('runtime').className = 'runtime ready';
    $('setup-form').querySelector('button').textContent = 'Modelo pronto';
    return true;
  } catch (error) {
    $('runtime').textContent = 'Motor local indisponível';
    $('runtime').className = 'runtime error';
    setProgress(0, `Não foi possível carregar o modelo: ${error.message}`);
    return false;
  }
}

function compact(text, max = 480) { return text.length > max ? `${text.slice(0, max)}...` : text; }
function promptForAnswer(answer) {
  const recent = state.turns.slice(-3).map((turn) => `Pergunta: ${turn.question}\nResposta: ${turn.answer}`).join('\n');
  return `<s>[INST] És um coach de entrevistas. Responde em português de Portugal, com frases curtas e humanas.\n\nPersona: ${state.config.persona}\nInstruções: ${state.config.instructions}\nEvitar: ${state.config.avoid}\nContexto: ${compact(state.config.context, 700)}\n\nHistórico recente:\n${compact(recent, 900)}\n\nPergunta atual: ${state.questions[state.index]}\nResposta do candidato: ${answer}\n\nDá feedback em uma frase, sugere uma melhoria concreta, faz uma pergunta de follow-up curta e termina com NEXT: seguido da próxima pergunta. Não uses JSON nem listas longas. [/INST]`;
}

function localRubric(answer) {
  const lower = answer.toLowerCase();
  const signals = {
    example: /(projeto|situação|quando|constru|implemente|trabalh)/.test(lower),
    action: /(fiz|desenhei|implementei|usei|criei|decidi|resolvi)/.test(lower),
    result: /(resultado|impacto|melhor|reduz|aument|auc|%|zero|milisseg|clientes)/.test(lower),
    reflection: /(aprendi|mudaria|trade.?off|limitação|falha|próximo)/.test(lower),
  };
  const points = Object.values(signals).filter(Boolean).length;
  const feedback = [];
  if (!signals.example) feedback.push('Situa a resposta com um exemplo específico.');
  if (!signals.action) feedback.push('Explica claramente o que tu fizeste.');
  if (!signals.result) feedback.push('Acrescenta um resultado mensurável ou impacto.');
  if (!signals.reflection) feedback.push('Fecha com uma aprendizagem ou trade-off.');
  return { score: Math.min(5, Math.max(1, points + 1)), feedback: feedback.slice(0, 2), signals };
}

function cleanModelText(raw) {
  const text = raw.replace(/^\s*(assistant|resposta)\s*:?/i, '').trim();
  const nextMarker = text.search(/\bNEXT\s*:/i);
  const body = nextMarker >= 0 ? text.slice(0, nextMarker).trim() : text;
  const next = nextMarker >= 0 ? text.slice(nextMarker).replace(/^NEXT\s*:/i, '').trim() : '';
  const candidateQuestion = (next.match(/[^?]*\?/) || [])[0] || '';
  const safeBody = body.length <= 220 && !/questionário|questionnaire|equipe|tecnica/i.test(body) ? body : '';
  const safeNext = candidateQuestion.length <= 220 ? candidateQuestion.trim() : '';
  return { body: safeBody, next: safeNext };
}

async function coachAnswer(answer) {
  const rubric = localRubric(answer);
  let generated = '';
  if (state.generator) {
    const output = await state.generator(promptForAnswer(answer), { max_new_tokens: 64, temperature: 0.35, do_sample: true, return_full_text: false });
    generated = Array.isArray(output) ? output[0].generated_text : output.generated_text;
  }
  const cleaned = cleanModelText(generated || '');
  const modelFeedback = cleaned.body ? [cleaned.body] : [];
  return {
    score: rubric.score,
    feedback: [...rubric.feedback, ...modelFeedback].slice(0, 3),
    next: cleaned.next || state.questions[Math.min(state.index + 1, state.questions.length - 1)] || 'Que perguntas tens para mim?',
    rationale: `${rubric.score}/5: ${rubric.signals.result ? 'há evidência de impacto' : 'falta evidência de impacto'}.`,
  };
}

function addMessage(kind, text) {
  const article = document.createElement('article'); article.className = `message ${kind}`;
  article.innerHTML = `<div class="meta">${kind === 'agent' ? 'NORTHSTAR LOCAL' : 'TU'}</div><div class="text">${escapeHtml(text).replace(/\n/g, '<br>')}</div>`;
  $('messages').appendChild(article); $('messages').scrollTop = $('messages').scrollHeight;
}
function renderSteps() { $('steps').innerHTML = state.questions.map((q, i) => `<div class="step ${i === state.index ? 'active' : ''}"><small>${String(i + 1).padStart(2, '0')}</small>${escapeHtml(q)}</div>`).join(''); }
function escapeHtml(value) { return String(value).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c])); }
function renderReview(result) {
  $('score').textContent = result.score;
  $('rationale').textContent = result.rationale;
  $('feedback').innerHTML = result.feedback.map((item) => `<li>${escapeHtml(item)}</li>`).join('');
  $('followup').textContent = result.next;
}

function setupActiveListening() {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const button = $('listen');
  if (!Recognition) {
    button.disabled = true;
    button.textContent = 'Microfone não suportado';
    return;
  }
  const recognition = new Recognition();
  recognition.lang = 'pt-PT';
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.onstart = () => {
    button.classList.add('active');
    button.textContent = '● A ouvir';
    $('listen-status').textContent = 'A ouvir. Faz uma pausa quando terminares.';
  };
  recognition.onend = () => {
    if (state.listeningRequested && $('busy').classList.contains('hidden')) {
      try { recognition.start(); } catch (_) { }
    } else {
      button.classList.remove('active');
      button.textContent = '🎙 Escuta ativa';
      $('listen-status').textContent = 'Microfone desligado';
    }
  };
  recognition.onerror = () => { $('listen-status').textContent = 'Microfone indisponível. Podes escrever.'; };
  recognition.onresult = (event) => {
    let transcript = '';
    for (let index = event.resultIndex; index < event.results.length; index += 1) transcript += event.results[index][0].transcript;
    $('answer').value = `${$('answer').value} ${transcript}`.trim();
    clearTimeout(state.silenceTimer);
    if (event.results[event.results.length - 1].isFinal) state.silenceTimer = setTimeout(() => $('answer-form').requestSubmit(), 1500);
  };
  state.recognition = recognition;
  button.addEventListener('click', () => {
    state.listeningRequested = !state.listeningRequested;
    if (state.listeningRequested) {
      try { recognition.start(); } catch (_) { }
    } else recognition.stop();
  });
}

setupActiveListening();

$('setup-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  state.config = { persona: $('persona').value.trim(), instructions: $('instructions').value.trim(), avoid: $('avoid').value.trim(), context: $('context').value.trim() };
  state.questions = $('questions').value.split('\n').map((q) => q.trim()).filter(Boolean);
  if (!state.questions.length) return;
  const button = $('setup-form').querySelector('button');
  button.disabled = true;
  button.textContent = 'A preparar modelo...';
  try {
    if (!(await loadModel())) return;
    state.index = 0; state.turns = [];
    setup.classList.add('hidden'); coach.classList.remove('hidden');
    addMessage('agent', state.questions[0]); renderSteps();
  } finally {
    button.disabled = false;
    if (!coach.classList.contains('hidden')) button.textContent = 'Modelo pronto';
    else if (!state.generator) button.textContent = 'Tentar novamente';
  }
});

$('answer-form').addEventListener('submit', async (event) => {
  event.preventDefault(); const answer = $('answer').value.trim();
  if (!answer || $('busy').classList.contains('hidden') === false) return;
  clearTimeout(state.silenceTimer);
  if (state.listeningRequested) try { state.recognition?.stop(); } catch (_) { }
  addMessage('user', answer); $('answer').value = ''; $('busy').classList.remove('hidden');
  const instant = localRubric(answer);
  renderReview({ score: instant.score, rationale: `${instant.score}/5: avaliação inicial baseada na estrutura da resposta.`, feedback: instant.feedback, next: state.questions[Math.min(state.index + 1, state.questions.length - 1)] || 'Que perguntas tens para mim?' });
  const result = await coachAnswer(answer);
  state.turns.push({ question: state.questions[state.index], answer });
  $('busy').classList.add('hidden'); renderReview(result);
  state.index = Math.min(state.index + 1, state.questions.length - 1); addMessage('agent', result.next); renderSteps();
  if (state.listeningRequested) try { state.recognition?.start(); } catch (_) { }
});
$('answer').addEventListener('keydown', (event) => { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) $('answer-form').requestSubmit(); });
$('speak').addEventListener('click', () => { const text = [...document.querySelectorAll('.message.agent .text')].pop()?.textContent; if (text && speechSynthesis) speechSynthesis.speak(new SpeechSynthesisUtterance(text)); });
$('reset').addEventListener('click', () => location.reload());
