const state = { sessionId: null, questions: [], current: 0, socket: null };
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
    $('thinking').classList.add('hidden');
    updateInsights(result);
    if (result.next_question) {
      state.current = Math.min(state.current + 1, state.questions.length - 1);
      addMessage('agent', result.next_question);
      renderQuestions();
    }
  };
  state.socket.onerror = () => $('thinking').classList.add('hidden');
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
  if (!text || !state.socket || state.socket.readyState !== WebSocket.OPEN) return;
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

$('voice-input').addEventListener('click', () => {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) return alert('O reconhecimento de voz não é suportado neste browser.');
  const recognition = new SpeechRecognition();
  recognition.lang = 'pt-PT';
  recognition.onstart = () => $('voice-input').classList.add('recording');
  recognition.onend = () => $('voice-input').classList.remove('recording');
  recognition.onresult = (event) => { $('answer').value = event.results[0][0].transcript; };
  recognition.start();
});

async function endSession() {
  if (state.sessionId) await fetch('/stop', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({session_id: state.sessionId}) });
  if (state.socket) state.socket.close();
  chatView.classList.add('hidden');
  setupView.classList.remove('hidden');
}
$('end-session').addEventListener('click', endSession);
$('new-session').addEventListener('click', endSession);

function escapeHtml(value) { return String(value).replace(/[&<>"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[char])); }
checkStatus();
