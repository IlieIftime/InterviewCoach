import { pipeline, env } from 'https://cdn.jsdelivr.net/npm/@huggingface/transformers@3.7.2';

const MODEL_ID = 'onnx-community/Qwen2.5-0.5B-Instruct-ONNX';
env.allowLocalModels = false;
env.useBrowserCache = true;
const state = {
  generator: null,
  config: null,
  questions: [],
  index: 0,
  turns: [],
  summary: '',
  recognition: null,
  recognitionMode: null,
  recognitionRunning: false,
  activeListening: false,
  explicitListening: false,
  recognitionBaseText: '',
  finalTranscript: '',
  interimTranscript: '',
  silenceTimer: null,
  restartTimer: null,
  submitting: false,
};
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

function setVoiceStatus(text) {
  $('listen-status').textContent = text;
}

function updateVoiceButtons() {
  const activeButton = $('listen');
  const microphoneButton = $('microphone');
  activeButton.classList.toggle('active', state.activeListening);
  activeButton.setAttribute('aria-pressed', String(state.activeListening));
  activeButton.textContent = state.activeListening ? '● A ouvir' : '🎙 Escuta ativa';
  microphoneButton.classList.toggle('active', state.explicitListening);
  microphoneButton.setAttribute('aria-pressed', String(state.explicitListening));
  microphoneButton.textContent = state.explicitListening ? '■ Parar microfone' : '🎤 Microfone';
}

function clearVoiceTimers() {
  clearTimeout(state.silenceTimer);
  clearTimeout(state.restartTimer);
  state.silenceTimer = null;
  state.restartTimer = null;
}

function renderTranscript() {
  const text = [state.recognitionBaseText, state.finalTranscript, state.interimTranscript]
    .map((part) => part.trim())
    .filter(Boolean)
    .join(' ');
  $('answer').value = text;
}

function scheduleActiveRestart() {
  clearTimeout(state.restartTimer);
  state.restartTimer = setTimeout(() => {
    state.restartTimer = null;
    if (state.activeListening && !state.submitting && $('busy').classList.contains('hidden') && !state.recognitionRunning) {
      startRecognition('active');
    }
  }, 180);
}

function stopRecognition() {
  clearTimeout(state.restartTimer);
  state.restartTimer = null;
  const recognition = state.recognition;
  state.recognition = null;
  state.recognitionRunning = false;
  if (!recognition) return;
  try { recognition.stop(); } catch (_) { /* already stopped */ }
}

function startRecognition(mode) {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Recognition) {
    $('listen').disabled = true;
    $('microphone').disabled = true;
    setVoiceStatus('Microfone não suportado neste browser.');
    return;
  }

  if (state.recognitionRunning) return;
  const recognition = new Recognition();
  recognition.lang = 'pt-PT';
  recognition.continuous = mode === 'active';
  recognition.interimResults = true;
  recognition.maxAlternatives = 1;
  state.recognition = recognition;
  state.recognitionMode = mode;
  state.recognitionBaseText = $('answer').value.trim();
  state.finalTranscript = '';
  state.interimTranscript = '';

  recognition.onstart = () => {
    state.recognitionRunning = true;
    updateVoiceButtons();
    setVoiceStatus(mode === 'active' ? 'A ouvir. Uma pausa envia a resposta.' : 'A gravar. Clica novamente para terminar.');
  };
  recognition.onend = () => {
    if (state.recognition !== recognition) return;
    state.recognitionRunning = false;
    if (mode === 'active' && state.activeListening && !state.submitting && $('busy').classList.contains('hidden')) {
      scheduleActiveRestart();
      return;
    }
    if (mode === 'explicit') state.explicitListening = false;
    updateVoiceButtons();
    if (!state.activeListening && !state.explicitListening) setVoiceStatus('Microfone desligado');
  };
  recognition.onerror = (event) => {
    if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {
      state.activeListening = false;
      state.explicitListening = false;
      clearVoiceTimers();
      updateVoiceButtons();
      setVoiceStatus('Permissão do microfone recusada. Podes escrever.');
      return;
    }
    if (event.error !== 'aborted') setVoiceStatus('Microfone indisponível. Podes escrever.');
  };
  recognition.onresult = (event) => {
    let finalText = '';
    let interimText = '';
    for (let index = event.resultIndex; index < event.results.length; index += 1) {
      if (event.results[index].isFinal) finalText += event.results[index][0].transcript;
      else interimText += event.results[index][0].transcript;
    }
    state.finalTranscript += finalText;
    state.interimTranscript = interimText;
    renderTranscript();
    clearTimeout(state.silenceTimer);
    if (mode === 'active' && finalText.trim()) {
      state.silenceTimer = setTimeout(() => $('answer-form').requestSubmit(), 1500);
    }
  };
  try {
    recognition.start();
  } catch (_) {
    state.recognitionRunning = false;
    state.activeListening = false;
    state.explicitListening = false;
    updateVoiceButtons();
    setVoiceStatus('Não foi possível iniciar o microfone.');
  }
}

function setupVoiceControls() {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Recognition) {
    $('listen').disabled = true;
    $('microphone').disabled = true;
    setVoiceStatus('Microfone não suportado neste browser.');
    return;
  }

  $('listen').addEventListener('click', () => {
    clearTimeout(state.silenceTimer);
    if (state.activeListening) {
      state.activeListening = false;
      stopRecognition();
      updateVoiceButtons();
      if (!state.explicitListening) setVoiceStatus('Microfone desligado');
      return;
    }
    state.activeListening = true;
    state.explicitListening = false;
    stopRecognition();
    updateVoiceButtons();
    startRecognition('active');
  });

  $('microphone').addEventListener('click', () => {
    clearTimeout(state.silenceTimer);
    if (state.explicitListening) {
      state.explicitListening = false;
      stopRecognition();
      updateVoiceButtons();
      if (!state.activeListening) setVoiceStatus('Microfone desligado');
      return;
    }
    state.activeListening = false;
    state.explicitListening = true;
    stopRecognition();
    updateVoiceButtons();
    startRecognition('explicit');
  });
}

setupVoiceControls();

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
  if (!answer || $('busy').classList.contains('hidden') === false || state.submitting) return;
  clearTimeout(state.silenceTimer);
  state.submitting = true;
  stopRecognition();
  addMessage('user', answer); $('answer').value = ''; $('busy').classList.remove('hidden');
  const instant = localRubric(answer);
  renderReview({ score: instant.score, rationale: `${instant.score}/5: avaliação inicial baseada na estrutura da resposta.`, feedback: instant.feedback, next: state.questions[Math.min(state.index + 1, state.questions.length - 1)] || 'Que perguntas tens para mim?' });
  try {
    const result = await coachAnswer(answer);
    state.turns.push({ question: state.questions[state.index], answer });
    renderReview(result);
    state.index = Math.min(state.index + 1, state.questions.length - 1); addMessage('agent', result.next); renderSteps();
  } catch (error) {
    renderReview({ score: instant.score, rationale: 'A resposta foi guardada, mas o modelo não conseguiu gerar feedback.', feedback: instant.feedback, next: state.questions[Math.min(state.index + 1, state.questions.length - 1)] || 'Que perguntas tens para mim?' });
    setVoiceStatus(`Erro do modelo: ${error.message || 'tenta novamente.'}`);
  } finally {
    state.submitting = false;
    $('busy').classList.add('hidden');
    if (state.activeListening) scheduleActiveRestart();
  }
});
$('answer').addEventListener('keydown', (event) => { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) $('answer-form').requestSubmit(); });
$('speak').addEventListener('click', () => { const text = [...document.querySelectorAll('.message.agent .text')].pop()?.textContent; if (text && speechSynthesis) speechSynthesis.speak(new SpeechSynthesisUtterance(text)); });
$('reset').addEventListener('click', () => location.reload());
