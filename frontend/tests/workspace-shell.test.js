import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {JSDOM} from 'jsdom';


function createShell() {
  const dom = new JSDOM(`<!doctype html><html><body>
    <aside class="app-sidebar"><nav>
      <a class="nav-link" href="/">题库</a>
      <a class="nav-link" href="/handouts">讲义</a>
      <a class="nav-link" href="/library">资料库</a>
    </nav></aside>
    <section id="workspace-shell" data-initial-path="/">
      <iframe id="workspace-question-frame"></iframe>
      <iframe id="workspace-page-frame" hidden></iframe>
      <iframe id="workspace-library-frame" hidden></iframe>
    </section>
  </body></html>`, {
    url: 'http://localhost/workspace?path=/', pretendToBeVisual: true,
    runScripts: 'outside-only',
  });
  const source = fs.readFileSync(
    new URL('../../static/js/workspace-shell.js', import.meta.url), 'utf8');
  dom.window.eval(source);
  return dom;
}


test('题库切到讲义再返回时保留原题库 iframe 与加载状态', () => {
  const dom = createShell();
  const document = dom.window.document;
  const question = document.getElementById('workspace-question-frame');
  const page = document.getElementById('workspace-page-frame');
  question.dataset.loaded = '60';

  document.querySelector('a[href="/handouts"]').click();
  assert.equal(question.hidden, true);
  assert.equal(page.hidden, false);
  assert.equal(page.getAttribute('src'), '/handouts?_embedded=1');

  document.querySelector('a[href="/"]').click();
  assert.equal(question.hidden, false);
  assert.equal(page.hidden, true);
  assert.equal(question.getAttribute('src'), '/?_embedded=1');
  assert.equal(question.dataset.loaded, '60');
});


test('隐藏业务页的迟到定位消息不会把用户从题库拉回讲义', () => {
  const dom = createShell();
  const document = dom.window.document;
  const question = document.getElementById('workspace-question-frame');
  const page = document.getElementById('workspace-page-frame');

  document.querySelector('a[href="/handouts"]').click();
  document.querySelector('a[href="/"]').click();
  dom.window.dispatchEvent(new dom.window.MessageEvent('message', {
    origin: 'http://localhost', source: page.contentWindow,
    data: {
      source: 'quizforge', type: 'location',
      url: 'http://localhost/handouts?path=_handouts%2Ftry.md',
    },
  }));

  assert.equal(question.hidden, false);
  assert.equal(page.hidden, true);
  assert.equal(document.querySelector('a[href="/"]').classList.contains('active'), true);
  assert.match(dom.window.location.search, /path=%2F$/);
});


test('题库文件可切换到资料库并把定位请求交给常驻 iframe', async () => {
  const dom = createShell();
  const document = dom.window.document;
  const question = document.getElementById('workspace-question-frame');
  const library = document.getElementById('workspace-library-frame');
  const calls = [];

  dom.window.dispatchEvent(new dom.window.MessageEvent('message', {
    origin: 'http://localhost', source: question.contentWindow,
    data: {source: 'quizforge', type: 'open-library-file', path: '卷子/试卷.pdf'},
  }));
  assert.equal(library.hidden, false);

  await new Promise(resolve => setTimeout(resolve, 0));
  Object.defineProperty(library.contentWindow, 'postMessage', {
    configurable: true, value: message => calls.push(message),
  });
  dom.window.dispatchEvent(new dom.window.MessageEvent('message', {
    origin: 'http://localhost', source: library.contentWindow,
    data: {source: 'quizforge', type: 'library-ready'},
  }));
  assert.equal(calls[0].source, 'quizforge');
  assert.equal(calls[0].type, 'open-library-file');
  assert.equal(calls[0].path, '卷子/试卷.pdf');
});


test('题库原卷点击后调用桌面默认程序打开', async () => {
  const dom = createShell();
  const question = dom.window.document.getElementById('workspace-question-frame');
  const opened = [];
  dom.window.pywebview = {api: {open_local_file: async path => opened.push(path)}};
  dom.window.dispatchEvent(new dom.window.MessageEvent('message', {
    origin: 'http://localhost', source: question.contentWindow,
    data: {source: 'quizforge', type: 'open-file', path: '卷子/试卷.pdf'},
  }));
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.deepEqual(opened, ['卷子/试卷.pdf']);
});
