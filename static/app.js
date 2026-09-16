const state = {
  sessionId: null,
  config: {language: 'pt-PT', min_pause_seconds: 1.5, duration_minutes: 20, script_fidelity: 'adaptive', model: ''},
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
    lastConfidence: null,
    captureMode: 'text',
    autoSubmitted: false,
    answerMaxTimer: null,
    sttMode: localStorage.getItem('apx-stt-mode-v2') || 'browser',
  },
};
const $ = (id) => document.getElementById(id);
const setupView = $('setup-view');
const chatView = $('chat-view');

async function checkStatus() {
  const label = $('status-label');
  const dot = $('status-dot');
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 3000);
  label.textContent = 'A ligar motor...';
  dot.className = 'status-dot';
  try {
    const response = await fetch('/api/status', {cache: 'no-store', signal: controller.signal});
    if (!response.ok) throw new Error(`Estado indisponivel (${response.status})`);
    const status = await response.json();
    label.textContent = status.label;
    dot.className = `status-dot ${status.available ? 'online' : 'offline'}`;
    if (!status.available) setTimeout(checkStatus, 5000);
  } catch (_) {
    label.textContent = 'API indisponivel';
    dot.className = 'status-dot offline';
    setTimeout(checkStatus, 5000);
  } finally {
    clearTimeout(timeout);
  }
}

async function loadModelOptions() {
  const select = $('llm-model');
  if (!select) return;
  try {
    const response = await fetch('/api/models', {cache: 'no-store'});
    if (!response.ok) return;
    const result = await response.json();
    const models = Array.isArray(result.models) ? result.models : [];
    select.innerHTML = models.map((model) => `<option value="${escapeHtml(model)}">${escapeHtml(model)}</option>`).join('');
    if (result.current && models.includes(result.current)) select.value = result.current;
  } catch (_) {
    // The static defaults in the HTML remain available when Ollama is starting.
  }
}

function sessionLanguage() {
  return state.config.language || $('language')?.value || 'pt-PT';
}

function pauseDelayMs() {
  const configured = Number(state.config.min_pause_seconds || $('min-pause-seconds')?.value || 1.5);
  return Math.max(900, Math.min(4000, configured * 1000));
}

function expectedAnswerWindowMs() {
  const question = state.questions[state.current] || '';
  const words = question.trim().split(/\s+/).filter(Boolean).length;
  return Math.max(18000, Math.min(75000, 18000 + words * 2200));
}

function clearAnswerWindowTimer() {
  clearTimeout(state.voice.answerMaxTimer);
  state.voice.answerMaxTimer = null;
}

function scheduleAnswerWindowTimer() {
  clearAnswerWindowTimer();
  state.voice.answerMaxTimer = setTimeout(() => {
    const voice = state.voice;
    if (!voice.activeRequested || state.answerPending || !voice.finalText.trim() && !$('answer').value.trim()) return;
    voice.autoSubmitted = true;
    if (voice.localRecorder) finishLocalRecording(true);
    else $('answer-form').requestSubmit();
  }, expectedAnswerWindowMs());
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
  state.config = {...state.config, ...(start.config || {})};
  state.questions = [...(JSON.parse($('questions').dataset.questions || '[]'))];
  state.current = Number(start.current_index || 0);
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
    if (result.stt_confidence != null) {
      setVoiceStatus(`Resposta recebida (${Math.round(Number(result.stt_confidence) * 100)}% de confiança STT).`);
    }
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
  const payload = {
    persona: $('persona').value.trim(),
    instructions: $('instructions').value.trim(),
    avoid: $('avoid').value.trim(),
    candidate_context: $('candidate-context').value.trim(),
    questions,
    language: $('language').value,
    must: $('must').value.trim(),
    dont: $('avoid').value.trim(),
    questions_to_ask: questions,
    questions_to_avoid: $('questions-to-avoid').value.split('\n').map((q) => q.trim()).filter(Boolean),
    script_fidelity: $('script-fidelity').value,
    duration_minutes: Number($('duration-minutes').value || 20),
    min_pause_seconds: Number($('min-pause-seconds').value || 1.5),
    model: $('llm-model').value,
  };
  const response = await fetch('/start', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) });
  if (!response.ok) return alert('Não foi possível iniciar a sessão.');
  showSession(await response.json());
});

$('answer-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const text = $('answer').value.trim();
  if (!text || state.answerPending || !state.socket || state.socket.readyState !== WebSocket.OPEN) return;
  const voice = state.voice;
  const captureMode = voice.captureMode || 'text';
  const sttConfidence = voice.lastConfidence;
  const autoSubmitted = Boolean(voice.autoSubmitted);
  state.answerPending = true;
  cancelAgentSpeech();
  clearTimeout(state.voice.silenceTimer);
  clearAnswerWindowTimer();
  stopVoiceRecognition();
  addMessage('user', text);
  $('answer').value = '';
  $('thinking').classList.remove('hidden');
  state.socket.send(JSON.stringify({
    text_answer: text,
    stt_confidence: sttConfidence,
    capture_mode: captureMode,
    auto_submitted: autoSubmitted,
  }));
  voice.lastConfidence = null;
  voice.captureMode = 'text';
  voice.autoSubmitted = false;
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
      body: JSON.stringify({text, language: sessionLanguage(), session_id: state.sessionId}),
    });
    if (!response.ok) {
      const detail = await response.text();
      throw new Error(detail || 'Piper indisponivel');
    }
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
  } catch (error) {
    if (requestId === voice.ttsRequestId) {
      setVoiceStatus(`Piper indisponivel; fallback browser. ${String(error.message || '').slice(0, 120)}`);
      speakWithBrowser(text);
    }
  }
}

function speakWithBrowser(text) {
  if (!('speechSynthesis' in window)) {
    state.voice.speaking = false;
    return;
  }
  speechSynthesis.cancel();
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = sessionLanguage();
  utterance.rate = 0.96;
  utterance.pitch = 1;
  const voices = speechSynthesis.getVoices();
  utterance.voice = voices.find((voice) => voice.lang.toLowerCase() === 'pt-pt')
    || voices.find((voice) => voice.lang.toLowerCase().startsWith('pt'))
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
  clearAnswerWindowTimer();
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
    if (voice.localHadSpeech && elapsed > 1400 && Date.now() - voice.localLastVoiceAt > pauseDelayMs()) {
      voice.autoSubmitted = voice.mode === 'active';
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
      body: JSON.stringify({audio_base64: bytesToBase64(await blob.arrayBuffer()), audio_format: format, language: sessionLanguage()}),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || 'STT local indisponível.');
    voice.finalText = String(result.text || '').trim();
    voice.lastConfidence = result.confidence == null ? null : Number(result.confidence);
    voice.interimText = '';
    renderVoiceTranscript();
    if (!voice.finalText) {
      setVoiceStatus('Não ouvi uma resposta. Podes repetir.');
      if (voice.activeRequested) scheduleActiveVoiceRestart();
      return;
    }
    if (voice.mode === 'active') {
      voice.autoSubmitted = true;
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
    voice.captureMode = mode === 'active' ? 'active-local' : 'local';
    voice.lastConfidence = null;
    voice.autoSubmitted = false;
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
    if (mode === 'active') scheduleAnswerWindowTimer();

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
  clearAnswerWindowTimer();
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
  voice.captureMode = mode === 'active' ? 'active-browser' : 'browser';
  voice.lastConfidence = null;
  voice.autoSubmitted = false;
  recognition.lang = sessionLanguage();
  recognition.continuous = mode === 'active';
  recognition.interimResults = true;
  recognition.maxAlternatives = 1;
  recognition.onstart = () => {
    if (voice.recognition !== recognition) return;
    voice.running = true;
    if (mode === 'active') scheduleAnswerWindowTimer();
    updateVoiceControls();
    setVoiceStatus(mode === 'active' ? 'A ouvir com STT browser. Uma pausa envia.' : 'A gravar com STT browser. Clica para terminar.');
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
      const alternative = event.results[index][0];
      if (event.results[index].isFinal) {
        finalText += alternative.transcript;
        if (typeof alternative.confidence === 'number' && alternative.confidence > 0) voice.lastConfidence = alternative.confidence;
      } else interimText += alternative.transcript;
    }
    voice.finalText += finalText;
    voice.interimText = interimText;
    renderVoiceTranscript();
    clearTimeout(voice.silenceTimer);
    if (mode === 'active' && finalText.trim()) {
      voice.silenceTimer = setTimeout(() => {
        voice.autoSubmitted = true;
        $('answer-form').requestSubmit();
      }, pauseDelayMs());
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
loadModelOptions();
