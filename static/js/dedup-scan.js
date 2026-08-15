// 查重从同步页面请求改为后台任务：万级文件库的目录遍历就算要几十秒，也不能让
// 工作区 iframe 一直白屏。当前页轮询状态，完成后只替换结果区。
(function () {
  'use strict';
  const form = document.getElementById('dedup-scan-form');
  const threshold = document.getElementById('dedup-threshold');
  const button = document.getElementById('dedup-scan-button');
  const progress = document.getElementById('dedup-progress');
  const title = document.getElementById('dedup-status-title');
  const detail = document.getElementById('dedup-status-detail');
  const results = document.getElementById('dedup-results');
  if (!form || !threshold || !button || !progress || !results) return;

  let timer = 0;
  let activeJob = '';
  let completedJob = '';

  function setState(kind, heading, message) {
    progress.dataset.state = kind;
    title.textContent = heading;
    detail.textContent = message;
  }

  async function poll() {
    if (!activeJob) return;
    try {
      const response = await fetch('/api/dedup/' + encodeURIComponent(activeJob));
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || '读取查重进度失败');
      if (data.status === 'done') {
        clearTimeout(timer);
        completedJob = activeJob;
        activeJob = '';
        button.disabled = false;
        results.innerHTML = data.html || '';
        setState('done', '扫描完成', `已扫描 ${data.total || 0} 道题，发现 ${data.groups || 0} 组疑似重复。`);
        window.QMath?.typeset(results);
        return;
      }
      if (data.status === 'error') throw new Error(data.error || '查重失败');
      const compared = Number(data.compared || 0);
      const compareText = compared ? `已分析 ${compared}/${data.total} 道题…` : `已读取 ${data.total} 道题，正在建立候选索引…`;
      const totalText = data.total == null ? '正在读取题库文件…' : compareText;
      setState('running', '正在扫描当前题库', totalText + ' 可以离开本页继续使用其他功能。');
      timer = window.setTimeout(poll, 900);
    } catch (error) {
      activeJob = '';
      button.disabled = false;
      setState('error', '扫描失败', error.message || '请稍后重试');
    }
  }

  async function start() {
    clearTimeout(timer);
    button.disabled = true;
    results.innerHTML = '';
    setState('running', '正在启动扫描…', '页面可以正常切换；扫描完成后结果会自动显示。');
    try {
      const response = await fetch('/api/dedup/start', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({threshold: Number(threshold.value) || 0.85}),
      });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || '启动查重失败');
      activeJob = data.job_id;
      poll();
    } catch (error) {
      button.disabled = false;
      setState('error', '扫描启动失败', error.message || '请稍后重试');
    }
  }

  form.addEventListener('submit', event => { event.preventDefault(); start(); });
  results.addEventListener('click', event => {
    const control = event.target.closest('[data-dedup-select]');
    if (control) {
      results.querySelectorAll('.del-cb').forEach(cb => {
        cb.checked = control.dataset.dedupSelect === 'all';
      });
      return;
    }
    const more = event.target.closest('[data-dedup-more]');
    if (!more) return;
    const offset = Number(more.dataset.nextOffset || 0);
    more.disabled = true;
    more.textContent = '正在加载…';
    fetch('/api/dedup/' + encodeURIComponent(completedJob || more.dataset.jobId) + '?offset=' + offset)
      .then(response => response.json().then(data => ({response, data})))
      .then(({response, data}) => {
        if (!response.ok || !data.ok) throw new Error(data.error || '加载结果失败');
        results.querySelector('[data-dedup-groups]')?.insertAdjacentHTML('beforeend', data.html || '');
        more.dataset.nextOffset = String(data.next_offset || offset);
        more.textContent = '继续加载结果';
        more.disabled = false;
        if (!data.has_more) more.closest('[data-dedup-more-row]').hidden = true;
        window.QMath?.typeset(results);
      })
      .catch(error => {
        more.disabled = false;
        more.textContent = '加载失败，点击重试';
        setState('error', '部分结果加载失败', error.message || '请重试');
      });
  });
  window.confirmDedupDelete = function () {
    const count = results.querySelectorAll('.del-cb:checked').length;
    if (!count) { alert('没有勾选要删除的题目'); return false; }
    return confirm('确定删除勾选的 ' + count + ' 道题？题目会进入回收站。');
  };

  if (progress.dataset.autoStart === '1') start();
})();

// 共享图片库体检与题目相似度查重是两项独立任务：前者跨全部已登记题库并带永久
// 删除，故必须先生成服务端扫描快照、展示结果，再由用户二次确认。删除时后端还会
// 重新全量扫描，只接受“仍未引用且文件身份没变”的原候选。
(function () {
  'use strict';
  const startButton = document.getElementById('asset-audit-start');
  const deleteButton = document.getElementById('asset-audit-delete');
  const status = document.getElementById('asset-audit-status');
  const result = document.getElementById('asset-audit-result');
  if (!startButton || !deleteButton || !status || !result) return;

  let activeJob = '';
  let timer = 0;
  let lastScan = null;

  function bytes(value) {
    const size = Number(value || 0);
    if (size < 1024) return size + ' B';
    if (size < 1024 * 1024) return (size / 1024).toFixed(1) + ' KiB';
    if (size < 1024 * 1024 * 1024) return (size / 1024 / 1024).toFixed(1) + ' MiB';
    return (size / 1024 / 1024 / 1024).toFixed(2) + ' GiB';
  }

  function setStatus(message, isError) {
    status.textContent = message;
    status.classList.toggle('is-error', !!isError);
  }

  function showScan(data) {
    lastScan = data;
    result.hidden = false;
    const lines = [
      `共享目录：${data.asset_dir || '未配置'}`,
      `覆盖 ${data.bank_count || 0} 个题库、${data.markdown_files || 0} 个 Markdown；图片目录共 ${data.asset_files || 0} 个受管文件。`,
      `仍被引用 ${data.referenced_files || 0} 个；可永久删除 ${data.orphan_count || 0} 个（${bytes(data.orphan_bytes)}）。`,
    ];
    if (data.recent_unreferenced) {
      lines.push(`另有 ${data.recent_unreferenced} 个最近五分钟写入的未引用文件暂不处理，防止撞上正在入库的任务。`);
    }
    if (data.missing_references) {
      lines.push(`注意：发现 ${data.missing_references} 个引用在共享目录中没有对应文件；本操作不会修改这些引用。`);
    }
    if (data.ignored_files) {
      lines.push(`目录内另有 ${data.ignored_files} 个非受管格式或链接文件，已忽略。`);
    }
    result.textContent = lines.join('\n');
    result.style.whiteSpace = 'pre-line';
    deleteButton.hidden = !(Number(data.orphan_count || 0) > 0);
    deleteButton.disabled = false;
  }

  async function poll() {
    if (!activeJob) return;
    try {
      const response = await fetch('/api/assets/orphans/' + encodeURIComponent(activeJob));
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || '读取图片扫描进度失败');
      if (data.status === 'done') {
        startButton.disabled = false;
        setStatus('图片扫描完成。删除按钮只处理本次扫描得到的候选。', false);
        showScan(data);
        return;
      }
      if (data.status === 'error') throw new Error(data.error || '图片扫描失败');
      timer = window.setTimeout(poll, 900);
    } catch (error) {
      activeJob = '';
      startButton.disabled = false;
      deleteButton.hidden = true;
      setStatus('扫描失败：' + (error.message || '请稍后重试'), true);
    }
  }

  startButton.addEventListener('click', async function () {
    clearTimeout(timer);
    activeJob = '';
    lastScan = null;
    startButton.disabled = true;
    deleteButton.hidden = true;
    result.hidden = true;
    setStatus('正在扫描全部已登记题库及共享图片目录；可以离开本页继续使用其他功能…', false);
    try {
      const response = await fetch('/api/assets/orphans/start', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}',
      });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || '启动图片扫描失败');
      activeJob = data.job_id;
      poll();
    } catch (error) {
      startButton.disabled = false;
      setStatus('扫描启动失败：' + (error.message || '请稍后重试'), true);
    }
  });

  deleteButton.addEventListener('click', async function () {
    const count = Number(lastScan?.orphan_count || 0);
    if (!activeJob || !count) return;
    const message = `确定永久删除 ${count} 个未引用图片文件（${bytes(lastScan.orphan_bytes)}）？\n\n`
      + '它们不会进入题目回收站。删除前系统会再次扫描全部题库；新增引用或发生变化的文件会自动跳过。';
    if (!confirm(message)) return;
    deleteButton.disabled = true;
    startButton.disabled = true;
    setStatus('正在重新核对全部引用并永久删除，请勿关闭软件…', false);
    try {
      const response = await fetch('/api/assets/orphans/' + encodeURIComponent(activeJob) + '/delete', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({confirm: true}),
      });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || '删除失败');
      deleteButton.hidden = true;
      setStatus(`已永久删除 ${data.removed || 0} 个文件，释放 ${bytes(data.removed_bytes)}。`
        + (data.changed_or_skipped ? ` ${data.changed_or_skipped} 个文件因新增引用或发生变化而保留。` : ''), false);
      result.textContent += `\n本次实际删除：${data.removed || 0} 个（${bytes(data.removed_bytes)}）。`;
    } catch (error) {
      deleteButton.disabled = false;
      setStatus('删除失败：' + (error.message || '请重新扫描后重试'), true);
    } finally {
      startButton.disabled = false;
    }
  });
})();
