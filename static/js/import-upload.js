/* 批量导入页的全部上传交互（方式一~三）。
 *
 * 从服务器版 quizbank-web/static/js/import-upload.js 移植，去掉多用户相关的
 * CSRF 令牌与配额，改用软件版的单用户路由。
 *
 * 所有上传边界（组数 / 文件数 / 单文件大小 / 允许的扩展名）都不在这里写死，而是
 * 从 #task-list 的 data-limits 读——那份 JSON 由后端 app._upload_limits() 按
 * config.py 生成。前端这几道检查只为「提交前就报错」，真正的边界在后端；两边共用
 * 同一份数字才不会再出现「后端放到 1000、前端还卡在 20」这种分叉。
 *
 * 方式一＝批量试卷转换（原「方式四」，现已置顶为主路径），方式二＝上传 md，
 * 方式三＝粘贴 md。原先的「单份 PDF/图片」入口已删除，见下方注释。
 *
 * 只在非预览态引入（见 import.html）：预览态页面上一个上传控件都没有。
 */
(function () {
  'use strict';

  const MB = 1024 * 1024;

  // 后端注入的上传边界（app._upload_limits）。默认值只在 data-limits 缺失时兜底，
  // 刻意取得比后端宽松——前端拦不住时后端仍会拦，而这里过严会把合法提交挡在门外。
  const LIMITS = (function () {
    const el = document.getElementById('task-list');
    let raw = {};
    try { raw = JSON.parse((el && el.dataset.limits) || '{}'); } catch (e) { raw = {}; }
    return {
      maxGroups: raw.max_groups || 1000,
      maxBatchFiles: raw.max_batch_files || 2000,
      maxFilesPerSide: raw.max_files_per_side || 200,
      maxDocumentBytes: raw.max_document_bytes || 200 * MB,
      maxImageBytes: raw.max_image_bytes || 25 * MB,
      maxRequestBytes: raw.max_request_bytes || 8192 * MB,
      maxMdFiles: raw.max_md_files || 50,
      maxMdFileBytes: raw.max_md_file_bytes || 5 * MB,
      maxMdBatchBytes: raw.max_md_batch_bytes || 20 * MB,
      examExts: raw.exam_exts || ['.bmp', '.docx', '.jpeg', '.jpg', '.pdf', '.png', '.webp'],
      imageExts: raw.image_exts || ['.bmp', '.jpeg', '.jpg', '.png', '.webp'],
      mdExts: raw.md_exts || ['.markdown', '.md', '.txt'],
    };
  })();

  const MAX_GROUPS = LIMITS.maxGroups;

  const extOf = name => {
    const i = String(name || '').lastIndexOf('.');
    return i >= 0 ? String(name).slice(i).toLowerCase() : '';
  };
  const isExamImage = f => LIMITS.imageExts.includes(extOf(f.name));
  const isExamFile = f => LIMITS.examExts.includes(extOf(f.name));
  const isMdFile = f => LIMITS.mdExts.includes(extOf(f.name));
  const mb = bytes => Math.round(bytes / MB);

  /* 单个试卷文件的格式与大小，与后端 _check_exam_file 一一对应。
   * 通过返回 ''，不通过返回给用户看的那句话。 */
  function checkExamFile(f) {
    const ext = extOf(f.name);
    if (!LIMITS.examExts.includes(ext)) {
      // .doc 单独说：pandoc 读不了旧版二进制格式，收下只会让人白等一趟转换。
      if (ext === '.doc') {
        return `「${f.name}」是旧版 .doc 格式，暂不支持，请用 Word / WPS 另存为 .docx 后重新选择`;
      }
      return `「${f.name}」格式不支持，试卷只收 PDF、DOCX 与 PNG/JPG/WEBP/BMP 图片`;
    }
    const isImg = LIMITS.imageExts.includes(ext);
    const limit = isImg ? LIMITS.maxImageBytes : LIMITS.maxDocumentBytes;
    if (f.size > limit) {
      return `「${f.name}」过大（${isImg ? '图片' : '文档'}上限 ${mb(limit)}MB）`;
    }
    return '';
  }

  // ---------- 通用：把一个 .dropzone 变成可拖放 / 可粘贴的灰框 ----------
  // 选中的文件存在闭包里的 files 数组，不回写 input.files（那是只读的，
  // 而且拖进来的文件本来就进不去）。提交时直接读这个数组。
  function makeDropzone(zone, opts) {
    if (!zone) return null;
    opts = opts || {};
    const input = zone.querySelector('input[type=file]');
    const list = zone.querySelector('.dz-files');
    const files = [];

    function render() {
      list.innerHTML = '';
      zone.classList.toggle('has-files', files.length > 0);
      files.forEach((f, i) => {
        const li = document.createElement('li');
        li.className = 'dz-file';
        const name = document.createElement('span');
        name.className = 'dz-fname';
        name.textContent = f.name;   // 文件名可能含 < >，只能走 textContent
        name.title = f.name;
        const rm = document.createElement('button');
        rm.type = 'button';
        rm.className = 'dz-rm';
        rm.textContent = '×';
        rm.title = '移除';
        rm.addEventListener('click', ev => {
          ev.stopPropagation();   // 否则冒泡到 zone 又弹出文件选择框
          files.splice(i, 1);
          render();
        });
        li.appendChild(name);
        li.appendChild(rm);
        list.appendChild(li);
      });
      if (opts.onChange) opts.onChange(files);
    }
    function add(newFiles) {
      const multi = input && input.multiple;
      for (const f of newFiles) {
        if (!f) continue;
        if (!multi) files.length = 0;   // 单文件区：后选的覆盖先选的
        files.push(f);
        if (!multi) break;
      }
      render();
    }

    zone.addEventListener('click', ev => {
      if (ev.target.closest('.dz-rm')) return;
      input.click();
    });
    // 灰框是 div，靠 tabindex 进 tab 序，键盘也得能触发（无障碍）
    zone.addEventListener('keydown', ev => {
      if (ev.key === 'Enter' || ev.key === ' ') {
        ev.preventDefault();
        input.click();
      }
    });
    input.addEventListener('change', () => {
      add(input.files);
      input.value = '';   // 清空原生控件，同名文件才能再次触发 change
    });

    zone.addEventListener('dragover', ev => {
      ev.preventDefault();
      zone.classList.add('drag-over');
    });
    zone.addEventListener('dragleave', () => zone.classList.remove('drag-over'));
    zone.addEventListener('drop', ev => {
      ev.preventDefault();
      zone.classList.remove('drag-over');
      if (ev.dataTransfer && ev.dataTransfer.files.length) add(ev.dataTransfer.files);
    });

    // 截图粘贴：只在这个灰框获得焦点（或鼠标悬停）时接管，避免抢走文本框的粘贴
    if (opts.acceptPaste) {
      document.addEventListener('paste', ev => {
        if (!zone.matches(':hover') && !zone.contains(document.activeElement)) return;
        const items = (ev.clipboardData && ev.clipboardData.files) || [];
        if (items.length) {
          ev.preventDefault();
          add(items);
        }
      });
    }

    render();
    return {
      files: files,
      add: add,
      clear: function () { files.length = 0; render(); },
    };
  }

  // ---------- 通用：题号圆圈点选（批量转换的每个任务卡各一套）----------
  function parseSpec(spec) {
    // '1-6,9,15,16' → [1,2,3,4,5,6,9,15,16]
    const out = [];
    String(spec || '').split(',').forEach(p => {
      p = p.trim();
      if (!p) return;
      if (p.includes('-')) {
        const [a, b] = p.split('-').map(Number);
        for (let i = a; i <= b; i++) out.push(i);
      } else out.push(Number(p));
    });
    return out;
  }

  // sel: 各元素的选择器（任务卡里用 class，root 限定查找范围）。函数保留通用的
  // root 参数：删掉的旧方式一曾以 document 为 root 用 id 选择器，形状不变更省事。
  function wirePicknum(root, sel) {
    const onCb = root.querySelector(sel.on);
    if (!onCb) return null;
    const panel = root.querySelector(sel.panel);
    const circles = root.querySelector(sel.circles);
    const summary = root.querySelector(sel.summary);
    const maxInput = root.querySelector(sel.max);
    const picked = new Set();

    function limit() {
      return Math.max(1, Math.min(99, parseInt(maxInput.value, 10) || 25));
    }
    function renderSummary() {
      const arr = [...picked].sort((a, b) => a - b);
      summary.textContent = arr.length ? arr.join('、') : '无';
    }
    function buildCircles() {
      const max = limit();
      circles.innerHTML = '';
      for (let i = 1; i <= max; i++) {
        const el = document.createElement('span');
        el.className = 'picknum-circle' + (picked.has(i) ? ' on' : '');
        el.textContent = i;
        el.addEventListener('click', () => {
          if (picked.has(i)) { picked.delete(i); el.classList.remove('on'); }
          else { picked.add(i); el.classList.add('on'); }
          renderSummary();
        });
        circles.appendChild(el);
      }
    }
    function applyKeep(nums) {          // 选中 = 要导入的题
      picked.clear();
      nums.forEach(n => picked.add(n));
      buildCircles(); renderSummary();
    }
    function applyDrop(nums) {          // 舍弃这些 → 保留题号范围内其余
      const max = limit();
      const drop = new Set(nums);
      picked.clear();
      for (let i = 1; i <= max; i++) if (!drop.has(i)) picked.add(i);
      buildCircles(); renderSummary();
    }

    onCb.addEventListener('change', () => {
      panel.style.display = onCb.checked ? '' : 'none';
      if (onCb.checked && !circles.children.length) buildCircles();
    });
    maxInput.addEventListener('change', buildCircles);
    root.querySelectorAll(sel.preset).forEach(b =>
      b.addEventListener('click', () => applyKeep(parseSpec(b.dataset.preset))));
    root.querySelectorAll(sel.presetDrop).forEach(b =>
      b.addEventListener('click', () =>
        applyDrop(parseSpec(b.dataset.presetDrop || b.dataset.drop))));
    root.querySelectorAll(sel.clear).forEach(b =>
      b.addEventListener('click', () => applyKeep([])));

    // 返回逗号分隔的已选题号串；开关关着时返回空串（= 不过滤）
    return () => (onCb.checked ? [...picked].sort((a, b) => a - b).join(',') : '');
  }

  // 原先这里有一整段「单份文件 → /convert/start → 轮询 → 填隐藏表单自动进
  // preview」的逻辑（旧方式一）。那条入口已从页面删掉：它与批量试卷转换走同一条
  // 后端链路，只是一次只能传一份，却要单独维护一套 #conv-* / #picknum-* 控件。
  // 批量那条路转好一组后由看板进校对页（后端 batch_group_review 直接 render），
  // 不需要前端代提交，所以轮询与 #auto-preview-form 一起去掉。
  // 后端 /convert/start、/convert/status 保留：批量每组仍登记为一个 job。

  // ---------- 方式二：多个 md 文件 → 建队列逐个校对 ----------
  (function () {
    const btn = document.getElementById('md-submit');
    if (!btn) return;
    const statusEl = document.getElementById('md-status');
    const dz = makeDropzone(document.getElementById('dz-md'), {});
    if (!dz) return;

    btn.addEventListener('click', async () => {
      if (!dz.files.length) { alert('请先选择 .md 文件'); return; }
      // 份数与体量：与后端 import_batch_start 的三道检查对应（那边整个队列连全文
      // 一起留在内存里，所以不是可有可无的形式检查）。
      if (dz.files.length > LIMITS.maxMdFiles) {
        statusEl.textContent = `md 文件过多（${dz.files.length} 个，上限 ${LIMITS.maxMdFiles} 个）`;
        return;
      }
      const bad = dz.files.find(f => !isMdFile(f));
      if (bad) {
        statusEl.textContent = `「${bad.name}」不是 .md / .markdown / .txt 文件`;
        return;
      }
      const oversize = dz.files.find(f => f.size > LIMITS.maxMdFileBytes);
      if (oversize) {
        statusEl.textContent = `「${oversize.name}」过大（单文件上限 ${mb(LIMITS.maxMdFileBytes)}MB）`;
        return;
      }
      const mdTotal = dz.files.reduce((s, f) => s + f.size, 0);
      if (mdTotal > LIMITS.maxMdBatchBytes) {
        statusEl.textContent = `md 文件总量超过上限（${mb(LIMITS.maxMdBatchBytes)}MB）`;
        return;
      }
      const fd = new FormData();
      for (const f of dz.files) fd.append('md_files', f);
      btn.disabled = true;
      statusEl.textContent = '上传中…';
      try {
        // 后端是 302 到队列首页，fetch 默认跟随重定向，跟完取最终 url 跳过去
        const res = await fetch('/import/batch', {method: 'POST', body: fd,
                                                  redirect: 'follow'});
        window.location.href = res.url;
      } catch (e) {
        statusEl.textContent = '请求出错：' + e.message;
        btn.disabled = false;
      }
    });
  })();

  // ---------- 方式三：把 .md 文件拖进粘贴框，直接读成文本 ----------
  (function () {
    const ta = document.getElementById('paste-md');
    if (!ta) return;
    ta.addEventListener('dragover', ev => {
      ev.preventDefault();
      ta.classList.add('drag-over');
    });
    ta.addEventListener('dragleave', () => ta.classList.remove('drag-over'));
    ta.addEventListener('drop', ev => {
      const f = ev.dataTransfer && ev.dataTransfer.files[0];
      if (!f) return;
      ev.preventDefault();
      ta.classList.remove('drag-over');
      const r = new FileReader();
      r.onload = () => { ta.value = String(r.result || ''); };
      r.readAsText(f, 'utf-8');
    });
  })();

  // ---------- 方式一：批量试卷转换（多组，页面上排在最前）----------
  // 这段仍写在文件末尾：它不依赖前面的方式二/三，而 makeDropzone 与 wirePicknum
  // 是函数声明（提升），顺序无关。挪上去只会让 diff 变大。
  (function () {
    const listEl = document.getElementById('task-list');
    if (!listEl) return;
    const tpl = document.getElementById('task-card-tpl');
    const addBtn = document.getElementById('add-task');
    const startBtn = document.getElementById('batch-start');
    const statusEl = document.getElementById('batch-status');
    const ocrSel = document.getElementById('batch-ocr-backend');
    const engSel = document.getElementById('batch-engine');
    const modeWrap = document.getElementById('batch-block-mode-wrap');
    const modeSel = document.getElementById('batch-block-mode');
    const numTpl = document.getElementById('batch-num-template');
    const stayCb = document.getElementById('batch-stay');
    const folderBtn = document.getElementById('import-folder');
    const folderInput = document.getElementById('folder-input');
    const targetParent = document.getElementById('batch-target-parent');
    const targetParentOpen = document.getElementById('batch-target-parent-open');
    const targetParentDialog = document.getElementById('batch-target-parent-dialog');
    const targetParentTree = document.getElementById('batch-target-parent-tree');
    const targetParentCurrent = document.getElementById('batch-target-parent-current');
    const targetParentConfirm = document.getElementById('batch-target-parent-confirm');
    const packCb = document.getElementById('batch-pack-cb');
    const packName = document.getElementById('batch-folder-name');
    const autoCb = document.getElementById('batch-auto-import-cb');
    const perTaskWrap = document.getElementById('batch-per-task-folder-wrap');
    const perTaskCb = document.getElementById('batch-per-task-folder-cb');
    const keepOrigWrap = document.getElementById('batch-keep-original-wrap');
    const keepOrigCb = document.getElementById('batch-keep-original-cb');

    // 目标目录必须按层读取。真实题库有数百个目录；首屏为了一个原生 <select>
    // 递归完整目录树会把“打开批量导入”阻塞数秒。这里展开哪一级才请求哪一级，
    // hidden input 继续沿用原提交字段，后端校验边界不变。
    let pendingFolder = {id: '', name: '题库根目录'};

    function folderNode(folder) {
      const wrap = document.createElement('div');
      wrap.className = 'batch-folder-node';
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'batch-folder-row';
      row.dataset.folderId = folder.id;
      row.dataset.folderName = folder.name;
      row.dataset.hasChildren = folder.has_children ? '1' : '0';
      const twist = document.createElement('span');
      twist.className = 'batch-folder-twist';
      twist.setAttribute('aria-hidden', 'true');
      twist.textContent = folder.has_children ? '›' : '';
      const icon = document.createElement('span');
      icon.className = 'batch-folder-icon';
      icon.setAttribute('aria-hidden', 'true');
      icon.textContent = '▰';
      const label = document.createElement('span');
      label.textContent = folder.name;
      row.append(twist, icon, label);
      wrap.append(row);
      if (folder.has_children) {
        const children = document.createElement('div');
        children.className = 'batch-folder-children';
        children.dataset.folderChildren = folder.id;
        children.hidden = true;
        wrap.append(children);
      }
      return wrap;
    }

    async function loadFolderLevel(host, parentId) {
      if (!host || host.dataset.loaded === '1') return;
      host.dataset.loading = '1';
      const loading = document.createElement('div');
      loading.className = 'batch-folder-loading muted';
      loading.textContent = '正在读取…';
      host.append(loading);
      try {
        const response = await fetch('/collections/children?parent=' + encodeURIComponent(parentId));
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !data.ok) throw new Error(data.error || `读取失败（${response.status}）`);
        host.replaceChildren(...data.children.map(folderNode));
        host.dataset.loaded = '1';
      } catch (error) {
        loading.textContent = '读取失败：' + error.message;
        loading.classList.add('is-error');
      } finally {
        delete host.dataset.loading;
      }
    }

    function markPendingFolder(row) {
      targetParentTree.querySelectorAll('.batch-folder-row.is-selected').forEach(
        item => item.classList.remove('is-selected'));
      row.classList.add('is-selected');
      pendingFolder = {id: row.dataset.folderId || '', name: row.dataset.folderName || '题库根目录'};
      if (targetParentCurrent) targetParentCurrent.textContent = '当前：' + pendingFolder.name;
    }

    async function toggleFolderRow(row) {
      const host = row.parentElement?.querySelector(':scope > .batch-folder-children')
        || (row.classList.contains('is-root')
          ? targetParentTree.querySelector(':scope > .batch-folder-children') : null);
      if (!host) return;
      const opening = host.hidden;
      host.hidden = !opening;
      row.classList.toggle('is-open', opening);
      if (opening) await loadFolderLevel(host, row.dataset.folderId || '');
    }

    if (targetParentTree) targetParentTree.addEventListener('click', event => {
      const row = event.target.closest('.batch-folder-row');
      if (!row) return;
      markPendingFolder(row);
      if (row.dataset.hasChildren === '1' || row.classList.contains('is-root')) {
        toggleFolderRow(row);
      }
    });

    if (targetParentOpen) targetParentOpen.addEventListener('click', async () => {
      if (!targetParentDialog || typeof targetParentDialog.showModal !== 'function') return;
      pendingFolder = {id: targetParent?.value || '', name: targetParentOpen.textContent.trim()};
      if (targetParentCurrent) targetParentCurrent.textContent = '当前：' + pendingFolder.name;
      targetParentDialog.showModal();
      const root = targetParentTree?.querySelector('.batch-folder-row.is-root');
      const rootChildren = targetParentTree?.querySelector(':scope > .batch-folder-children');
      if (root && rootChildren && rootChildren.dataset.loaded !== '1') {
        rootChildren.hidden = false;
        root.classList.add('is-open');
        await loadFolderLevel(rootChildren, '');
      }
    });

    document.querySelectorAll('[data-batch-folder-close]').forEach(button => {
      button.addEventListener('click', () => targetParentDialog?.close());
    });
    if (targetParentConfirm) targetParentConfirm.addEventListener('click', () => {
      if (targetParent) targetParent.value = pendingFolder.id;
      if (targetParentOpen) {
        targetParentOpen.textContent = pendingFolder.name;
        targetParentOpen.title = pendingFolder.id || '题库根目录';
      }
      targetParentDialog?.close();
    });

    // 子选项只在上一级勾上时显示。隐藏时不清 checked：用户勾了又收起来再展开，
    // 状态还在；后端按同样的层级收，隐藏着的值发上去也不会生效。
    function syncPack() {
      if (packName) packName.style.display = packCb.checked ? 'inline-block' : 'none';
    }
    if (packCb) { packCb.addEventListener('change', syncPack); syncPack(); }

    // 三层：免审 → 每任务一个文件夹 → 保存原卷。原卷没有落点目录就存不下，
    // 所以第三层跟着第二层，两层都得勾上才显示。
    function syncAuto() {
      const on = autoCb.checked;
      if (perTaskWrap) perTaskWrap.style.display = on ? 'block' : 'none';
      if (keepOrigWrap) {
        keepOrigWrap.style.display =
          (on && perTaskCb && perTaskCb.checked) ? 'block' : 'none';
      }
    }
    if (autoCb) { autoCb.addEventListener('change', syncAuto); }
    if (perTaskCb) { perTaskCb.addEventListener('change', syncAuto); }
    if (autoCb) syncAuto();

    // 拆题选项只有逐题识别才有意义。另外：勾了「不审核直接入库」时后端会把
    // manual 否决成 all_ai（见 app.py 的 _convert_one_group），所以这里同步把
    // 那一项禁掉并回落——两处口径必须一致，不然用户选了「先人工审核拆题结果」
    // 却不生效，转换直接跑到底，看不出是被谁改的。
    function syncEngine() {
      if (modeWrap) modeWrap.style.display = engSel.value === 'block' ? '' : 'none';
      if (!modeSel) return;
      const manualOpt = modeSel.querySelector('option[value="manual"]');
      if (!manualOpt) return;
      const noReview = !!(autoCb && autoCb.checked);
      manualOpt.disabled = noReview;
      if (noReview && modeSel.value === 'manual') modeSel.value = 'all_ai';
    }
    if (engSel) { engSel.addEventListener('change', syncEngine); }
    if (autoCb) { autoCb.addEventListener('change', syncEngine); }
    if (engSel) syncEngine();

    function renumber() {
      listEl.querySelectorAll('.task-card').forEach((c, i) => {
        c.querySelector('.task-title').textContent = '任务组 ' + (i + 1);
      });
    }

    // 返回该卡的句柄对象（挂到 DOM 节点上，提交时统一读）
    function addTask() {
      if (listEl.querySelectorAll('.task-card').length >= MAX_GROUPS) {
        alert('任务组最多 ' + MAX_GROUPS + ' 组');
        return null;
      }
      const card = tpl.content.firstElementChild.cloneNode(true);
      const incSol = card.querySelector('.task-include-sol');
      const collectionMode = card.querySelector('.task-collection-mode');
      const dzStem = makeDropzone(card.querySelector('.dz-stem'), {acceptPaste: true});
      const dzSol = makeDropzone(card.querySelector('.dz-sol'), {
        acceptPaste: true,
        onChange: fs => { if (fs.length && incSol) incSol.checked = true; },
      });
      const pickedOf = wirePicknum(card, {
        on: '.bg-picknum-on', panel: '.bg-picknum-panel',
        circles: '.bg-picknum-circles', summary: '.bg-picknum-summary',
        max: '.bg-picknum-max', preset: '.bg-preset',
        presetDrop: '.bg-preset-drop', clear: '.bg-picknum-clear',
      });
      card.querySelector('.task-del').addEventListener('click', () => {
        card.remove();
        renumber();
      });
      card._handle = {
        stem: dzStem, sol: dzSol, incSol: incSol,
        collectionMode: collectionMode, picked: pickedOf,
      };
      listEl.appendChild(card);
      renumber();
      return card;
    }

    function resetTasks() {
      // 整卡重建，不复用旧卡：选中的文件存在 makeDropzone 的闭包里，
      // 只清 DOM 清不掉那个数组。
      listEl.innerHTML = '';
      addTask();
    }

    addBtn.addEventListener('click', addTask);
    addTask();   // 默认给一组

    /* ---- 从文件夹导入并自动配对题干 / 解析 ---- */
    // 「XX.pdf」+「XX答案.pdf」这类命名自动凑成一组；落单的解析自成一组。
    const SOLUTION_SUFFIX_RE =
      /(参考答案|答案解析|详细解析|解析|答案|详解|解答)[\s\-_()（）\d]*$/;

    function stripExt(name) { return name.replace(/\.[^.]+$/, ''); }
    function isSolutionLike(name) { return SOLUTION_SUFFIX_RE.test(stripExt(name)); }
    function coreName(name) {
      // 去掉扩展名和「答案」类后缀，剩下的当作配对键
      return stripExt(name).replace(SOLUTION_SUFFIX_RE, '')
        .replace(/[\s\-_()（）]+$/, '');
    }

    // 文件在所选目录树里的父目录（`webkitRelativePath` 形如
    // `选中的文件夹/2026模考/第一份/p1.png`）。图片的分组键就是它，见下面
    // buildGroups 的注释。不带 relativePath 的（拖进来的散图）归到 '' 这个键。
    function parentDir(f) {
      const rel = f.webkitRelativePath || '';
      const i = rel.lastIndexOf('/');
      return i >= 0 ? rel.slice(0, i) : '';
    }

    // 自然序：`p2.png` 必须排在 `p10.png` 前面。字典序会让 12 页的扫描件直接乱页，
    // 而多图是要按这个顺序合成一份 PDF 的（后端 _resolve_input → images_to_pdf）。
    function byNaturalName(a, b) {
      return String(a.name).localeCompare(String(b.name), 'zh',
                                          {numeric: true, sensitivity: 'base'});
    }

    /* 把选中的一堆文件分成若干「组」，每组 {stem: [], sol: []}。
     *
     * 文档（PDF / DOCX）与图片走两套不同的分组键，因为它们的语义本来就不同：
     *
     *   - 一份 PDF / DOCX **就是一整份卷子**，所以按文件名（去掉「答案」类后缀后的
     *     coreName）分组，`XX.pdf` + `XX答案.pdf` 凑成一组。
     *   - 一张图片**只是一页**，一份卷子往往是同一个子目录下的十几张扫描图。所以
     *     图片按**所在子目录**分组，整个目录的图按自然序合成一份卷子。原先图片跟
     *     文档共用 coreName 这一个键，于是 12 页扫描件被切成 12 个各含一张图的任务
     *     组——这是「文件夹里的图片识别不了」最主要的那个原因。
     *
     * 两套键都带上父目录前缀，避免不同子目录里的同名卷子被并到一起。
     */
    function buildGroups(files) {
      const groups = new Map();   // key -> {stem: [], sol: [], dir}
      const bucket = (key, dir) => {
        if (!groups.has(key)) groups.set(key, {stem: [], sol: [], dir: dir});
        return groups.get(key);
      };
      files.forEach(f => {
        const dir = parentDir(f);
        // 图片：整个目录算一份卷子。目录里若混了「答案」字样的图，它们单独凑成
        // 该组的解析侧（`第一份/p1.png` 与 `第一份/答案1.png` 这种排法）。
        const key = isExamImage(f)
          ? 'dir\u0000' + dir
          : 'doc\u0000' + dir + '\u0000' + coreName(f.name);
        bucket(key, dir)[isSolutionLike(f.name) ? 'sol' : 'stem'].push(f);
      });
      // 组内排序放在这里而不是加入时：加入顺序取决于 FileList 的给出顺序，
      // 浏览器不保证它是自然序。
      groups.forEach(g => { g.stem.sort(byNaturalName); g.sol.sort(byNaturalName); });
      return groups;
    }

    if (folderBtn && folderInput) {
      folderBtn.addEventListener('click', () => folderInput.click());
      folderInput.addEventListener('change', () => {
        const all = [...folderInput.files];
        // webkitRelativePath 的第一段就是用户选择的根目录名。按年份目录导入时，
        // 自动把它带成整批文件夹名；用户仍可在提交前取消或改名。
        const selectedRoot = all.length
          ? String(all[0].webkitRelativePath || '').split('/')[0]
          : '';
        folderInput.value = '';
        // 按后端认的扩展名筛（LIMITS.examExts，即 PDF / DOCX / PNG / JPG / JPEG /
        // WEBP / BMP）。目录里的 .doc、.zip、缩略图之类一律跳过并计数报出来，
        // 不静默丢——用户挑了一整个文件夹，得知道有几个没被收下。
        const files = all.filter(isExamFile);
        const skippedDoc = all.filter(f => extOf(f.name) === '.doc').length;
        const oversize = [];
        const usable = files.filter(f => {
          if (checkExamFile(f)) { oversize.push(f.name); return false; }
          return true;
        });
        if (!usable.length) {
          let msg = '这个文件夹里没有可识别的 PDF / Word / 图片。';
          if (skippedDoc) msg += `\n有 ${skippedDoc} 个 .doc 文件：请先用 Word / WPS 另存为 .docx。`;
          if (oversize.length) msg += `\n有 ${oversize.length} 个文件超过大小上限。`;
          alert(msg);
          return;
        }
        if (!confirm(`将从 ${usable.length} 个文件自动配对题干与答案`
                     + '（同一子目录下的图片按页码顺序合成一份卷子），'
                     + '并覆盖当前的任务组配置。继续？')) return;

        const buckets = buildGroups(usable);

        if (selectedRoot && packCb && packName) {
          packCb.checked = true;
          packName.value = selectedRoot;
          syncPack();
        }

        listEl.innerHTML = '';
        let n = 0;
        let trimmedSides = 0;   // 因每侧文件数超限而被截掉尾巴的组数
        for (const [, b] of buckets) {
          if (n >= MAX_GROUPS) break;
          const card = addTask();
          if (!card) break;
          // 只有解析、没有题干 → 它自己当题干（用户可能就想只导这份）
          let stem = b.stem.length ? b.stem : b.sol;
          let sol = b.stem.length ? b.sol : [];
          // 每侧文件数上限：后端 _check_batch_files 会拒整批，这里先截断并告知，
          // 免得用户排好几百组之后才被一句话整批打回。
          if (stem.length > LIMITS.maxFilesPerSide) {
            stem = stem.slice(0, LIMITS.maxFilesPerSide);
            trimmedSides++;
          }
          if (sol.length > LIMITS.maxFilesPerSide) {
            sol = sol.slice(0, LIMITS.maxFilesPerSide);
            trimmedSides++;
          }
          card._handle.stem.add(stem);
          if (sol.length) card._handle.sol.add(sol);
          n++;
        }
        if (!listEl.querySelectorAll('.task-card').length) addTask();
        let msg = '已自动配成 ' + n + ' 组，请检查后开始转换';
        if (buckets.size > MAX_GROUPS) {
          msg += `（超过 ${MAX_GROUPS} 组的部分未加入）`;
        }
        if (skippedDoc) msg += `；跳过 ${skippedDoc} 个 .doc（请另存为 .docx）`;
        if (oversize.length) msg += `；跳过 ${oversize.length} 个超大文件`;
        if (trimmedSides) msg += `；${trimmedSides} 处因单侧文件超过 ${LIMITS.maxFilesPerSide} 个已截断`;
        statusEl.textContent = msg;
      });
    }

    /* ---- 提交 → 跳看板（后台并发转换，转好一组即可审一组）---- */
    startBtn.addEventListener('click', async () => {
      const cards = [...listEl.querySelectorAll('.task-card')];
      if (!cards.length) { alert('请先添加至少一个任务组'); return; }
      if (cards.length > MAX_GROUPS) {
        alert('任务组最多 ' + MAX_GROUPS + ' 组');
        return;
      }
      const fd = new FormData();
      let bad = false;
      let err = '';
      let totalFiles = 0;
      let totalBytes = 0;
      cards.forEach((card, i) => {
        const h = card._handle;
        if (!h.stem.files.length) { bad = true; return; }
        // 逐组逐文件把后端那几道检查先跑一遍（_check_batch_files / _check_exam_file）。
        // 只记第一条错误：一次列十几条没人看，改完再点一次就露出下一条。
        if (h.stem.files.length > LIMITS.maxFilesPerSide
            || h.sol.files.length > LIMITS.maxFilesPerSide) {
          err = err || `任务组 ${i + 1} 的文件过多（每组题干/解析各上限 ${LIMITS.maxFilesPerSide} 个）`;
        }
        if (h.stem.files.length > 1 && !h.stem.files.every(isExamImage)) {
          err = err || `任务组 ${i + 1} 的题干选择了多个非图片文件；多 PDF/Word 请拆成不同任务组`;
        }
        if (h.sol.files.length > 1 && !h.sol.files.every(isExamImage)) {
          err = err || `任务组 ${i + 1} 的解析选择了多个非图片文件；多 PDF/Word 请拆成不同任务组`;
        }
        if (h.collectionMode && h.collectionMode.checked) {
          const validStem = h.stem.files.length === 1
            && extOf(h.stem.files[0].name) === '.pdf';
          const validSol = h.sol.files.length <= 1
            && (!h.sol.files.length || extOf(h.sol.files[0].name) === '.pdf');
          if (!validStem || !validSol) {
            err = err || `任务组 ${i + 1} 的合集模式要求题干恰好一份 PDF，解析至多一份 PDF`;
          } else {
            fd.append('groups[' + i + '][collection_mode]', '1');
          }
        }
        for (const f of [...h.stem.files, ...h.sol.files]) {
          totalFiles++;
          totalBytes += f.size;
          err = err || checkExamFile(f);
        }
        // 题干可多文件：同名字段全部追加，后端 getlist 收（多图按序合成 PDF）
        for (const f of h.stem.files) fd.append('groups[' + i + '][file]', f);
        for (const f of h.sol.files) fd.append('groups[' + i + '][solution_file]', f);
        if (h.incSol.checked) fd.append('groups[' + i + '][include_solution]', '1');
        const picked = h.picked ? h.picked() : '';
        if (picked) fd.append('groups[' + i + '][only_numbers]', picked);
      });
      if (bad) { alert('每个任务组都要选题干文件'); return; }
      if (err) { statusEl.textContent = err; return; }
      if (totalFiles > LIMITS.maxBatchFiles) {
        statusEl.textContent = `本批文件过多（${totalFiles} 个，上限 ${LIMITS.maxBatchFiles} 个）`;
        return;
      }
      if (totalBytes > LIMITS.maxRequestBytes) {
        statusEl.textContent = `本次上传总量超过上限（${mb(LIMITS.maxRequestBytes)}MB），请拆成几批提交`;
        return;
      }
      // OCR 服务与拆题方式相互独立，均按整批提交。
      if (ocrSel) fd.append('ocr_backend', ocrSel.value);
      if (engSel) fd.append('engine', engSel.value);
      if (engSel && engSel.value === 'block') {
        if (modeSel) fd.append('block_mode', modeSel.value);
        if (numTpl && numTpl.value.trim()) fd.append('num_template', numTpl.value.trim());
      }
      // 落点与免审（整批统一）。只在勾上时才发，层级与后端一致。
      if (targetParent && targetParent.value) {
        fd.append('target_parent_id', targetParent.value);
      }
      if (packCb && packCb.checked) {
        fd.append('pack_folder', '1');
        fd.append('pack_folder_name', packName ? packName.value.trim() : '');
      }
      if (autoCb && autoCb.checked) {
        fd.append('auto_import', '1');
        if (perTaskCb && perTaskCb.checked) {
          fd.append('per_task_folder', '1');
          if (keepOrigCb && keepOrigCb.checked) fd.append('auto_keep_original', '1');
        }
      }

      startBtn.disabled = true;
      addBtn.disabled = true;
      statusEl.textContent = '上传中…';
      try {
        const res = await fetch('/batch-convert/create', {method: 'POST', body: fd});
        // 后端所有失败路径（含 413）都回 JSON，但 catch 仍留着：反代或
        // werkzeug 自己插话时拿到的是 HTML，直接 res.json() 会抛在这里，
        // 让用户看到「Unexpected token '<'」而不是发生了什么。
        const data = await res.json().catch(
          () => ({ok: false, error: `服务器返回 ${res.status}`}));
        startBtn.disabled = false;
        addBtn.disabled = false;
        if (!data.ok) {
          statusEl.textContent = '失败：' + (data.error || '');
          return;
        }
        // 建组阶段就废掉的组（多图合成失败）在看板上没有卡片，只能在这里说。
        // 不跳转时也要说：跳过去看板只会看到「20 组里只有 19 张卡」。
        const skipped = data.skipped || [];
        const skipMsg = skipped.length
          ? `（跳过 ${skipped.length} 组：`
            + skipped.slice(0, 3).map(s => `第 ${s.group} 组 ${s.error}`).join('；')
            + (skipped.length > 3 ? ' 等' : '') + '）'
          : '';
        if (skipped.length && !(stayCb && stayCb.checked)) {
          alert(`已提交 ${data.count} 组${skipMsg}`);
        }
        if (stayCb && stayCb.checked) {
          // 留在本页接着排下一批：清空任务组，给个进看板的链接
          resetTasks();
          statusEl.innerHTML = '';
          statusEl.appendChild(document.createTextNode(
            '已提交 ' + data.count + ' 组，正在后台转换。' + skipMsg));
          const a = document.createElement('a');
          a.href = data.dashboard;
          a.textContent = '进入该批看板 →';
          statusEl.appendChild(a);
        } else {
          statusEl.textContent = '已提交，进入看板…';
          window.location.href = data.dashboard;
        }
      } catch (e) {
        statusEl.textContent = '请求出错：' + e.message;
        startBtn.disabled = false;
        addBtn.disabled = false;
      }
    });
  })();
})();
