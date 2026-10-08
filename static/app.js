const LANGUAGE_RECOGNITION_TAG = { 'pt-PT': 'pt-PT', en: 'en-US' };
const LANGUAGE_SPEECH_TAG = { 'pt-PT': 'pt-pt', en: 'en-us' };

const state = {
  sessionId: null,
  questions: [],
  current: 0,
  socket: null,
  answerPending: false,
  language: 'pt-PT',
  durationMinutes: 30,
  minPauseSeconds: 0.8,
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
    maxListenTimer: null,
    speechEnabled: true,
    speaking: false,
    ttsAudio: null,
    ttsUrl: '',
    ttsRequestId: 0,
    localRecorder: null,
    localStream: null,
    localChunks: [],
    localMimeType: '',
    localCancel: false,
    localSubmit: false,
    localProcessing: false,
    localAnalyser: null,
    localAudioContext: null,
    localFrame: null,
    localStartedAt: 0,
    localLastVoiceAt: 0,
    localHadSpeech: false,
    sttMode: localStorage.getItem('apx-stt-mode-v2') || 'browser',
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

async function loadModelSelector() {
  const select = $('model-select');
  if (!select) return;
  try {
    const data = await fetch('/api/models').then((r) => r.json());
    select.innerHTML = data.models.map((model) => `<option value="${model.id}">${escapeHtml(model.label)}</option>`).join('');
    select.value = data.current;
  } catch (_) {
    select.innerHTML = '<option>Modelo indisponível</option>';
  }
}

$('model-select')?.addEventListener('change', async (event) => {
  try {
    await fetch('/api/model', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({model: event.target.value}),
    });
    checkStatus();
  } catch (_) { /* mantém a seleção anterior visível */ }
});

$('script-fidelity')?.addEventListener('input', (event) => {
  $('fidelity-value').textContent = `${event.target.value}%`;
});

// Tempo máximo de escuta dinâmico: proporcional ao comprimento esperado da resposta
// (ex.: pergunta curta ~50 palavras/30s, pergunta aberta ~100 palavras/60s), entre 15s e 90s.
function estimateMaxListenMs(questionText) {
  const words = (questionText || '').trim().split(/\s+/).filter(Boolean).length;
  const expectedWords = Math.max(40, Math.min(160, words * 6));
  const ms = (expectedWords / 100) * 60000;
  return Math.max(15000, Math.min(90000, ms));
}

function currentQuestionText() {
  const nodes = document.querySelectorAll('.message.agent .message-text');
  return nodes.length ? nodes[nodes.length - 1].textContent : '';
}

// Duração de silêncio curto (fim de resposta) configurável entre 800-1200ms via
// min_pause_seconds da sessão.
function shortSilenceMs() {
  return Math.max(800, Math.min(1200, Math.round((state.minPauseSeconds || 0.8) * 1000)));
}

function clearMaxListenTimer() {
  clearTimeout(state.voice.maxListenTimer);
  state.voice.maxListenTimer = null;
}

function scheduleMaxListenTimeout(onTimeout) {
  clearMaxListenTimer();
  const ms = estimateMaxListenMs(currentQuestionText());
  state.voice.maxListenTimer = setTimeout(onTimeout, ms);
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
  node.innerHTML = `<div class="message-meta">${kind === 'agent' ? 'ENTREVISTADOR APX' : 'TU'}</div><div class="message-text">${escapeHtml(text).replace(/\n/g, '<br>')}</div>`;
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
  state.language = start.language || 'pt-PT';
  state.durationMinutes = start.duration_minutes || 30;
  state.minPauseSeconds = start.min_pause_seconds || 0.8;
  setupView.classList.add('hidden');
  chatView.classList.remove('hidden');
  $('messages').innerHTML = '';
  addMessage('agent', start.current_question);
  speakAgent(start.current_question);
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
      if (!result.is_followup) state.current = Math.min(state.current + 1, state.questions.length - 1);
      addMessage('agent', result.next_question);
      speakAgent(result.next_question);
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
  const linesOf = (id) => $(id).value.split('\n').map((line) => line.trim()).filter(Boolean);
  const payload = {
    persona: $('persona').value.trim(),
    instructions: $('instructions').value.trim(),
    avoid: $('avoid').value.trim(),
    candidate_context: $('candidate-context').value.trim(),
    questions,
    language: $('language').value,
    must: linesOf('must'),
    dont: linesOf('avoid'),
    questions_to_avoid: linesOf('questions-avoid'),
    script_fidelity: Number($('script-fidelity').value) / 100,
    duration_minutes: Number($('duration-minutes').value) || 30,
    min_pause_seconds: Number($('min-pause').value) || 0.8,
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
  cancelAgentSpeech();
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
  if (latest) speakAgent(latest.textContent);
});

async function speakAgent(text) {
  if (!state.voice.speechEnabled || !text) return;
  cancelAgentSpeech();
  const voice = state.voice;
  const requestId = ++voice.ttsRequestId;
  voice.speaking = true;
  try {
    const response = await fetch('/speak', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text, language: state.language}),
    });
    if (!response.ok) throw new Error('Piper indisponível');
    const audioUrl = URL.createObjectURL(await response.blob());
    if (requestId !== voice.ttsRequestId) {
      URL.revokeObjectURL(audioUrl);
      return;
    }
    const audio = new Audio(audioUrl);
    voice.ttsAudio = audio;
    voice.ttsUrl = audioUrl;
    const finishAudio = () => {
      if (voice.ttsAudio !== audio) return;
      voice.ttsAudio = null;
      voice.ttsUrl = '';
      URL.revokeObjectURL(audioUrl);
      voice.speaking = false;
      if (voice.activeRequested && !state.answerPending) scheduleActiveVoiceRestart();
    };
    audio.onended = finishAudio;
    audio.onerror = finishAudio;
    await audio.play();
  } catch (_) {
    if (requestId === voice.ttsRequestId) speakWithBrowser(text);
  }
}

function speakWithBrowser(text) {
  if (!('speechSynthesis' in window)) {
    state.voice.speaking = false;
    return;
  }
  speechSynthesis.cancel();
  const utterance = new SpeechSynthesisUtterance(text);
  const langTag = LANGUAGE_RECOGNITION_TAG[state.language] || 'pt-PT';
  const langPrefix = LANGUAGE_SPEECH_TAG[state.language] || 'pt-pt';
  const langFamily = langPrefix.split('-')[0];
  utterance.lang = langTag;
  utterance.rate = 0.96;
  utterance.pitch = 1;
  const voices = speechSynthesis.getVoices();
  utterance.voice = voices.find((voice) => voice.lang.toLowerCase() === langPrefix)
    || voices.find((voice) => voice.lang.toLowerCase().startsWith(langFamily))
    || null;
  const finishSpeech = () => {
    state.voice.speaking = false;
    if (state.voice.activeRequested && !state.answerPending) scheduleActiveVoiceRestart();
  };
  utterance.onend = finishSpeech;
  utterance.onerror = finishSpeech;
  speechSynthesis.speak(utterance);
}

function cancelAgentSpeech() {
  if ('speechSynthesis' in window) speechSynthesis.cancel();
  const voice = state.voice;
  voice.ttsRequestId += 1;
  if (voice.ttsAudio) {
    voice.ttsAudio.pause();
    voice.ttsAudio.currentTime = 0;
    voice.ttsAudio = null;
  }
  if (voice.ttsUrl) URL.revokeObjectURL(voice.ttsUrl);
  voice.ttsUrl = '';
  voice.speaking = false;
}

function voiceRecognitionConstructor() {
  return window.SpeechRecognition || window.webkitSpeechRecognition;
}

function setVoiceStatus(text) {
  $('voice-status').textContent = text;
}

function updateVoiceControls() {
  const activeButton = $('active-listening');
  const microphoneButton = $('voice-input');
  const sttButton = $('stt-mode');
  const voice = state.voice;
  activeButton.classList.toggle('active', voice.activeRequested);
  activeButton.setAttribute('aria-pressed', String(voice.activeRequested));
  activeButton.textContent = voice.activeRequested ? '● A ouvir' : 'Escuta ativa';
  microphoneButton.classList.toggle('recording', voice.explicitRequested);
  microphoneButton.textContent = voice.explicitRequested ? '■ Parar microfone' : '🎤 Microfone';
  microphoneButton.setAttribute('aria-pressed', String(voice.explicitRequested));
  microphoneButton.setAttribute('title', voice.explicitRequested ? 'Parar microfone' : 'Ditar resposta');
  microphoneButton.setAttribute('aria-label', voice.explicitRequested ? 'Parar microfone' : 'Ditar resposta');
  if (sttButton) {
    const mode = selectedSttMode();
    sttButton.textContent = mode === 'browser' ? 'STT browser' : 'STT local';
    sttButton.classList.toggle('local', mode === 'local');
  }
}

function clearVoiceTimers() {
  clearTimeout(state.voice.silenceTimer);
  clearTimeout(state.voice.restartTimer);
  state.voice.silenceTimer = null;
  state.voice.restartTimer = null;
  clearMaxListenTimer();
}

function renderVoiceTranscript() {
  const voice = state.voice;
  $('answer').value = [voice.baseText, voice.finalText, voice.interimText]
    .map((part) => part.trim())
    .filter(Boolean)
    .join(' ');
}

function localVoiceSupported() {
  return Boolean(navigator.mediaDevices?.getUserMedia && window.MediaRecorder);
}

function selectedSttMode() {
  if (state.voice.sttMode === 'browser' && voiceRecognitionConstructor()) return 'browser';
  if (state.voice.sttMode === 'local' && localVoiceSupported()) return 'local';
  return localVoiceSupported() ? 'local' : 'browser';
}

function localVoiceMimeType() {
  const candidates = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus'];
  return candidates.find((type) => MediaRecorder.isTypeSupported(type)) || '';
}

function stopLocalAudioMonitor() {
  const voice = state.voice;
  if (voice.localFrame) cancelAnimationFrame(voice.localFrame);
  voice.localFrame = null;
  voice.localAnalyser = null;
  if (voice.localAudioContext) {
    voice.localAudioContext.close().catch(() => {});
    voice.localAudioContext = null;
  }
}

function monitorLocalSilence() {
  const voice = state.voice;
  if (!voice.localRecorder || voice.mode !== 'active') return;
  const analyser = voice.localAnalyser;
  if (analyser) {
    const data = new Uint8Array(analyser.fftSize);
    analyser.getByteTimeDomainData(data);
    const rms = Math.sqrt(data.reduce((sum, value) => sum + ((value - 128) / 128) ** 2, 0) / data.length);
    if (rms > 0.035) {
      voice.localHadSpeech = true;
      voice.localLastVoiceAt = Date.now();
    }
    const elapsed = Date.now() - voice.localStartedAt;
    const silenceThreshold = shortSilenceMs();
    if (voice.localHadSpeech && elapsed > Math.min(900, silenceThreshold) && Date.now() - voice.localLastVoiceAt > silenceThreshold) {
      finishLocalRecording(true);
      return;
    }
  }
  voice.localFrame = requestAnimationFrame(monitorLocalSilence);
}

function bytesToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = '';
  const chunkSize = 0x8000;
  for (let index = 0; index < bytes.length; index += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(index, index + chunkSize));
  }
  return btoa(binary);
}

async function submitLocalAudio(blob, mimeType) {
  const voice = state.voice;
  voice.localProcessing = true;
  setVoiceStatus('A transcrever localmente...');
  try {
    const format = mimeType.includes('ogg') ? 'ogg' : 'webm';
    const response = await fetch('/transcribe', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({audio_base64: bytesToBase64(await blob.arrayBuffer()), audio_format: format}),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || 'STT local indisponível.');
    voice.finalText = String(result.text || '').trim();
    voice.interimText = '';
    renderVoiceTranscript();
    if (!voice.finalText) {
      setVoiceStatus('Não ouvi uma resposta. Podes repetir.');
      if (voice.activeRequested) scheduleActiveVoiceRestart();
      return;
    }
    if (result.low_confidence) {
      setVoiceStatus('Não tenho a certeza que percebi bem. Confirma ou repete antes de enviar.');
      voice.explicitRequested = false;
      updateVoiceControls();
      return;
    }
    if (voice.mode === 'active') {
      $('answer-form').requestSubmit();
    } else {
      voice.explicitRequested = false;
      updateVoiceControls();
      setVoiceStatus('Transcrição pronta. Confirma com Enviar.');
    }
  } catch (error) {
    setVoiceStatus(`STT local indisponível: ${error.message || 'tenta escrever.'}`);
    if (voice.activeRequested) scheduleActiveVoiceRestart();
  } finally {
    voice.localProcessing = false;
  }
}

function finishLocalRecording(submitAudio) {
  const voice = state.voice;
  const recorder = voice.localRecorder;
  if (!recorder || recorder.state === 'inactive') return;
  voice.localCancel = !submitAudio;
  voice.localSubmit = submitAudio;
  try { recorder.stop(); } catch (_) { /* already stopped */ }
}

async function startLocalRecognition(mode) {
  const voice = state.voice;
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true},
    });
    const mimeType = localVoiceMimeType();
    const recorder = mimeType ? new MediaRecorder(stream, {mimeType}) : new MediaRecorder(stream);
    voice.localRecorder = recorder;
    voice.localStream = stream;
    voice.localChunks = [];
    voice.localMimeType = mimeType || recorder.mimeType || 'audio/webm';
    voice.localCancel = false;
    voice.localSubmit = false;
    voice.localStartedAt = Date.now();
    voice.localLastVoiceAt = voice.localStartedAt;
    voice.localHadSpeech = false;
    voice.baseText = $('answer').value.trim();
    voice.finalText = '';
    voice.interimText = '';
    voice.mode = mode;
    voice.running = true;
    updateVoiceControls();
    setVoiceStatus(mode === 'active' ? 'A ouvir localmente. Faz uma pausa para enviar.' : 'A gravar localmente. Clica para terminar.');

    recorder.ondataavailable = (event) => {
      if (event.data.size > 0) voice.localChunks.push(event.data);
    };
    recorder.onerror = () => setVoiceStatus('Erro na captura de áudio.');
    recorder.onstop = async () => {
      const shouldSubmit = voice.localSubmit && !voice.localCancel;
      const blob = new Blob(voice.localChunks, {type: voice.localMimeType});
      stream.getTracks().forEach((track) => track.stop());
      stopLocalAudioMonitor();
      clearMaxListenTimer();
      voice.localRecorder = null;
      voice.localStream = null;
      voice.localChunks = [];
      voice.running = false;
      voice.localSubmit = false;
      if (shouldSubmit && blob.size > 0) await submitLocalAudio(blob, voice.localMimeType);
      else if (!voice.activeRequested && !voice.explicitRequested) setVoiceStatus('Microfone desligado');
      updateVoiceControls();
    };
    recorder.start(250);

    if (mode === 'active') {
      const AudioContext = window.AudioContext || window.webkitAudioContext;
      if (AudioContext) {
        voice.localAudioContext = new AudioContext();
        const source = voice.localAudioContext.createMediaStreamSource(stream);
        voice.localAnalyser = voice.localAudioContext.createAnalyser();
        voice.localAnalyser.fftSize = 512;
        source.connect(voice.localAnalyser);
        voice.localFrame = requestAnimationFrame(monitorLocalSilence);
      }
      scheduleMaxListenTimeout(() => finishLocalRecording(voice.localHadSpeech));
    }
  } catch (error) {
    voice.localRecorder = null;
    voice.localStream?.getTracks().forEach((track) => track.stop());
    voice.localStream = null;
    voice.running = false;
    voice.activeRequested = false;
    voice.explicitRequested = false;
    updateVoiceControls();
    setVoiceStatus(error.name === 'NotAllowedError' ? 'Permissão do microfone recusada.' : 'Não foi possível iniciar o áudio local.');
  }
}

function stopVoiceRecognition(options = {}) {
  const voice = state.voice;
  clearTimeout(voice.restartTimer);
  voice.restartTimer = null;
  clearMaxListenTimer();
  if (voice.localRecorder) {
    voice.localCancel = !options.submitAudio;
    voice.localSubmit = Boolean(options.submitAudio);
    try { voice.localRecorder.stop(); } catch (_) { /* already stopped */ }
    return;
  }
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
    if (voice.activeRequested && !state.answerPending && !voice.localProcessing && !voice.speaking && $('thinking').classList.contains('hidden') && !voice.running) {
      startVoiceRecognition('active');
    }
  }, 180);
}

function startBrowserRecognition(mode) {
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
  recognition.lang = LANGUAGE_RECOGNITION_TAG[state.language] || 'pt-PT';
  recognition.continuous = mode === 'active';
  recognition.interimResults = true;
  recognition.maxAlternatives = 1;
  recognition.onstart = () => {
    if (voice.recognition !== recognition) return;
    voice.running = true;
    updateVoiceControls();
    setVoiceStatus(mode === 'active' ? 'A ouvir com STT browser. Uma pausa envia.' : 'A gravar com STT browser. Clica para terminar.');
    if (mode === 'active') {
      scheduleMaxListenTimeout(() => {
        if (voice.finalText.trim()) $('answer-form').requestSubmit();
        else stopVoiceRecognition();
      });
    }
  };
  recognition.onend = () => {
    if (voice.recognition !== recognition) return;
    voice.running = false;
    clearMaxListenTimer();
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
      voice.silenceTimer = setTimeout(() => $('answer-form').requestSubmit(), shortSilenceMs());
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

function startVoiceRecognition(mode) {
  if (selectedSttMode() === 'local') {
    startLocalRecognition(mode);
    return;
  }
  startBrowserRecognition(mode);
}

function setupVoiceControls() {
  if (!localVoiceSupported() && !voiceRecognitionConstructor()) {
    $('active-listening').disabled = true;
    $('voice-input').disabled = true;
    setVoiceStatus('Microfone não suportado neste browser.');
    return;
  }
  $('active-listening').addEventListener('click', () => {
    const voice = state.voice;
    clearTimeout(voice.silenceTimer);
    cancelAgentSpeech();
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
    setVoiceStatus('A iniciar escuta ativa...');
    startVoiceRecognition('active');
  });
  $('voice-input').addEventListener('click', () => {
    const voice = state.voice;
    clearTimeout(voice.silenceTimer);
    cancelAgentSpeech();
    if (voice.explicitRequested) {
      voice.explicitRequested = false;
      stopVoiceRecognition({submitAudio: true});
      updateVoiceControls();
      if (!voice.activeRequested) setVoiceStatus('Microfone desligado');
      return;
    }
    voice.activeRequested = false;
    voice.explicitRequested = true;
    stopVoiceRecognition();
    updateVoiceControls();
    setVoiceStatus('A iniciar microfone...');
    startVoiceRecognition('explicit');
  });
  $('stt-mode').addEventListener('click', () => {
    const voice = state.voice;
    if (voice.running || voice.localProcessing) return;
    voice.sttMode = selectedSttMode() === 'browser' ? 'local' : 'browser';
    localStorage.setItem('apx-stt-mode-v2', voice.sttMode);
    updateVoiceControls();
    setVoiceStatus(voice.sttMode === 'browser' ? 'STT browser: mais rápido e preciso.' : 'STT local: Whisper, sem serviço de voz.');
  });
  updateVoiceControls();
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
  cancelAgentSpeech();
  updateVoiceControls();
  setVoiceStatus('Microfone desligado');
  chatView.classList.add('hidden');
  setupView.classList.remove('hidden');
}
$('end-session').addEventListener('click', endSession);
$('new-session').addEventListener('click', endSession);

function escapeHtml(value) { return String(value).replace(/[&<>"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[char])); }
checkStatus();
loadModelSelector();
