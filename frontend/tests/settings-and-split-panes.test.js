import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {JSDOM} from 'jsdom';


function flush() {
  return new Promise(resolve => setTimeout(resolve, 0));
}


test('设置页只保留开源下载与更新入口', () => {
  const source = fs.readFileSync(
    new URL('../../static/js/settings-page.js', import.meta.url), 'utf8');
  const template = fs.readFileSync(
    new URL('../../templates/settings.html', import.meta.url), 'utf8');

  assert.doesNotMatch(source, /settings\/cloud|send_sms|invite_code/);
  assert.doesNotMatch(template, /settings\/cloud|send_sms|invite_code/);
  assert.match(template, /data-update-control/);
  assert.match(template, /github_repository_url/);
  assert.match(template, /github_releases_url/);
});


test('隐藏分栏未被拖动时保留响应式 CSS 默认宽度', async () => {
  const dom = new JSDOM(`<!doctype html><html><head><style>
    #owner { --panel-width: 330px; display: grid; }
  </style></head><body>
    <div id="owner">
      <aside id="panel" hidden></aside>
      <div data-split-resizer data-split-owner="#owner" data-split-panel="#panel"
           data-split-property="--panel-width" data-split-min="240"
           data-split-max="520" data-split-main-min="420"></div>
    </div>
  </body></html>`, {
    url: 'http://localhost/', pretendToBeVisual: true, runScripts: 'outside-only',
  });
  const source = fs.readFileSync(
    new URL('../../static/js/split-panes.js', import.meta.url), 'utf8');
  dom.window.eval(source);
  await new Promise(resolve => dom.window.requestAnimationFrame(resolve));

  const owner = dom.window.document.getElementById('owner');
  const handle = dom.window.document.querySelector('[data-split-resizer]');
  assert.equal(owner.style.getPropertyValue('--panel-width'), '');
  assert.equal(handle.getAttribute('aria-valuenow'), '330');
});


test('检查到已签名版本后可调用桌面一键更新', async () => {
  const dom = new JSDOM(`<!doctype html><html><body>
    <div data-update-control>
      <button data-update-action>检查更新</button>
      <span data-update-status></span>
      <progress data-update-progress hidden></progress>
      <a data-update-download hidden>手动下载</a>
    </div>
  </body></html>`, {
    url: 'http://localhost/settings', runScripts: 'outside-only',
  });
  let starts = 0;
  dom.window.confirm = () => true;
  dom.window.pywebview = {api: {
    start_update: async () => {
      starts += 1;
      return {ok: true, update: {status: 'failed', message: '测试结束'}};
    },
  }};
  dom.window.fetch = async () => ({
    ok: true,
    json: async () => ({
      ok: true, enabled: true, available: true, installable: true,
      latest_version: '1.0.0', download_url: 'https://download.example.test/setup.exe',
    }),
  });
  const shellSource = fs.readFileSync(
    new URL('../../static/js/app-shell.js', import.meta.url), 'utf8');
  dom.window.eval(shellSource);
  const source = fs.readFileSync(
    new URL('../../static/js/update-controls.js', import.meta.url), 'utf8');
  dom.window.eval(source);

  const button = dom.window.document.querySelector('[data-update-action]');
  button.click();
  await flush();
  await flush();
  assert.equal(button.textContent, '一键更新');
  assert.match(dom.window.document.querySelector('[data-update-status]').textContent, /1\.0\.0/);

  button.click();
  await flush();
  await flush();
  assert.equal(starts, 1);
  assert.equal(dom.window.document.querySelector('[data-update-status]').textContent, '测试结束');
});


test('iframe 页面通过父窗口桌面桥打开三个目录', async () => {
  const dom = new JSDOM('<!doctype html><html><body><iframe></iframe></body></html>', {
    url: 'http://localhost/workspace', runScripts: 'outside-only',
  });
  const calls = [];
  dom.window.pywebview = {api: {
    open_bank_folder: async () => { calls.push('bank'); return {ok: true}; },
    open_log_folder: async () => { calls.push('logs'); return {ok: true}; },
    open_data_folder: async () => { calls.push('data'); return {ok: true}; },
  }};
  const child = dom.window.document.querySelector('iframe').contentWindow;
  child.document.body.innerHTML = `
    <div class="desktop-only-actions" hidden>
      <button data-desktop-open="bank">题库</button>
      <button data-desktop-open="logs">日志</button>
      <button data-desktop-open="data">数据</button>
    </div>`;
  const alerts = [];
  child.alert = message => alerts.push(message);
  child.eval(fs.readFileSync(
    new URL('../../static/js/app-shell.js', import.meta.url), 'utf8'));
  child.eval(fs.readFileSync(
    new URL('../../static/js/about-page.js', import.meta.url), 'utf8'));

  assert.equal(child.document.querySelector('.desktop-only-actions').hidden, false);
  child.document.querySelectorAll('[data-desktop-open]').forEach(button => button.click());
  await flush();
  await flush();
  assert.deepEqual(calls.sort(), ['bank', 'data', 'logs']);
  assert.deepEqual(alerts, []);
});


test('DeepSeek 和 Qwen 预设填充模型候选与推荐输出', () => {
  const presets = [
    {id: 'deepseek', name: 'DeepSeek', base_url: 'https://api.deepseek.com', models: [
      {id: 'deepseek-v4-flash', label: 'DeepSeek V4 Flash', context_label: '1M', recommended_max_tokens: 32768, supports_vision: false},
    ]},
    {id: 'qwen', name: 'Qwen', base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1', models: [
      {id: 'qwen3.7-flash', label: 'Qwen3.7 Flash', context_label: '控制台', recommended_max_tokens: 32768, supports_vision: false},
      {id: 'qwen3.5-omni-plus', label: 'Qwen Omni', context_label: '控制台', recommended_max_tokens: 16384, supports_vision: true},
    ]},
  ];
  const dom = new JSDOM(`<!doctype html><html><body><main class="settings-page">
    <form class="model-add-form">
      <script type="application/json" data-llm-presets>${JSON.stringify(presets)}</script>
      <select data-llm-preset><option value="">自定义</option><option value="deepseek">DeepSeek</option><option value="qwen">Qwen</option></select>
      <input name="llm_name"><input name="llm_base_url">
      <input name="llm_model" list="llm-model-options"><datalist id="llm-model-options"></datalist>
      <input name="llm_max_tokens" value="8192"><input type="checkbox" name="llm_for_redraw">
      <p data-llm-preset-hint></p>
    </form>
  </main></body></html>`, {url: 'http://localhost/settings', runScripts: 'outside-only'});
  dom.window.eval(fs.readFileSync(
    new URL('../../static/js/settings-page.js', import.meta.url), 'utf8'));
  const form = dom.window.document.querySelector('form');
  const select = form.querySelector('[data-llm-preset]');

  select.value = 'deepseek';
  select.dispatchEvent(new dom.window.Event('change'));
  assert.equal(form.elements.namedItem('llm_model').value, 'deepseek-v4-flash');
  assert.equal(form.elements.namedItem('llm_max_tokens').value, '32768');
  assert.match(form.querySelector('[data-llm-preset-hint]').textContent, /上下文 1M/);

  select.value = 'qwen';
  select.dispatchEvent(new dom.window.Event('change'));
  assert.equal(form.elements.namedItem('llm_base_url').value, 'https://dashscope.aliyuncs.com/compatible-mode/v1');
  assert.equal(form.querySelectorAll('#llm-model-options option').length, 2);
  const model = form.elements.namedItem('llm_model');
  model.value = 'qwen3.5-omni-plus';
  model.dispatchEvent(new dom.window.Event('input'));
  assert.equal(form.elements.namedItem('llm_for_redraw').checked, true);
  assert.equal(form.elements.namedItem('llm_max_tokens').value, '16384');
});
