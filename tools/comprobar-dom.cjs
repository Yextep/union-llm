// Alternativa sin renderizado para entornos donde Chromium no puede arrancar.
const {JSDOM, VirtualConsole} = require('jsdom');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const html = fs.readFileSync('web/index.html', 'utf8');
const script = fs.readFileSync('web/app.js', 'utf8');
let calls = 0, stopped = false, disconnected = false, streamController;
const errors = [];
const consoleBridge = new VirtualConsole();
consoleBridge.on('jsdomError', error => errors.push(error.message));
const dom = new JSDOM(html, {url:'http://192.168.1.3:8080', runScripts:'outside-only', virtualConsole:consoleBridge});
const win = dom.window;
win.TextDecoder = TextDecoder;
win.AbortController = AbortController;
win.HTMLElement.prototype.scrollIntoView = () => {};
win.document.execCommand = () => true;
win.fetch = async (path, options) => {
  if (disconnected) throw new Error('Red desconectada');
  if (path === '/api/estado') return {ok:true,json:async () => ({fase:'listo',ocupado:false, modelo:'Qwen2.5-Coder 3B',
    nodos:['central','movil2','movil3'].map(nombre => ({nombre,conectado:true}))})};
  if (path === '/api/detener') { stopped = true; streamController.close(); return {ok:true}; }
  assert.equal(path, '/api/chat');
  calls++;
  const payload = JSON.parse(options.body);
  assert.equal(payload.messages.at(-1).role,'user');
  const encoder = new TextEncoder();
  const packets = ['event: inicio\ndata: {}\n\n', 'event: token\ndata: ' + JSON.stringify({texto:'```python\ndef sumar(a, b):\n    return a + b\n```\n\n<img src=x onerror="window.inyectado=true">'}) + '\n\n'];
  if (calls === 1) packets.push('event: fin\ndata: {"tokens":30,"segundos":5}\n\n');
  const stream = new ReadableStream({start(controller) {
    streamController = controller;
    // Fragmentos pequenos comprueban que se reconstruyen paquetes y UTF-8.
    const bytes = encoder.encode(packets.join(''));
    for (let start=0; start<bytes.length; start+=7) controller.enqueue(bytes.slice(start,start+7));
    if (calls === 1) controller.close();
  }});
  return {ok:true,body:stream};
};
const waitUntil = async predicate => {
  for (let n=0;n<400;n++) { if (predicate()) return; await new Promise(r => setTimeout(r,10)); }
  throw new Error('No se alcanzo el estado esperado');
};
(async () => {
  try {
    win.eval(script);
    await waitUntil(() => win.document.getElementById('status-short').textContent === 'Listo');
    const prompt = win.document.getElementById('prompt');
    prompt.value = 'Escribe una funcion de Python';
    prompt.dispatchEvent(new win.Event('input'));
    assert.equal(win.document.getElementById('send').disabled, false);
    win.document.getElementById('chat-form').dispatchEvent(new win.Event('submit', {cancelable:true}));
    await waitUntil(() => win.document.getElementById('stop').hidden);
    assert.equal(win.document.querySelectorAll('.message').length,2);
    assert.match(win.document.querySelector('pre code').textContent,/def sumar/);
    assert.equal(win.document.querySelector('.message-body img'),null);
    assert.equal(win.inyectado,undefined);
    win.document.querySelector('.copy-code').click();
    await waitUntil(() => win.document.querySelector('.copy-code').textContent === 'Copiado');
    assert.equal(JSON.parse(win.localStorage.getItem('union-llm-chat-v1')).length,2);
    prompt.value = 'Otra pregunta'; prompt.dispatchEvent(new win.Event('input'));
    win.document.getElementById('chat-form').dispatchEvent(new win.Event('submit', {cancelable:true}));
    await waitUntil(() => calls === 2 && win.document.querySelectorAll('pre code').length === 2);
    win.document.getElementById('stop').click();
    await waitUntil(() => win.document.getElementById('stop').hidden);
    assert.equal(stopped,true);
    win.document.getElementById('new-chat').click();
    assert.equal(win.document.querySelectorAll('.message').length,0);
    assert.equal(win.document.getElementById('welcome').hidden,false);
    disconnected = true;
    await waitUntil(() => win.document.getElementById('status-short').textContent === 'Sin conexión');
    assert.equal(win.document.getElementById('network-dot').className,'dot error');
    assert.equal(win.document.getElementById('send').disabled,true);
    assert.deepEqual(errors,[]);
    console.log('DOM comprobado: paquetes SSE fragmentados, código, copia, HTML seguro, historial, detener y nuevo chat.');
  } finally { dom.window.close(); }
})().catch(error => { console.error(error); process.exit(1); });
