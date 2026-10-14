'use strict';
const $ = id => document.getElementById(id);
const storageKey = 'union-llm-chat-v1';
let history = [], state = null, generating = false, activeId = null, abort = null, transientNotice = '';
try {
  const saved = JSON.parse(localStorage.getItem(storageKey) || '[]');
  if (Array.isArray(saved) && saved.every((m, i) => m && m.role === (i % 2 ? 'assistant' : 'user') && typeof m.content === 'string' && m.content.trim())) {
    history = saved.length % 2 ? saved.slice(0, -1) : saved;
  }
} catch (_) {}
const escapeHTML = text => text.replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
function inline(text) {
  return escapeHTML(text).replace(/`([^`\n]+)`/g, '<code>$1</code>').replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
}
function markdown(text) {
  const parts = text.split('```');
  return parts.map((part, i) => {
    if (!(i % 2)) return part.split(/\n\s*\n/).filter(p => p.trim()).map(p => '<p>' + inline(p).replace(/\n/g, '<br>') + '</p>').join('');
    const newline = part.indexOf('\n');
    const language = newline < 0 ? '' : part.slice(0, newline).trim();
    const code = newline < 0 ? part : part.slice(newline + 1).replace(/\n$/, '');
    return '<div class="code-block"><div class="code-header"><span>' + escapeHTML(language || 'Código') + '</span><button class="copy-code" type="button">Copiar código</button></div><pre><code>' + escapeHTML(code) + '</code></pre></div>';
  }).join('');
}
function save() { try { localStorage.setItem(storageKey, JSON.stringify(history)); } catch (_) {} }
function addMessage(role, text) {
  const article = document.createElement('article');
  article.className = 'message ' + role;
  article.innerHTML = '<div class="avatar">' + (role === 'user' ? 'Tú' : 'U') + '</div><div class="message-main"><div class="message-title">' + (role === 'user' ? 'Tú' : 'Union LLM') + '<small></small></div><div class="message-body"></div></div>';
  $('messages').append(article);
  renderMessage(article, role, text);
  return article;
}
function renderMessage(article, role, text) {
  const body = article.querySelector('.message-body');
  if (role === 'user') body.textContent = text;
  else body.innerHTML = text ? markdown(text) : '<span class="typing">Preparando la respuesta</span>';
}
function renderHistory() {
  $('messages').replaceChildren();
  history.forEach(m => addMessage(m.role, m.content));
  $('welcome').hidden = history.length > 0;
  $('conversation-title').querySelector('span').textContent = history[0]?.content || 'Empecemos algo';
}
function scrollDown(force = false) {
  const scroller = $('chat-scroll');
  if (force || scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 220) scroller.scrollTop = scroller.scrollHeight;
}
function controls() {
  $('send').disabled = generating || !state || state.fase !== 'listo' || state.ocupado || !$('prompt').value.trim();
  $('send').hidden = generating;
  $('stop').hidden = !generating;
  $('new-chat').disabled = generating;
  $('prompt').readOnly = generating;
}
function showNotice() {
  const offline = !state || state.fase !== 'listo';
  const text = transientNotice || (offline ? (state?.mensaje || 'Conectando con el servidor…') : (generating ? (state.en_uso === 1 ? 'El central está trabajando en tu respuesta…' : `${state.en_uso} dispositivos están trabajando en tu respuesta…`) : state.ocupado ? 'Hay otra pregunta en curso. Espera un momento.' : `${state.en_uso} ${state.en_uso === 1 ? 'dispositivo' : 'dispositivos'} en uso · Una pregunta a la vez`));
  $('notice').textContent = text;
  $('notice').classList.toggle('error', state?.fase === 'error' || !!transientNotice);
}
async function poll() {
  try {
    const response = await fetch('/api/estado', {cache:'no-store'});
    if (!response.ok) throw new Error('Estado no disponible');
    state = await response.json();
    $('model-name').textContent = state.modelo;
    const labels = {listo:'Listo', cargando:'Cargando modelo', conectando:'Conectando', reconectando:'Reconectando', error:'Sin conexión', interfaz:'Vista previa'};
    $('status-short').textContent = state.ocupado ? 'Respondiendo' : (labels[state.fase] || state.fase);
    for (const id of ['status-dot','network-dot']) $(id).className = 'dot ' + (state.fase === 'listo' ? 'ready' : state.fase === 'error' ? 'error' : '');
    $('nodes').replaceChildren(...state.nodos.map(node => {
      const row = document.createElement('div');
      row.className = 'node';
      const icon = node.nombre === 'central' ? '<path d="M3 4h18v12H3zM8 20h8M12 16v4"/>' : '<rect x="6" y="2" width="12" height="20" rx="2"/><path d="M10 18h4"/>';
      row.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true">' + icon + '</svg><span></span><small></small>';
      row.querySelector('span').textContent = node.nombre === 'central' ? 'Central' : node.nombre;
      row.querySelector('small').textContent = node.detalle || (node.conectado ? 'Conectado' : 'En espera');
      return row;
    }));
  } catch (_) {
    state = {fase:'error', ocupado:false, mensaje:'No se puede conectar con el central. Comprueba la Wi-Fi y que el chat siga abierto.'};
    $('status-short').textContent = 'Sin conexión';
    $('status-dot').className = 'dot error';
    $('network-dot').className = 'dot error';
    $('nodes').querySelectorAll('small').forEach(label => { label.textContent = 'Sin datos'; });
  }
  controls(); showNotice();
}
async function send(event) {
  event.preventDefault();
  const question = $('prompt').value.trim();
  if (!question || generating || $('send').disabled) return;
  transientNotice = '';
  generating = true;
  activeId = Date.now().toString(36) + '-' + Math.random().toString(36).slice(2);
  abort = new AbortController();
  const previous = [...history];
  history.push({role:'user', content:question});
  $('welcome').hidden = true;
  $('conversation-title').querySelector('span').textContent = history[0].content;
  addMessage('user', question);
  const article = addMessage('assistant', '');
  let answer = '', completed = false;
  $('prompt').value = ''; resize(); controls(); showNotice(); scrollDown(true);
  try {
    const response = await fetch('/api/chat', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({id:activeId, messages:history}), signal:abort.signal});
    if (!response.ok) throw new Error((await response.json()).error || 'No se pudo enviar la pregunta.');
    const reader = response.body.getReader(), decoder = new TextDecoder();
    let pending = '';
    function packet(block) {
      const lines = block.split('\n');
      const name = lines.find(l => l.startsWith('event: '))?.slice(7);
      const data = JSON.parse(lines.filter(l => l.startsWith('data: ')).map(l => l.slice(6)).join('\n') || '{}');
      if (name === 'token') { answer += data.texto; renderMessage(article, 'assistant', answer); scrollDown(); }
      if (name === 'inicio' && data.historial_recortado) { transientNotice = 'El contexto se llenó: el modelo usa los mensajes más recientes.'; showNotice(); }
      if (name === 'error') throw new Error(data.error);
      if (name === 'fin') {
        completed = true;
        article.querySelector('.message-title small').textContent = data.detenido ? 'Respuesta detenida' : [data.segundos && data.segundos + ' s', data.tokens && data.tokens + ' tokens'].filter(Boolean).join(' · ');
        if (data.limite) transientNotice = 'La respuesta alcanzó el límite. Puedes pedirle que continúe.';
      }
    }
    while (true) {
      const {value, done} = await reader.read();
      pending += decoder.decode(value || new Uint8Array(), {stream:!done});
      let boundary;
      while ((boundary = pending.indexOf('\n\n')) !== -1) { const block = pending.slice(0, boundary); pending = pending.slice(boundary + 2); if (block.trim()) packet(block); }
      if (done) break;
    }
    if (!completed) throw new Error('La respuesta se interrumpió. Comprueba la conexión con el central.');
  } catch (error) {
    if (error.name === 'AbortError') {
      article.querySelector('.message-title small').textContent = 'Respuesta detenida';
      if (!answer) { article.remove(); history = previous; $('prompt').value = question; }
    } else {
      transientNotice = error.message;
      article.classList.add('error');
      if (!answer) {
        article.querySelector('.message-body').textContent = error.message;
        history = previous;
        $('prompt').value = question;
      }
    }
  } finally {
    if (answer) history.push({role:'assistant', content:answer});
    // El navegador conserva pares completos; los intentos fallidos no se reenvían.
    save(); generating = false; activeId = null; abort = null;
    await poll(); resize(); controls(); showNotice(); $('prompt').focus();
  }
}
async function stop() {
  if (!activeId) return;
  $('stop').disabled = true;
  try { await fetch('/api/detener', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({id:activeId})}); }
  finally { abort?.abort(); $('stop').disabled = false; }
}
function resize() { $('prompt').style.height = 'auto'; $('prompt').style.height = Math.min($('prompt').scrollHeight, 160) + 'px'; }
async function copyCode(button) {
  const code = button.closest('.code-block').querySelector('code').textContent;
  try {
    if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(code);
    else {
      const helper = document.createElement('textarea'); helper.className = 'sr-only'; helper.value = code;
      document.body.append(helper); helper.select(); const copied = document.execCommand('copy'); helper.remove();
      if (!copied) throw new Error('Copia no disponible');
    }
    button.textContent = 'Copiado';
  } catch (_) { button.textContent = 'Selecciona y copia'; }
  setTimeout(() => { button.textContent = 'Copiar código'; }, 1800);
}
$('chat-form').addEventListener('submit', send);
$('stop').addEventListener('click', stop);
$('prompt').addEventListener('input', () => { resize(); controls(); });
$('prompt').addEventListener('keydown', event => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); $('chat-form').requestSubmit(); } });
$('new-chat').addEventListener('click', () => { history = []; save(); transientNotice = ''; renderHistory(); $('prompt').value = ''; resize(); $('sidebar').classList.remove('open'); $('menu').setAttribute('aria-expanded','false'); controls(); showNotice(); $('prompt').focus(); });
$('menu').addEventListener('click', () => { const open = $('sidebar').classList.toggle('open'); $('menu').setAttribute('aria-expanded', String(open)); });
document.querySelectorAll('[data-prompt]').forEach(button => button.addEventListener('click', () => { $('prompt').value = button.dataset.prompt; resize(); controls(); $('prompt').focus(); }));
$('messages').addEventListener('click', event => { const button = event.target.closest('.copy-code'); if (button) copyCode(button); });
window.addEventListener('pagehide', () => { if (activeId) fetch('/api/detener', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({id:activeId}), keepalive:true}).catch(() => {}); });
renderHistory(); resize(); poll(); setInterval(poll, 2500);
