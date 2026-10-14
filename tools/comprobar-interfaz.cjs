const {chromium} = require('playwright-core');
const assert = require('node:assert/strict');
(async () => {
  const browser = await chromium.launchPersistentContext('', {executablePath:'/usr/bin/chromium', headless:true,
    viewport:{width:1366,height:900},
    args:['--no-sandbox','--disable-dev-shm-usage','--disable-gpu']});
  try {
    const page = browser.pages()[0] || await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/api/estado', route => route.fulfill({json:{fase:'listo', mensaje:'Listo', ocupado:false,
      modelo:'Qwen2.5-Coder 3B', contexto:2048, max_tokens:384,
      nodos:['central','movil2','movil3'].map(nombre => ({nombre, conectado:true}))}}));
    let calls = 0;
    await page.route('**/api/chat', route => {
      calls++;
      const payload = route.request().postDataJSON();
      assert.equal(payload.messages.at(-1).role,'user');
      const body = 'event: inicio\ndata: {}\n\nevent: token\ndata: ' + JSON.stringify({texto:'Puedes usar esta función:\n\n```python\ndef sumar(a, b):\n    return a + b\n```\n\n<script>window.inyectado = true</script>'}) +
        '\n\nevent: fin\ndata: {"segundos":4,"tokens":30}\n\n';
      return route.fulfill({status:200,contentType:'text/event-stream',body});
    });
    await page.goto('http://127.0.0.1:8080');
    await page.locator('#status-short').filter({hasText:'Listo'}).waitFor();
    await page.screenshot({path:'resultados/chat-escritorio.png'});
    await page.locator('[data-prompt]').nth(1).click();
    await page.locator('#send').click();
    await page.locator('.copy-code').waitFor();
    await page.waitForFunction(() => document.getElementById('stop').hidden);
    assert.equal(await page.locator('.message').count(),2);
    assert.equal(await page.evaluate(() => window.inyectado), undefined);
    assert.equal(await page.locator('.message-body script').count(),0);
    assert.match(await page.locator('pre code').textContent(),/def sumar/);
    await page.locator('.copy-code').click();
    assert.equal(await page.locator('.copy-code').textContent(),'Copiado');
    await page.reload();
    await page.locator('.message').nth(1).waitFor();
    assert.equal(calls,1);
    await page.locator('#new-chat').click();
    assert.equal(await page.locator('.message').count(),0);
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:'resultados/chat-movil.png'});
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.locator('#menu').click();
    assert.equal(await page.locator('#menu').getAttribute('aria-expanded'),'true');
    assert.ok(await page.locator('#sidebar').isVisible());
    assert.deepEqual(errors,[]);
    console.log('Interfaz comprobada: escritorio, móvil, chat, historial, copia de código y escape HTML.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
