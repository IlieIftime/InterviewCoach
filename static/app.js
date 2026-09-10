const state = {
  sessionId: null,
  questions: [],
  current: 0,
  socket: null,
  answerPending: false,
  voice: {
    recognition: null,
    mode: null,
    running: false,
    activeRequested: false,
    explicitRequested: false,
    baseText: '',
    finalText: '',
    interimText: '',
    silenceTimer: null,
    restartTimer: null,
  },
};
const $ = (id) => document.getElementById(id);
const setupView = $('setup-view');
const chatView = $('chat-view');

async function checkStatus() {
  try {
    const status = await fetch('/api/status').then((r) => r.json());
    $('status-label').textContent = status.label;
    $('status-dot').className = `status-dot ${status.available ? 'online' : 'offline'}`;
  } catch (_) {
    $('status-label').textContent = 'API indisponível';
    $('status-dot').className = 'status-dot offline';
  }
}

function renderQuestions() {
  $('question-count').textContent = `${Math.min(state.current + 1, state.questions.length)} / ${state.questions.length}`;
  $('question-list').innerHTML = state.questions.map((question, index) => `
    <button class="question-item ${index === state.current ? 'active' : ''}" type="button" data-index="${index}">
      <span>${String(index + 1).padStart(2, '0')}</span>${escapeHtml(question)}
    </button>`).join('');
}

function addMessage(kind, text) {
  const node = document.createElement('article');
  node.className = `message ${kind}`;
  node.innerHTML = `<div class="message-meta">${kind === 'agent' ? 'NORTHSTAR' : 'TU'}</div><div class="message-text">${escapeHtml(text).replace(/\n/g, '<br>')}</div>`;
  $('messages').appendChild(node);
  $('messages').scrollTop = $('messages').scrollHeight;
}

function updateInsights(result) {
  $('score').textContent = result.score ?? '--';
  $('notes').textContent = result.notes || 'Continua a desenvolver a resposta com exemplos concretos.';
  $('feedback-list').innerHTML = (result.feedback || []).map((item) => `<li>${escapeHtml(item)}</li>`).join('') || '<li>Sem feedback adicional.</li>';
  $('followups').innerHTML = (result.followups || []).map((item) => `<div class="followup">${escapeHtml(item)}</div>`).join('') || '<p>Segue para a próxima pergunta.</p>';
}

function showSession(start) {
  state.sessionId = start.session_id;
  state.questions = [...(JSON.parse($('questions').dataset.questions || '[]'))];
  state.current = 0;
  setupView.classList.add('hidden');
  chatView.classList.remove('hidden');
  $('messages').innerHTML = '';
  addMessage('agent', start.current_question);
  renderQuestions();
  connectSocket();
}

function connectSocket() {
  const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
  state.socket = new WebSocket(`${protocol}://${location.host}/ws/${state.sessionId}`);
  state.socket.onmessage = (event) => {
    const result = JSON.parse(event.data);
    state.answerPending = false;
    $('thinking').classList.add('hidden');
    updateInsights(result);
    if (result.next_question) {
      state.current = Math.min(state.current + 1, state.questions.length - 1);
      addMessage('agent', result.next_question);
      renderQuestions();
    }
    scheduleActiveVoiceRestart();
  };
  state.socket.onerror = () => {
    state.answerPending = false;
    $('thinking').classList.add('hidden');
    setVoiceStatus('Ligação ao coach interrompida.');
  };
}

$('setup-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const questions = $('questions').value.split('\n').map((q) => q.trim()).filter(Boolean);
  $('questions').dataset.questions = JSON.stringify(questions);
  const payload = {
    persona: $('persona').value.trim(),
    instructions: $('instructions').value.trim(),
    avoid: $('avoid').value.trim(),
    candidate_context: $('candidate-context').value.trim(),
    questions,
  };
  const response = await fetch('/start', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) });
  if (!response.ok) return alert('Não foi possível iniciar a sessão.');
  showSession(await response.json());
});

$('answer-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const text = $('answer').value.trim();
  if (!text || state.answerPending || !state.socket || state.socket.readyState !== WebSocket.OPEN) return;
  state.answerPending = true;
  clearTimeout(state.voice.silenceTimer);
  stopVoiceRecognition();
  addMessage('user', text);
  $('answer').value = '';
  $('thinking').classList.remove('hidden');
  state.socket.send(JSON.stringify({ text_answer: text }));
});

$('answer').addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) $('answer-form').requestSubmit();
});

$('read-aloud').addEventListener('click', () => {
  const latest = [...document.querySelectorAll('.message.agent .message-text')].pop();
  if (latest && 'speechSynthesis' in window) speechSynthesis.speak(new SpeechSynthesisUtterance(latest.textContent));
});

function voiceRecognitionConstructor() {
  return window.SpeechRecognition || window.webkitSpeechRecognition;
}

function setVoiceStatus(text) {
  $('voice-status').textContent = text;
}

function updateVoiceControls() {
  const activeButton = $('active-listening');
  const microphoneButton = $('voice-input');
  const voice = state.voice;
  activeButton.classList.toggle('active', voice.activeRequested);
  activeButton.setAttribute('aria-pressed', String(voice.activeRequested));
  activeButton.textContent = voice.activeRequested ? '● A ouvir' : 'Escuta ativa';
  microphoneButton.classList.toggle('recording', voice.explicitRequested);
  microphoneButton.textContent = voice.explicitRequested ? '■ Parar microfone' : '🎤 Microfone';
  microphoneButton.setAttribute('aria-pressed', String(voice.explicitRequested));
  microphoneButton.setAttribute('title', voice.explicitRequested ? 'Parar microfone' : 'Ditar resposta');
  microphoneButton.setAttribute('aria-label', voice.explicitRequested ? 'Parar microfone' : 'Ditar resposta');
}

function clearVoiceTimers() {
  clearTimeout(state.voice.silenceTimer);
  clearTimeout(state.voice.restartTimer);
  state.voice.silenceTimer = null;
  state.voice.restartTimer = null;
}

function renderVoiceTranscript() {
  const voice = state.voice;
  $('answer').value = [voice.baseText, voice.finalText, voice.interimText]
    .map((part) => part.trim())
    .filter(Boolean)
    .join(' ');
}

function stopVoiceRecognition() {
  const voice = state.voice;
  clearTimeout(voice.restartTimer);
  voice.restartTimer = null;
  const recognition = voice.recognition;
  voice.recognition = null;
  voice.running = false;
  if (recognition) {
    try { recognition.stop(); } catch (_) { /* already stopped */ }
  }
}

function scheduleActiveVoiceRestart() {
  const voice = state.voice;
  clearTimeout(voice.restartTimer);
  voice.restartTimer = setTimeout(() => {
    voice.restartTimer = null;
    if (voice.activeRequested && !state.answerPending && $('thinking').classList.contains('hidden') && !voice.running) {
      startVoiceRecognition('active');
    }
  }, 180);
}

function startVoiceRecognition(mode) {
  const SpeechRecognition = voiceRecognitionConstructor();
  if (!SpeechRecognition) {
    $('active-listening').disabled = true;
    $('voice-input').disabled = true;
    setVoiceStatus('Microfone não suportado neste browser.');
    return;
  }
  const voice = state.voice;
  if (voice.running) return;
  const recognition = new SpeechRecognition();
  voice.recognition = recognition;
  voice.mode = mode;
  voice.baseText = $('answer').value.trim();
  voice.finalText = '';
  voice.interimText = '';
  recognition.lang = 'pt-PT';
  recognition.continuous = mode === 'active';
  recognition.interimResults = true;
  recognition.maxAlternatives = 1;
  recognition.onstart = () => {
    if (voice.recognition !== recognition) return;
    voice.running = true;
    updateVoiceControls();
    setVoiceStatus(mode === 'active' ? 'A ouvir. Uma pausa envia a resposta.' : 'A gravar. Clica no microfone para terminar.');
  };
  recognition.onend = () => {
    if (voice.recognition !== recognition) return;
    voice.running = false;
    if (mode === 'active' && voice.activeRequested && !state.answerPending && $('thinking').classList.contains('hidden')) {
      scheduleActiveVoiceRestart();
      return;
    }
    if (mode === 'explicit') voice.explicitRequested = false;
    updateVoiceControls();
    if (!voice.activeRequested && !voice.explicitRequested) setVoiceStatus('Microfone desligado');
  };
  recognition.onerror = (event) => {
    if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {
      voice.activeRequested = false;
      voice.explicitRequested = false;
      clearVoiceTimers();
      updateVoiceControls();
      setVoiceStatus('Permissão do microfone recusada. Podes escrever.');
    } else if (event.error !== 'aborted') {
      setVoiceStatus('Microfone indisponível. Podes escrever.');
    }
  };
  recognition.onresult = (event) => {
    let finalText = '';
    let interimText = '';
    for (let index = event.resultIndex; index < event.results.length; index += 1) {
      if (event.results[index].isFinal) finalText += event.results[index][0].transcript;
      else interimText += event.results[index][0].transcript;
    }
    voice.finalText += finalText;
    voice.interimText = interimText;
    renderVoiceTranscript();
    clearTimeout(voice.silenceTimer);
    if (mode === 'active' && finalText.trim()) {
      voice.silenceTimer = setTimeout(() => $('answer-form').requestSubmit(), 1500);
    }
  };
  try {
    recognition.start();
  } catch (_) {
    voice.recognition = null;
    voice.running = false;
    voice.activeRequested = false;
    voice.explicitRequested = false;
    updateVoiceControls();
    setVoiceStatus('Não foi possível iniciar o microfone.');
  }
}

function setupVoiceControls() {
  if (!voiceRecognitionConstructor()) {
    $('active-listening').disabled = true;
    $('voice-input').disabled = true;
    setVoiceStatus('Microfone não suportado neste browser.');
    return;
  }
  $('active-listening').addEventListener('click', () => {
    const voice = state.voice;
    clearTimeout(voice.silenceTimer);
    if (voice.activeRequested) {
      voice.activeRequested = false;
      stopVoiceRecognition();
      updateVoiceControls();
      if (!voice.explicitRequested) setVoiceStatus('Microfone desligado');
      return;
    }
    voice.activeRequested = true;
    voice.explicitRequested = false;
    stopVoiceRecognition();
    updateVoiceControls();
    startVoiceRecognition('active');
  });
  $('voice-input').addEventListener('click', () => {
    const voice = state.voice;
    clearTimeout(voice.silenceTimer);
    if (voice.explicitRequested) {
      voice.explicitRequested = false;
      stopVoiceRecognition();
      updateVoiceControls();
      if (!voice.activeRequested) setVoiceStatus('Microfone desligado');
      return;
    }
    voice.activeRequested = false;
    voice.explicitRequested = true;
    stopVoiceRecognition();
    updateVoiceControls();
    startVoiceRecognition('explicit');
  });
}

setupVoiceControls();

async function endSession() {
  state.voice.activeRequested = false;
  state.voice.explicitRequested = false;
  state.answerPending = false;
  clearVoiceTimers();
  stopVoiceRecognition();
  if (state.sessionId) await fetch('/stop', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({session_id: state.sessionId}) });
  if (state.socket) state.socket.close();
  state.socket = null;
  state.sessionId = null;
  updateVoiceControls();
  setVoiceStatus('Microfone desligado');
  chatView.classList.add('hidden');
  setupView.classList.remove('hidden');
}
$('end-session').addEventListener('click', endSession);
$('new-session').addEventListener('click', endSession);

function escapeHtml(value) { return String(value).replace(/[&<>"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[char])); }
checkStatus();
