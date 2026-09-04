import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import {JSDOM} from 'jsdom';

const indexSource = fs.readFileSync(
  new URL('../../templates/index.html', import.meta.url), 'utf8');

function extractFunction(name) {
  const plain = indexSource.indexOf(`function ${name}(`);
  const asyncStart = indexSource.indexOf(`async function ${name}(`);
  const starts = [plain, asyncStart].filter(index => index >= 0);
  assert.ok(starts.length, `找不到函数 ${name}`);
  const start = Math.min(...starts);
  const bodyStart = indexSource.indexOf('{', start);
  let depth = 0;
  for (let index = bodyStart; index < indexSource.length; index += 1) {
    if (indexSource[index] === '{') depth += 1;
    if (indexSource[index] === '}') depth -= 1;
    if (depth === 0) return indexSource.slice(start, index + 1);
  }
  throw new Error(`函数 ${name} 未闭合`);
}

function extractRange(startMarker, endMarker) {
  const start = indexSource.indexOf(startMarker);
  const end = indexSource.indexOf(endMarker, start);
  assert.ok(start >= 0, `找不到起始标记 ${startMarker}`);
  assert.ok(end > start, `找不到结束标记 ${endMarker}`);
  return indexSource.slice(start, end);
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return {promise, resolve, reject};
}

function jsonResponse(data, ok = true) {
  return {ok, json: async () => data};
}

async function flushAsyncWork() {
  await new Promise(resolve => setTimeout(resolve, 0));
  await new Promise(resolve => setTimeout(resolve, 0));
}

function setupSelection() {
  const dom = new JSDOM(`<!doctype html><html><body>
    <main id="q-list">
      <article class="card" data-id="q-1"><input class="sel-toggle" type="checkbox"></article>
      <article class="card" data-id="q-2"><input class="sel-toggle" type="checkbox"></article>
    </main>
  </body></html>`, {runScripts: 'outside-only'});
  dom.window.CSS ??= {};
  dom.window.CSS.escape = value => String(value).replace(/[^\w-]/g, '\\$&');
  dom.window.eval(`
    const listEl = document.getElementById('q-list');
    ${extractFunction('normalizeSelectionValue')}
    ${extractFunction('setQuestionSelected')}
    window.__selectionTest = {normalizeSelectionValue, setQuestionSelected};
  `);
  return dom;
}

test('题目勾选统一兼容 JSON 布尔值和旧 0/1 响应', () => {
  const dom = setupSelection();
  const api = dom.window.__selectionTest;
  for (const value of [true, 1, '1']) assert.equal(api.normalizeSelectionValue(value), true);
  for (const value of [false, 0, '0', null, undefined, 'true']) {
    assert.equal(api.normalizeSelectionValue(value), false);
  }

  for (const [value, expected] of [[true, true], [0, false], ['1', true], [false, false]]) {
    assert.equal(api.setQuestionSelected('q-1', value), expected);
    const card = dom.window.document.querySelector('[data-id="q-1"]');
    assert.equal(card.classList.contains('selected'), expected);
    assert.equal(card.querySelector('.sel-toggle').checked, expected);
  }
});

test('统一勾选函数会同步局部插入的同题卡节点', () => {
  const dom = setupSelection();
  const {document} = dom.window;
  const inserted = document.createElement('article');
  inserted.className = 'card';
  inserted.dataset.id = 'q-1';
  inserted.innerHTML = '<input class="sel-toggle" type="checkbox">';
  document.getElementById('q-list').append(inserted);

  dom.window.__selectionTest.setQuestionSelected('q-1', true);
  const copies = [...document.querySelectorAll('.card[data-id="q-1"]')];
  assert.equal(copies.length, 2);
  assert.ok(copies.every(card => card.classList.contains('selected')));
  assert.ok(copies.every(card => card.querySelector('.sel-toggle').checked));
});

const bulkDrawerSource = extractRange(
  "const bulkbar = document.getElementById('bulkbar');",
  'let selectionMutationTail = Promise.resolve();',
);

function setupBulkDrawer(fetchImpl, selectedCount = 2) {
  const dom = new JSDOM(`<!doctype html><html><body>
    <span id="sel-count">${selectedCount}</span>
    <main id="q-list" data-collection=""></main>
    <button type="button" id="bulk-drawer-trigger" aria-controls="bulkbar"
            aria-expanded="false" hidden>
      已选 <b id="bulk-trigger-count">${selectedCount}</b> 题
    </button>
    <dialog id="bulkbar" aria-labelledby="bulk-drawer-title">
      <h2 id="bulk-drawer-title">已选题目</h2>
      <b id="bulk-count">${selectedCount}</b>
      <button type="button" id="bulk-drawer-close">关闭</button>
      <div id="bulk-drawer-feedback"></div>
      <div id="bulk-selected-list"></div>
    </dialog>
  </body></html>`, {
    url: 'http://localhost/questions?collection=current',
    runScripts: 'outside-only',
  });
  const {document, Event} = dom.window;
  const dialog = document.getElementById('bulkbar');
  dialog.showModal = function showModal() {
    this.setAttribute('open', '');
  };
  dialog.close = function close() {
    if (!this.open) return;
    this.removeAttribute('open');
    this.dispatchEvent(new Event('close'));
  };
  dialog.getBoundingClientRect = () => ({left: 100, right: 500, top: 40, bottom: 700});
  dom.window.fetch = fetchImpl;
  dom.window.eval(`
    const listEl = document.getElementById('q-list');
    let folderPopoverCloseCount = 0;
    const locatedRows = [];
    function closeBulkFolderPopover() { folderPopoverCloseCount += 1; }
    function locateSelectedQuestion(row) { locatedRows.push(row); }
    ${bulkDrawerSource}
    window.__bulkDrawerTest = {
      closeBulkDrawer,
      openBulkDrawer,
      refreshBulkSelectedQuestions,
      renderBulkSelectedQuestions,
      setBulkCountDisplay,
      syncBulkbar,
      get locatedRows() { return locatedRows; },
      get folderPopoverCloseCount() { return folderPopoverCloseCount; },
    };
  `);
  return dom;
}

test('已选题标签按计数显隐，抽屉仅在首次打开时加载并在关闭后回收焦点', async () => {
  const pending = deferred();
  let fetchCount = 0;
  const dom = setupBulkDrawer(() => {
    fetchCount += 1;
    return pending.promise;
  });
  const {document, MouseEvent} = dom.window;
  const trigger = document.getElementById('bulk-drawer-trigger');
  const dialog = document.getElementById('bulkbar');

  assert.equal(trigger.hidden, false);
  assert.equal(trigger.textContent.replace(/\s+/g, ' ').trim(), '已选 2 题');
  assert.equal(fetchCount, 0, '收起时不得请求题目详情');

  trigger.focus();
  trigger.click();
  assert.equal(dialog.open, true);
  assert.equal(document.body.classList.contains('bulk-drawer-open'), true);
  assert.equal(trigger.getAttribute('aria-expanded'), 'true');
  assert.equal(document.activeElement, document.getElementById('bulk-drawer-close'));
  assert.equal(fetchCount, 1);

  pending.resolve(jsonResponse({
    ok: true,
    count: 2,
    questions: [
      {
        id: 'q-1', title: '第一题', number: 1, type: '选择题', difficulty: '3',
        source: '模拟卷', folder: '代数', body_html: '<p>完整题干</p>',
        solution_html: '<p>解析内容</p>', note_html: '<p>备注内容</p>',
        starred: true, tags: ['重点'], collection: '数学/代数',
      },
      {id: 'q-2', title: '第二题', body_html: '<p>另一题</p>', tags: []},
    ],
  }));
  await flushAsyncWork();

  const cards = [...document.querySelectorAll('.bulk-selected-card')];
  assert.equal(cards.length, 2);
  assert.equal(cards[0].querySelector('.bulk-selected-body').textContent, '完整题干');
  const disclosures = [...cards[0].querySelectorAll('details')];
  assert.deepEqual(disclosures.map(item => item.querySelector('summary').textContent), [
    '解析', '备注', '标签（1）',
  ]);
  assert.ok(disclosures.every(item => !item.open), '解析、备注和标签应默认折叠');
  assert.equal(cards[0].querySelector('.bulk-selected-note').textContent.includes('备注内容'), true);
  assert.equal(cards[0].querySelector('.tag').textContent, '重点');
  assert.equal(
    cards[0].querySelector('button, form, input, select, textarea, .sel-toggle, [draggable="true"]'),
    null,
    '只读卡片不得带入编辑、删除、勾选或拖拽入口',
  );
  cards[0].querySelector('.bulk-selected-body').click();
  cards[0].querySelector('.bulk-selected-body').dispatchEvent(
    new dom.window.KeyboardEvent('keydown', {key: 'Enter', bubbles: true}),
  );
  assert.deepEqual(
    Array.from(dom.window.__bulkDrawerTest.locatedRows, row => row.id),
    ['q-1', 'q-1'],
  );

  dialog.dispatchEvent(new MouseEvent('click', {bubbles: true, clientX: 20, clientY: 100}));
  assert.equal(dialog.open, false, '点击遮罩应关闭抽屉');
  assert.equal(document.body.classList.contains('bulk-drawer-open'), false);
  assert.equal(trigger.getAttribute('aria-expanded'), 'false');
  assert.equal(document.activeElement, trigger);
  assert.equal(dom.window.__bulkDrawerTest.folderPopoverCloseCount, 2);

  trigger.click();
  assert.equal(fetchCount, 1, '详情未变化时再次打开不得重复请求');
  document.getElementById('bulk-drawer-close').click();
  assert.equal(document.activeElement, trigger);

  trigger.click();
  const cancelEvent = new dom.window.Event('cancel', {cancelable: true});
  if (dialog.dispatchEvent(cancelEvent)) dialog.close();
  assert.equal(dialog.open, false, 'Esc 的原生 cancel 默认行为应关闭抽屉');
  assert.equal(document.activeElement, trigger);

  trigger.click();
  dom.window.__bulkDrawerTest.syncBulkbar(0);
  assert.equal(trigger.hidden, true);
  assert.equal(dialog.open, false, '选题数归零时应自动关闭抽屉');
  assert.equal(document.getElementById('bulk-count').textContent, '0');
  assert.equal(document.querySelectorAll('.bulk-selected-card').length, 0);
});

test('选题操作栏保留常驻清空/删除，并将题集移动复制收进独立面板', () => {
  assert.match(indexSource, /id="bulk-clear-form"[\s\S]*?取消勾选/);
  assert.match(indexSource, /id="bulk-delete-form"[\s\S]*?删除勾选/);
  assert.match(indexSource, /data-bulk-action-trigger="transfer"[\s\S]*?题集操作/);
  assert.match(indexSource, /id="bulk-transfer-section"/);
  assert.doesNotMatch(indexSource, /bulk-more-section|更多操作|移出本集/);
});

test('已选题详情失败后可重试，较早请求的迟到响应不会覆盖新结果', async () => {
  const older = deferred();
  const newer = deferred();
  const responses = [
    Promise.resolve(jsonResponse({ok: false, error: '暂时不可用'}, false)),
    older.promise,
    newer.promise,
  ];
  const dom = setupBulkDrawer(() => responses.shift());
  const {document} = dom.window;
  document.getElementById('bulk-drawer-trigger').click();
  await flushAsyncWork();

  const retry = document.querySelector('[data-bulk-selection-retry]');
  assert.ok(retry);
  assert.equal(retry.closest('.bulk-selected-empty').classList.contains('is-error'), true);

  retry.click();
  const latestRefresh = dom.window.__bulkDrawerTest.refreshBulkSelectedQuestions();
  newer.resolve(jsonResponse({
    ok: true,
    count: 1,
    questions: [{id: 'q-new', title: '新响应', body_html: '<p>新题干</p>', tags: []}],
  }));
  await latestRefresh;
  assert.equal(document.querySelector('.bulk-selected-title').textContent, '新响应');

  older.resolve(jsonResponse({
    ok: true,
    count: 1,
    questions: [{id: 'q-old', title: '迟到响应', body_html: '<p>旧题干</p>', tags: []}],
  }));
  await flushAsyncWork();
  assert.equal(document.querySelector('.bulk-selected-title').textContent, '新响应');
  assert.equal(document.querySelector('[data-id="q-old"]'), null);
});

test('抽屉打开期间选题变化会刷新详情', async () => {
  const payloads = [
    {ok: true, count: 1, questions: [{id: 'q-1', title: '初始题', tags: []}]},
    {ok: true, count: 2, questions: [
      {id: 'q-1', title: '初始题', tags: []},
      {id: 'q-2', title: '新增题', tags: []},
    ]},
  ];
  let fetchCount = 0;
  const dom = setupBulkDrawer(() => {
    const payload = payloads[fetchCount];
    fetchCount += 1;
    return Promise.resolve(jsonResponse(payload));
  }, 1);

  dom.window.document.getElementById('bulk-drawer-trigger').click();
  await flushAsyncWork();
  assert.equal(dom.window.document.querySelectorAll('.bulk-selected-card').length, 1);

  dom.window.__bulkDrawerTest.syncBulkbar(2);
  await flushAsyncWork();
  assert.equal(fetchCount, 2);
  assert.equal(dom.window.document.querySelectorAll('.bulk-selected-card').length, 2);
  assert.equal(dom.window.document.querySelector('[data-id="q-2"] .bulk-selected-title').textContent, '新增题');
});

test('模态抽屉打开时操作提示渲染在抽屉可见层', () => {
  const dom = new JSDOM(`<!doctype html><html><body>
    <dialog id="bulkbar" open><div id="bulk-drawer-feedback"></div></dialog>
  </body></html>`, {runScripts: 'outside-only'});
  dom.window.requestAnimationFrame = callback => callback();
  dom.window.setTimeout = () => 1;
  dom.window.eval(`
    const bulkbar = document.getElementById('bulkbar');
    ${extractFunction('flashToast')}
    window.__toastTest = {flashToast};
  `);

  dom.window.__toastTest.flashToast('批量修改完成');
  const drawerToast = dom.window.document.querySelector('.toast');
  assert.equal(drawerToast.parentElement.id, 'bulk-drawer-feedback');
  assert.equal(drawerToast.textContent, '批量修改完成');

  dom.window.document.getElementById('bulkbar').removeAttribute('open');
  dom.window.__toastTest.flashToast('普通提示');
  assert.equal(dom.window.document.querySelector('.toast').parentElement, dom.window.document.body);
});

function setupSelectedQuestionTabs() {
  const dom = new JSDOM(`<!doctype html><html><body>
    <main id="q-list" data-collection="current" data-loaded="60">
      <article class="card" data-id="q-anchor" data-path="current/q-anchor.md"></article>
    </main>
  </body></html>`, {
    url: 'http://localhost/questions?collection=current&search=函数&type=选择题&difficulty=4&_embedded=1&sort=updated#old',
    runScripts: 'outside-only',
  });
  Object.defineProperty(dom.window, 'scrollY', {configurable: true, value: 480});
  dom.window.document.querySelector('.card').getBoundingClientRect = () => ({
    top: 96, bottom: 260,
  });
  dom.window.eval(`
    const listEl = document.getElementById('q-list');
    let collectionTabSerial = 0;
    const collectionTabsState = {
      tabs: [{key: 'current-tab', id: 'current', name: '当前题集', url: '/questions?collection=current'}],
      active: 'current-tab',
    };
    function activeCollectionTab() {
      return collectionTabsState.tabs.find(tab => tab.key === collectionTabsState.active) || null;
    }
    function persistCollectionTabs() {}
    function renderCollectionTabs() {}
    ${extractFunction('nextCollectionTabKey')}
    ${extractFunction('collectionTabUrl')}
    ${extractFunction('collectionTabScope')}
    ${extractFunction('collectionTabName')}
    ${extractFunction('visibleQuestionAnchor')}
    ${extractFunction('captureCollectionTabPosition')}
    ${extractFunction('saveActiveCollectionTabState')}
    ${extractFunction('selectedQuestionTargetUrl')}
    ${extractFunction('addSelectedQuestionTargetTab')}
    window.__selectedTabTest = {
      selectedQuestionTargetUrl,
      addSelectedQuestionTargetTab,
      state: collectionTabsState,
    };
  `);
  return dom;
}

test('源题链接只保留嵌入参数并始终新建标签，普通目录与根目录分别使用权威范围', () => {
  const dom = setupSelectedQuestionTabs();
  const api = dom.window.__selectedTabTest;
  const collectionUrl = new URL(api.selectedQuestionTargetUrl({collection: '/数学/代数/'}), dom.window.location);
  assert.equal(collectionUrl.pathname, '/questions');
  assert.equal(collectionUrl.searchParams.get('_embedded'), '1');
  assert.equal(collectionUrl.searchParams.get('collection'), '数学/代数');
  assert.equal(collectionUrl.searchParams.get('sort'), 'custom');
  assert.equal(collectionUrl.searchParams.has('all'), false);
  assert.equal(collectionUrl.searchParams.has('search'), false);
  assert.equal(collectionUrl.searchParams.has('type'), false);
  assert.equal(collectionUrl.searchParams.has('difficulty'), false);

  const rootUrl = new URL(api.selectedQuestionTargetUrl({collection: ''}), dom.window.location);
  assert.equal(rootUrl.searchParams.get('all'), '1');
  assert.equal(rootUrl.searchParams.get('sort'), 'custom');
  assert.equal(rootUrl.searchParams.has('collection'), false);

  const original = api.state.tabs[0];
  const first = api.addSelectedQuestionTargetTab({collection: '数学/代数', folder: '代数'});
  assert.equal(original.url, '/questions?collection=current&search=%E5%87%BD%E6%95%B0&type=%E9%80%89%E6%8B%A9%E9%A2%98&difficulty=4&_embedded=1&sort=updated');
  assert.deepEqual({...original.position}, {
    id: 'q-anchor', path: 'current/q-anchor.md', top: 96, scrollY: 480, loaded: 60,
  });
  const second = api.addSelectedQuestionTargetTab({collection: '数学/代数', folder: '代数'});
  assert.equal(api.state.tabs.length, 3);
  assert.equal(api.state.tabs[0], original, '原标签不得被覆盖');
  assert.equal(first.previousKey, 'current-tab');
  assert.equal(second.previousKey, first.tab.key);
  assert.notEqual(first.tab.key, second.tab.key, '相同题集也不得去重新标签');
  assert.equal(api.state.active, second.tab.key);
});

test('新建题目与懒加载目录保留文件展示开关', () => {
  const dom = new JSDOM('<!doctype html><html><body></body></html>', {
    url: 'http://localhost/?collection=旧题集&all=1&recursive=1'
      + '&show_cards=1&show_pdf=1&show_general_md=1&q=函数',
    runScripts: 'outside-only',
  });
  dom.window.eval(`
    ${extractFunction('collectionTabUrl')}
    ${extractFunction('collectionContextUrl')}
    ${extractFunction('folderChildrenUrl')}
    window.__contextUrlTest = {collectionContextUrl, folderChildrenUrl};
  `);

  const target = new URL(
    dom.window.__contextUrlTest.collectionContextUrl('/新题集/'),
    dom.window.location,
  );
  assert.equal(target.searchParams.get('collection'), '新题集');
  assert.equal(target.searchParams.get('show_cards'), '1');
  assert.equal(target.searchParams.get('show_pdf'), '1');
  assert.equal(target.searchParams.get('show_general_md'), '1');
  assert.equal(target.searchParams.get('q'), '函数');
  assert.equal(target.searchParams.has('all'), false);
  assert.equal(target.searchParams.has('recursive'), false);

  const children = new URL(
    dom.window.__contextUrlTest.folderChildrenUrl('父题集'),
    dom.window.location,
  );
  assert.equal(children.searchParams.get('parent'), '父题集');
  assert.equal(children.searchParams.get('show_cards'), '1');
  assert.equal(children.searchParams.get('show_pdf'), '1');
  assert.equal(children.searchParams.get('show_general_md'), '1');
});

function setupQuestionLocator(fragmentPlans = [], pagePlans = []) {
  const dom = new JSDOM(`<!doctype html><html><body>
    <main id="q-list" data-collection="current" data-page-token=""
          data-loaded="0" data-total="0"></main>
  </body></html>`, {
    url: 'http://localhost/questions?collection=current&_embedded=1',
    runScripts: 'outside-only',
  });
  dom.window.CSS ??= {};
  dom.window.CSS.escape = value => String(value).replace(/[^\w-]/g, '\\$&');
  dom.window.HTMLElement.prototype.scrollIntoView = function scrollIntoView(options) {
    this.__scrollOptions = options;
    dom.window.__scrollCount += 1;
  };
  dom.window.__fragmentPlans = [...fragmentPlans];
  dom.window.__pagePlans = [...pagePlans];
  dom.window.__scrollCount = 0;
  dom.window.setTimeout = callback => {
    dom.window.__highlightCleanup = callback;
    return 1;
  };
  dom.window.eval(`
    const listEl = document.getElementById('q-list');
    let bulkLocateGeneration = 0;
    let bulkPendingLocate = null;
    let bulkSelectedDirty = false;
    let questionPageGeneration = 0;
    let questionPageLoading = false;
    let collectionTabSerial = 0;
    let pageLoadCount = 0;
    let drawerCloseCount = 0;
    let resetCount = 0;
    let refreshCount = 0;
    const toasts = [];
    const events = [];
    const questionPageObserver = {disconnect() {}};
    const collectionTabsState = {
      tabs: [{key: 'current-tab', id: 'current', name: '当前题集', url: '/questions?collection=current'}],
      active: 'current-tab',
    };
    function saveActiveCollectionTabState() {}
    function persistCollectionTabs() {}
    function renderCollectionTabs() {}
    function createBlankCollectionTab() {
      return {key: nextCollectionTabKey(), id: '', name: '空白页', url: '/questions', position: null};
    }
    function closeBulkDrawer() { drawerCloseCount += 1; }
    function flashToast(message, kind) { events.push('toast'); toasts.push({message, kind}); }
    function refreshBulkSelectedQuestions() {
      events.push('refresh'); refreshCount += 1; return Promise.resolve();
    }
    function waitForPageLayout() { return Promise.resolve(); }
    function resetQuestionInfiniteScroll() { resetCount += 1; }
    async function loadFolderFragment(url, _push, _refreshTree, _fallbackOnError, commitGuard) {
      const plan = window.__fragmentPlans.shift();
      const loaded = plan ? await plan : true;
      if (!loaded) return false;
      if (commitGuard && !commitGuard()) return false;
      const parsed = new URL(url, location.href);
      listEl.replaceChildren();
      listEl.dataset.collection = parsed.searchParams.get('collection') || '';
      listEl.dataset.pageToken = 'page-2';
      listEl.dataset.loaded = '30';
      listEl.dataset.total = '90';
      return true;
    }
    async function loadNextQuestionPage() {
      const plan = window.__pagePlans.shift();
      if (plan) await plan;
      pageLoadCount += 1;
      listEl.dataset.loaded = String(30 + pageLoadCount * 30);
      if (pageLoadCount === 1) {
        const middle = document.createElement('article');
        middle.className = 'card';
        middle.dataset.id = 'q-middle';
        listEl.append(middle);
        listEl.dataset.pageToken = 'page-3';
      } else {
        const target = document.createElement('article');
        target.className = 'card';
        target.dataset.id = 'q-deep';
        listEl.append(target);
        listEl.dataset.pageToken = '';
      }
    }
    ${extractFunction('nextCollectionTabKey')}
    ${extractFunction('collectionTabUrl')}
    ${extractFunction('collectionTabScope')}
    ${extractFunction('collectionTabName')}
    ${extractFunction('selectedQuestionTargetUrl')}
    ${extractFunction('addSelectedQuestionTargetTab')}
    ${extractFunction('rollbackSelectedQuestionTargetTab')}
    ${extractFunction('cancelPendingBulkLocate')}
    ${extractFunction('waitForQuestionPageIdle')}
    ${extractFunction('locateSelectedQuestion')}
    window.__locatorTest = {
      locateSelectedQuestion,
      state: collectionTabsState,
      cancelTo(tabKey) {
        bulkLocateGeneration += 1;
        collectionTabsState.active = tabKey;
      },
      cancelLocate() { cancelPendingBulkLocate(); },
      get pageLoadCount() { return pageLoadCount; },
      get drawerCloseCount() { return drawerCloseCount; },
      get resetCount() { return resetCount; },
      get refreshCount() { return refreshCount; },
      get events() { return events; },
      get toasts() { return toasts; },
    };
  `);
  return dom;
}

test('源题定位可跨多批分页命中，并将题卡居中、聚焦和短暂高亮', async () => {
  const dom = setupQuestionLocator();
  const api = dom.window.__locatorTest;
  await api.locateSelectedQuestion({id: 'q-deep', collection: '数学/代数', folder: '代数'});

  const target = dom.window.document.querySelector('[data-id="q-deep"]');
  assert.ok(target);
  assert.equal(api.pageLoadCount, 2);
  assert.equal(api.state.tabs.length, 2);
  assert.equal(api.state.tabs[0].key, 'current-tab');
  assert.equal(api.state.active, api.state.tabs[1].key);
  assert.equal(api.drawerCloseCount, 1);
  assert.equal(api.resetCount, 1);
  assert.equal(dom.window.document.activeElement, target);
  assert.equal(target.classList.contains('question-locate-highlight'), true);
  assert.equal(target.__scrollOptions.block, 'center');
  assert.equal(dom.window.__scrollCount, 1);
  assert.equal(api.toasts.length, 0);
});

test('连续定位时旧任务静默失效，后一次新标签和定位结果保持有效', async () => {
  const olderFragment = deferred();
  const dom = setupQuestionLocator([olderFragment.promise]);
  const api = dom.window.__locatorTest;
  const row = {id: 'q-deep', collection: '数学/代数', folder: '代数'};

  const olderLocate = api.locateSelectedQuestion(row);
  const newerLocate = api.locateSelectedQuestion(row);
  await newerLocate;
  olderFragment.resolve(false);
  await olderLocate;

  assert.equal(api.state.tabs.length, 3, '每次定位都应创建独立题集标签');
  assert.equal(api.state.active, api.state.tabs[2].key);
  assert.equal(dom.window.__scrollCount, 1, '旧任务不得重复滚动或夺取焦点');
  assert.equal(api.drawerCloseCount, 1);
  assert.equal(api.toasts.length, 0);
});

test('切回原标签会在提交前丢弃迟到的定位片段', async () => {
  const fragment = deferred();
  const dom = setupQuestionLocator([fragment.promise]);
  const api = dom.window.__locatorTest;
  const locating = api.locateSelectedQuestion({
    id: 'q-deep', collection: '数学/代数', folder: '代数',
  });
  api.cancelTo('current-tab');
  fragment.resolve(true);
  await locating;

  assert.equal(dom.window.document.getElementById('q-list').dataset.collection, 'current');
  assert.equal(api.state.active, 'current-tab');
  assert.equal(dom.window.__scrollCount, 0);
  assert.equal(api.drawerCloseCount, 0);
  assert.equal(api.toasts.length, 0);
});

test('首个定位片段返回前关闭抽屉会回滚尚未加载的目标标签', async () => {
  const fragment = deferred();
  const dom = setupQuestionLocator([fragment.promise]);
  const api = dom.window.__locatorTest;
  const locating = api.locateSelectedQuestion({
    id: 'q-deep', collection: '数学/代数', folder: '代数',
  });
  api.cancelLocate();
  assert.equal(api.state.tabs.length, 1, '关闭时应立即移除尚未提交的目标标签');
  assert.equal(api.state.active, 'current-tab');
  fragment.resolve(true);
  await locating;

  assert.equal(api.state.tabs.length, 1);
  assert.equal(api.state.active, 'current-tab');
  assert.equal(dom.window.document.getElementById('q-list').dataset.collection, 'current');
  assert.equal(api.toasts.length, 0);
});

test('连续定位后取消最新任务会回到最初已提交标签', async () => {
  const older = deferred();
  const newer = deferred();
  const dom = setupQuestionLocator([older.promise, newer.promise]);
  const api = dom.window.__locatorTest;
  const row = {id: 'q-deep', collection: '数学/代数', folder: '代数'};
  const first = api.locateSelectedQuestion(row);
  const second = api.locateSelectedQuestion(row);

  api.cancelLocate();
  assert.equal(api.state.active, 'current-tab');
  assert.equal(api.state.tabs.length, 2, '较早创建的未加载标签保留，但不得成为活动页');

  newer.resolve(true);
  older.resolve(true);
  await Promise.all([first, second]);
  assert.equal(api.state.active, 'current-tab');
  assert.equal(dom.window.document.getElementById('q-list').dataset.collection, 'current');
  assert.equal(api.toasts.length, 0);
});

test('关闭抽屉取消分页定位后仍恢复当前目标页的无限滚动监听', async () => {
  const pendingPage = deferred();
  const dom = setupQuestionLocator([], [pendingPage.promise]);
  const api = dom.window.__locatorTest;
  const locating = api.locateSelectedQuestion({
    id: 'q-deep', collection: '数学/代数', folder: '代数',
  });
  await flushAsyncWork();

  api.cancelLocate();
  pendingPage.resolve();
  await locating;

  assert.equal(api.resetCount, 1, '任务失效不能让当前页无限滚动永久断开');
  assert.equal(dom.window.__scrollCount, 0);
  assert.equal(api.drawerCloseCount, 0);
  assert.equal(api.toasts.length, 0);
});

test('目标失效时加载到分页末尾并刷新选题篮，不执行全页回退', async () => {
  const dom = setupQuestionLocator();
  const api = dom.window.__locatorTest;
  const originalUrl = dom.window.location.href;
  await api.locateSelectedQuestion({
    id: 'q-missing', collection: '数学/代数', folder: '代数',
  });

  assert.equal(api.pageLoadCount, 2);
  assert.equal(api.refreshCount, 1);
  assert.equal(api.resetCount, 1);
  assert.equal(api.toasts.length, 1);
  assert.match(api.toasts[0].message, /原题可能已移动或删除/);
  assert.deepEqual(Array.from(api.events), ['refresh', 'toast']);
  assert.equal(dom.window.location.href, originalUrl);
});

test('分页失败时明确提示并刷新选题篮', async () => {
  const page = deferred();
  const dom = setupQuestionLocator([], [page.promise]);
  const api = dom.window.__locatorTest;
  const locating = api.locateSelectedQuestion({
    id: 'q-deep', collection: '数学/代数', folder: '代数',
  });
  await flushAsyncWork();
  page.reject(new Error('分页连接中断'));
  await locating;

  assert.equal(api.refreshCount, 1);
  assert.equal(api.resetCount, 1);
  assert.equal(api.toasts.length, 1);
  assert.match(api.toasts[0].message, /分页连接中断/);
});

function setupFolderTree(bankKey = 'bank-a') {
  const dom = new JSDOM(`<!doctype html><html><body>
    <ul id="folder-tree" data-bank-key="${bankKey}">
      <li class="folder-item has-children collapsed" data-folder-id="manual" data-active-path="0">
        <div class="folder-row"><span class="folder-twist"></span></div>
        <ul class="folder-children collapsed" data-parent-id="manual" data-loaded="1"></ul>
      </li>
      <li class="folder-item has-children collapsed" data-folder-id="active-root" data-active-path="1">
        <div class="folder-row"><span class="folder-twist"></span></div>
        <ul class="folder-children collapsed" data-parent-id="active-root" data-loaded="1"></ul>
      </li>
      <li class="folder-item has-children collapsed" data-folder-id="unrelated" data-active-path="0">
        <div class="folder-row"><span class="folder-twist"></span></div>
        <ul class="folder-children collapsed" data-parent-id="unrelated" data-loaded="1"></ul>
      </li>
    </ul>
  </body></html>`, {url: 'http://localhost/', runScripts: 'outside-only'});
  dom.window.eval(`
    const folderTree = document.getElementById('folder-tree');
    const FOLDER_TREE_STORE = \`quizforge:folder-tree:v1:\${folderTree?.dataset.bankKey || 'default'}\`;
    ${extractFunction('readFolderExpansion')}
    ${extractFunction('writeFolderExpansion')}
    ${extractFunction('setFolderExpanded')}
    ${extractFunction('restoreFolderExpansion')}
    window.__folderTreeTest = {setFolderExpanded, restoreFolderExpansion};
  `);
  return dom;
}

test('文件树嵌套空白右键继承所属题集上下文', () => {
  const dom = new JSDOM(`<!doctype html><html><body>
    <ul id="folder-tree" class="folder-list">
      <li class="folder-item" data-folder-id="parent">
        <div class="folder-row"></div>
        <ul class="folder-children"><li class="folder-file-item" data-file-path="parent/paper.pdf"></li></ul>
      </li>
    </ul>
  </body></html>`, {runScripts: 'outside-only'});
  dom.window.eval(`
    const folderTree = document.getElementById('folder-tree');
    ${extractFunction('resolveFolderContext')}
    window.__resolveFolderContext = resolveFolderContext;
  `);

  const {document} = dom.window;
  const nestedChildren = document.querySelector('.folder-children');
  const nested = dom.window.__resolveFolderContext(nestedChildren);
  assert.equal(nested.kind, 'folder');
  assert.equal(nested.item.dataset.folderId, 'parent');

  const file = document.querySelector('.folder-file-item');
  const fileContext = dom.window.__resolveFolderContext(file);
  assert.equal(fileContext.kind, 'file');
  assert.equal(fileContext.item.dataset.folderId, 'parent');

  const root = dom.window.__resolveFolderContext(document.getElementById('folder-tree'));
  assert.equal(root.kind, 'root');
});

test('文件树只恢复手动项和活动祖先且按题库隔离状态', async () => {
  const dom = setupFolderTree('bank-a');
  const {document, sessionStorage} = dom.window;
  sessionStorage.setItem('quizforge:folder-tree:v1:bank-a', JSON.stringify({
    manual: true,
    'active-root': false,
  }));

  await dom.window.__folderTreeTest.restoreFolderExpansion();
  assert.equal(document.querySelector('[data-folder-id="manual"]').classList.contains('collapsed'), false);
  assert.equal(document.querySelector('[data-folder-id="active-root"]').classList.contains('collapsed'), false);
  assert.equal(document.querySelector('[data-folder-id="unrelated"]').classList.contains('collapsed'), true);

  await dom.window.__folderTreeTest.setFolderExpanded(
    document.querySelector('[data-folder-id="manual"]'), false, true);
  assert.deepEqual(
    JSON.parse(sessionStorage.getItem('quizforge:folder-tree:v1:bank-a')),
    {manual: false, 'active-root': false},
  );
  assert.equal(sessionStorage.getItem('quizforge:folder-tree:v1:bank-b'), null);
});

test('文件夹移动下拉增强后会连同可见包装器一起移动', () => {
  const dom = new JSDOM(`<!doctype html><html><body>
    <div id="origin"><span class="qf-select"><select id="folder-move-select"></select><button></button></span></div>
    <div id="target"></div>
  </body></html>`, {runScripts: 'outside-only'});
  dom.window.eval(`
    const folderMoveSelect = document.getElementById('folder-move-select');
    ${extractFunction('folderMoveControlNode')}
    ${extractFunction('placeFolderMoveControl')}
    window.__moveControl = {folderMoveControlNode, placeFolderMoveControl};
  `);

  const {document} = dom.window;
  dom.window.__moveControl.placeFolderMoveControl(document.getElementById('target'));
  const control = dom.window.__moveControl.folderMoveControlNode();
  assert.equal(control.className, 'qf-select');
  assert.equal(document.getElementById('target').nextElementSibling, control);
  assert.equal(control.querySelector('select').id, 'folder-move-select');
});
