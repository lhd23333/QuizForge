/* 导出抽屉交互控制器（题库页专属，2026-10-05 导出界面重构）。
 *
 * 从 index.html 内嵌脚本整体迁出（字体预览 / 模式联动 / 格式联动 / 预览 /
 * 提交 / ?export_task 回填），并新增：抽屉开合与「预设 ↔ 四维度」联动。
 * 迁出的直接收益是可以用 jsdom 独立测试（frontend/tests/）。
 *
 * 与既有页面的接口（为什么不全部收进 IIFE）：
 * - postForExport 仍是全局函数：题库页内联脚本里单题 TeX ZIP 按钮继续调用
 *   （defer 脚本晚于内联脚本执行，而调用发生在用户点击之后，必然已就绪）；
 * - syncModeUI / setExportPanelOpen 挂 window：题卡是 AJAX 动态替换的
 *   （inline 编辑保存、新建题卡、无限滚动、文件夹切换、总览视图），这些
 *   内联调用点要重刷新卡上「独占整页」复选框的显隐 / 收起导出配置。
 *
 * 结构约定（改动前必读）：
 * - 抽屉是非模态 aside，不是 dialog：custom-select.js 把下拉菜单 appendChild
 *   到 body，模态 dialog 的 inert 会把抽屉里的模板/格式/字体下拉点死。
 * - #export-mode 保留为隐藏 select：模板兼容过滤、任务回填、专属面板判断
 *   都经它走，提交的 payload 也以 mode 为兼容锚点；四维 radio 以自身 name
 *   直接提交；预设胶囊（name=preset）只驱动前端联动，后端按白名单挑字段。
 * - 「预设」只是四维的快捷配置：同四维必同产物（预设=试卷 与自定义
 *   flow/分题型/单栏/A4 完全等价，后端等价表同一路径）；改动任一维度即
 *   回退「自定义」高亮，不回弹。
 * - 提交路径与旧版完全一致：FormData(form) → POST /export 登记后台任务；
 *   预览固定 fmt=pdf。本文件不引入任何新的后端契约。
 */
(function () {
  'use strict';

  function notify(message, kind) {
    // flashToast 在 index.html 内联脚本里定义；jsdom 单测挂的同构 DOM 可能
    // 没有它，静默兜底即可——提示不是功能路径。
    window.flashToast?.(message, kind);
  }

  // 导出请求统一入口：后端出错回 JSON（{ok:false,error}），网络层出错连
  // JSON 都没有；统一收成一句话异常。全局函数——题库页单题 TeX ZIP 按钮也调。
  async function postForExport(url, fd) {
    const res = await fetch(url, {method: 'POST', body: fd});
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (!res.ok || !data || !data.ok) {
      throw new Error((data && data.error) || ('请求失败（HTTP ' + res.status + '）'));
    }
    return data;
  }
  window.postForExport = postForExport;

  const form = document.getElementById('export-form');
  const drawer = document.getElementById('export-panel');
  // postForExport 挂载在早退之前：即使页面结构变了，单题导出的依赖也不断。
  if (!form || !drawer) return;

  const trigger = document.getElementById('export-drawer-trigger');
  const closeBtn = document.getElementById('export-drawer-close');
  const modeSel = document.getElementById('export-mode');
  const stdExamInput = document.getElementById('export-std-exam');
  const keypointsField = document.getElementById('keypoints-field');
  const stdFields = document.getElementById('std-fields');
  const exportFormatSel = document.getElementById('export-format');
  const paperToneField = document.getElementById('paper-tone-field');
  const wimathLogoField = document.getElementById('wimath-logo-field');
  const exportTemplateField = document.getElementById('export-template-field');
  const exportTemplateSel = document.getElementById('export-template');
  const wordExportHint = document.getElementById('word-export-hint');
  const cjkFontSel = document.getElementById('export-cjk-font');
  const latinFontSel = document.getElementById('export-latin-font');
  const cjkFontPreview = document.getElementById('cjk-font-preview');
  const latinFontPreview = document.getElementById('latin-font-preview');
  const paperToneValue = document.getElementById('paper-tone-value');
  const toneSwatches = document.getElementById('tone-swatches');
  const toneColor = document.getElementById('tone-color');

  // —— 取值 / 设值小工具 ——
  // radio 组用 querySelector 直查，不走 form.elements 的 RadioNodeList——
  // 读写语义在 jsdom 与浏览器间有差异，直查最稳。
  const getRadio = name => {
    const el = form.querySelector(`input[type="radio"][name="${name}"]:checked`);
    return el ? el.value : '';
  };
  const setRadio = (name, value) => {
    const el = form.querySelector(
      `input[type="radio"][name="${name}"][value="${value}"]`);
    // 程序性赋值 checked 不派发 change（DOM 规范），不会触发联动监听器——
    // 需要联动时显式调 sync 系列。
    if (el) el.checked = true;
  };
  const eachRadio = (name, fn) => {
    form.querySelectorAll(`input[type="radio"][name="${name}"]`).forEach(fn);
  };
  const lockRadio = (name, value, locked) => {
    const el = form.querySelector(
      `input[type="radio"][name="${name}"][value="${value}"]`);
    if (el) el.disabled = locked;
  };

  // —— 抽屉开合（非模态）——
  // 选题清空时 .bulk-action-stack 整体隐藏（触发标签随之消失），index.html 的
  // syncBulkbar 会调 QFExportDrawer.closeIfOpen() 收掉抽屉。
  function setExportPanelOpen(open) {
    drawer.classList.toggle('hidden', !open);
    trigger?.setAttribute('aria-expanded', String(open));
  }
  const isDrawerOpen = () => !drawer.classList.contains('hidden');
  window.QFExportDrawer = {
    closeIfOpen() { if (isDrawerOpen()) setExportPanelOpen(false); },
  };
  trigger?.addEventListener('click', () => setExportPanelOpen(!isDrawerOpen()));
  closeBtn?.addEventListener('click', () => setExportPanelOpen(false));
  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape' || !isDrawerOpen()) return;
    // 预览层打开时 Esc 先归预览层管；不能顺带把抽屉也关了。
    const overlay = document.getElementById('preview-overlay');
    if (overlay && !overlay.classList.contains('hidden')) return;
    setExportPanelOpen(false);
  });

  // —— 预设 ↔ 四维度 ——
  //
  // 预设就是「四维 + 卷头开关」的快捷配置表，dims/stdExam/mode 三个字段与后端
  // 完全对应：
  //   dims    → 提交的 layout/grouped/columns/ratio 四个 radio；
  //   stdExam → 提交的 std_exam 隐藏域（与四维正交的卷头开关，E3：选标准试卷
  //             后改版式，卷头不静默消失）；
  //   mode    → 隐藏 select 的锚点值（模板兼容过滤 + payload 兼容）。
  // 「笔记」与「讲义」四维相同，靠「知识要点是否为内容型字段」区分——与后端
  // _paginate_custom（layout=two 且 keypoints 非空即插要点页）同一口径。
  const DEFAULT_DIMS = {layout: 'flow', grouped: '1', columns: '1', ratio: 'a4'};
  const PRESETS = {
    custom:   {dims: DEFAULT_DIMS, stdExam: false, mode: 'exam'},
    exam:     {dims: DEFAULT_DIMS, stdExam: false, mode: 'exam'},
    exam_std: {dims: DEFAULT_DIMS, stdExam: true,  mode: 'exam_std'},
    note:     {dims: {layout: 'two', grouped: '0', columns: '1', ratio: 'a4'},
               stdExam: false, mode: 'note'},
    lecture:  {dims: {layout: 'one', grouped: '0', columns: '1', ratio: 'a4'},
               stdExam: false, mode: 'lecture'},
    slides:   {dims: {layout: 'one', grouped: '0', columns: '1', ratio: 'wide'},
               stdExam: false, mode: 'slides'},
    practice: {dims: {layout: 'flow', grouped: '1', columns: '2', ratio: 'a4'},
               stdExam: false, mode: 'practice'},
    list:     {dims: {layout: 'compact', grouped: '1', columns: '1', ratio: 'a4'},
               stdExam: false, mode: 'list'},
    handout:  {dims: {layout: 'two', grouped: '0', columns: '1', ratio: 'a4'},
               stdExam: false, mode: 'handout'},
  };

  const readDims = () => ({
    layout: getRadio('layout'),
    grouped: getRadio('grouped'),
    columns: getRadio('columns'),
    ratio: getRadio('ratio'),
  });
  const dimsEqual = (a, b) =>
    a.layout === b.layout && a.grouped === b.grouped
    && a.columns === b.columns && a.ratio === b.ratio;

  // 四维 → mode 锚点。折算顺序必须与后端 exporter.resolve_export_layout 的
  // template_mode 完全一致（wide → 双栏 → 卷头 → two → one → compact →
  // else），否则前端按折算值过滤出的兼容模板清单会与后端门禁不一致。
  function modeForDims(dims, stdExam) {
    if (dims.ratio === 'wide') return 'slides';
    if (dims.columns === '2') return 'practice';
    if (stdExam && dims.columns === '1' && dims.ratio === 'a4') return 'exam_std';
    if (dims.layout === 'two') return 'note';
    if (dims.layout === 'one') return 'lecture';
    if (dims.layout === 'compact') return 'list';
    return 'exam';
  }

  function syncModeFromDims() {
    if (modeSel) {
      modeSel.value = modeForDims(readDims(), stdExamInput?.value === '1');
    }
  }

  // UI 防呆（后端 resolve_export_layout 有同规则 clamp 兜底，这里只是让用户
  // 点不出不存在的组合）：横版 16:9 只有「一页一题 + 单栏」；「一页 N 题」
  // 是单栏页结构；双栏只服务流式/紧凑/自适应（自适应+双栏 = 双栏刷题本来的
  // 留白口径，不锁）。
  function syncDimsAvailability() {
    const layout = getRadio('layout');
    const columns = getRadio('columns');
    const wide = getRadio('ratio') === 'wide';
    const pageLayout = layout === 'one' || layout === 'two';
    lockRadio('layout', 'flow', wide);
    lockRadio('layout', 'compact', wide);
    lockRadio('layout', 'adaptive', wide);
    lockRadio('layout', 'two', wide || columns === '2');
    lockRadio('layout', 'one', columns === '2');
    lockRadio('columns', '2', wide || pageLayout);
  }

  function syncModeUI() {
    // 「知识要点」与题卡上「独占整页」复选框只在「一页两题」类组合里有意义
    // （后端对 layout=two 才插要点页；fullpage_ids 只被 two 类分页器消费）。
    // 跟随版式而不是预设：预设被改动回退「自定义」后，隐藏字段的值仍在提交、
    // 按口径仍然生效——让显隐与生效口径一致，才不会静默丢内容。
    const isTwo = getRadio('layout') === 'two';
    if (keypointsField) keypointsField.classList.toggle('hidden', !isTwo);
    document.querySelectorAll('.fullpage-box').forEach(el => {
      el.classList.toggle('hidden', !isTwo);
    });
    // 标准试卷说明区跟随卷头开关本身：选「标准试卷」后改版式，std_exam 仍为
    // 1、卷头不消失，字段面板也必须跟着留下。
    if (stdFields) {
      stdFields.style.display = stdExamInput?.value === '1' ? 'block' : 'none';
    }
  }

  function applyPreset(name) {
    const def = PRESETS[name];
    if (!def) return;
    setRadio('layout', def.dims.layout);
    setRadio('grouped', def.dims.grouped);
    setRadio('columns', def.dims.columns);
    setRadio('ratio', def.dims.ratio);
    if (stdExamInput) stdExamInput.value = def.stdExam ? '1' : '';
    // mode 取预设自身的值而不是折算值：讲义的四维与笔记相同，只有 mode 能
    // 区分（后端 payload 兼容、模板过滤都依赖它）。
    if (modeSel) modeSel.value = def.mode;
    setRadio('preset', name);
    syncDimsAvailability();
    syncModeUI();
    syncExportTemplateOptions();
  }

  // 用户改动任一维度后：预设高亮按「四维 + 卷头开关是否仍等于该预设」回退，
  // 再刷新折算/禁用矩阵/专属面板/模板过滤。
  function syncFromDimChange() {
    const dims = readDims();
    const preset = getRadio('preset');
    const def = PRESETS[preset];
    if (def && !(dimsEqual(dims, def.dims)
                 && def.stdExam === (stdExamInput?.value === '1'))) {
      setRadio('preset', 'custom');
    }
    syncModeFromDims();
    syncDimsAvailability();
    syncModeUI();
    syncExportTemplateOptions();
  }

  eachRadio('preset', el => {
    el.addEventListener('change', () => {
      if (el.checked) applyPreset(el.value);
    });
  });
  ['layout', 'grouped', 'columns', 'ratio'].forEach(name => {
    eachRadio(name, el => {
      el.addEventListener('change', () => {
        if (!el.checked) return;
        if (name === 'ratio' && el.value === 'wide') {
          // 点选横版 16:9 时直接收敛到它唯一合法的版式（一页一题 + 单栏，
          // 与后端 clamp 一致），其余选项随后被禁用矩阵锁住。
          setRadio('layout', 'one');
          setRadio('columns', '1');
        }
        syncFromDimChange();
      });
    });
  });

  // 从当前四维反推预设高亮（任务回填用）：与某预设四维 + 卷头开关完全一致就
  // 高亮它（讲义/笔记靠知识要点是否非空区分）；否则回退「自定义」并调折算。
  function highlightPresetFromDims() {
    const dims = readDims();
    const stdExam = stdExamInput?.value === '1';
    const keypoints =
      (form.elements['keypoints'] && form.elements['keypoints'].value || '').trim();
    if (dimsEqual(dims, PRESETS.note.dims) && !stdExam) {
      applyPreset(keypoints ? 'handout' : 'note');
      return;
    }
    for (const name of ['exam_std', 'exam', 'lecture', 'slides',
                        'practice', 'list']) {
      const def = PRESETS[name];
      if (dimsEqual(dims, def.dims) && def.stdExam === stdExam) {
        applyPreset(name);
        return;
      }
    }
    setRadio('preset', 'custom');
    syncModeFromDims();
    syncDimsAvailability();
    syncModeUI();
    syncExportTemplateOptions();
  }

  // —— 纸张颜色：radio 视觉组 + hidden 真值（white / cream / #RRGGBB）——
  // radio 的 name=paper_tone_choice 也会随表单提交，但后端只读 paper_tone
  // （hidden），多出的字段直接被忽略——别把两者改成同名，否则两个值会打架。
  function applyTone(value) {
    const isHex = /^#[0-9A-Fa-f]{6}$/.test(value || '');
    const choice = value === 'cream' ? 'cream' : (isHex ? 'custom' : 'white');
    setRadio('paper_tone_choice', choice);
    if (isHex && toneColor) toneColor.value = value;
    // hex 统一大写存储：input[type=color] 的 value 会被浏览器规范化为小写，
    // 不归一的话「同一个颜色」在 payload / 提交里出现两种写法，重导出对拍
    // 与色卡选中比较都要额外兼容。渲染侧（xcolor HTML 模型）大小写不敏感。
    if (paperToneValue) {
      paperToneValue.value = isHex ? value.toUpperCase() : choice;
    }
    if (toneSwatches) toneSwatches.classList.toggle('hidden', choice !== 'custom');
  }
  eachRadio('paper_tone_choice', el => {
    el.addEventListener('change', () => {
      if (!el.checked) return;
      // 切到「自定义」时立刻采用色卡当前颜色（不提交 "custom" 这个字面值，
      // 后端白名单只认 white/cream/#RRGGBB）。
      applyTone(el.value === 'custom'
        ? ((toneColor && toneColor.value) || '#ADD8E6')
        : el.value);
    });
  });
  toneColor?.addEventListener('input', () => applyTone(toneColor.value));
  toneSwatches?.addEventListener('click', event => {
    const swatch = event.target.closest('.tone-swatch');
    if (swatch) applyTone(swatch.dataset.tone);
  });

  // —— 字体预览 ——
  // 下拉直接作用到预览行的 font-family（浏览器用本机同名系统字体渲染）。
  // FandolSong 是 TeX 分发字体、本机没有同名系统字形，直接用宋体近似——
  // 别让它落到后面的微软雅黑（黑体，与导出 PDF 的宋体观感差得远）。
  function syncExportFontPreviews() {
    if (cjkFontPreview) {
      cjkFontPreview.style.fontFamily = cjkFontSel?.value === 'FandolSong'
        ? '"SimSun", "Microsoft YaHei", serif'
        : `"${cjkFontSel?.value || 'Noto Sans SC'}", "Microsoft YaHei", serif`;
    }
    if (latinFontPreview) {
      latinFontPreview.style.fontFamily =
        `"${latinFontSel?.value || 'Latin Modern Roman'}", "Times New Roman", serif`;
    }
  }
  cjkFontSel?.addEventListener('change', syncExportFontPreviews);
  latinFontSel?.addEventListener('change', syncExportFontPreviews);

  // —— 格式联动（Word 禁用 PDF/TeX 专属项）——
  // Word 以可编辑语义为目标；底色打印行为和现有 PDF 标志资源在 Office 中不
  // 稳定。禁用控件可避免把 PDF 专属参数静默传给后端，切回其他格式时原选择
  // 仍保留。纸色区含按钮（色卡）与 hidden 真值，一并禁用；hidden 不提交时
  // 后端白名单默认白色——Word 拒绝非白底色，两边行为一致。
  function syncExportFormatUI() {
    const isWord = !!exportFormatSel && exportFormatSel.value === 'docx';
    [paperToneField, wimathLogoField].forEach(field => {
      if (!field) return;
      field.querySelectorAll('input, select, button').forEach(control => {
        control.disabled = isWord;
      });
    });
    if (exportTemplateSel) exportTemplateSel.disabled = isWord;
    if (exportTemplateField) {
      exportTemplateField.classList.toggle('is-disabled', isWord);
    }
    if (wordExportHint) wordExportHint.hidden = !isWord;
  }
  exportFormatSel?.addEventListener('change', syncExportFormatUI);

  // —— 模板下拉按当前 mode 过滤 ——
  // data-supported-modes 声明的就是模板契约的兼容模式（template_pipeline
  // 的 8 值枚举）；modeSel 的值是四维折算值，与后端 _resolve_template_path
  // 的门禁口径一致（后端用 spec.template_mode，同一折算）。
  function syncExportTemplateOptions() {
    if (!exportTemplateSel) return;
    const mode = modeSel?.value || 'exam';
    Array.from(exportTemplateSel.options).forEach((option, index) => {
      if (index === 0) return;
      const supported = String(option.dataset.supportedModes || '')
        .split(',').map(value => value.trim()).filter(Boolean);
      option.hidden = supported.length > 0 && !supported.includes(mode);
      option.disabled = option.hidden;
    });
    if (exportTemplateSel.selectedOptions[0]?.hidden) exportTemplateSel.value = '';
    window.QFSelect?.refresh(exportTemplateSel);
  }

  // —— 真实 PDF 预览 / 导出 ——
  // 预览是「POST 拿一个取件地址，再由地址去取文件」，不直接收文件流。原因是
  // 本页会被 Obsidian 插件嵌在 iframe 里（Electron 渲染进程），那里 blob: URL
  // 赋给 iframe 的 src 永远显示空白；换成普通 http 地址后预览就是同源 iframe
  // 的一次正常加载。导出则不再当场取件——提交登记为后台任务。
  const previewBtn = document.getElementById('preview-btn');
  const overlay = document.getElementById('preview-overlay');
  const frame = document.getElementById('preview-frame');
  const previewTitle = document.getElementById('preview-title');
  const previewInfo = document.getElementById('preview-info');
  const previewOpen = document.getElementById('preview-open');

  function closePreview() {
    overlay.classList.add('hidden');
    frame.src = 'about:blank';
    if (previewOpen) { previewOpen.style.display = 'none'; previewOpen.href = '#'; }
  }

  if (previewBtn) {
    previewBtn.addEventListener('click', async () => {
      const fd = new FormData(form);
      // 预览固定不受导出格式下拉影响；HTML 近似预览不启动 TeX。
      fd.set('fmt', 'pdf');
      const previewKind = document.getElementById('preview-kind')?.value || 'pdf';
      fd.set('preview_kind', previewKind);
      const previewLabel = previewKind === 'html' ? 'HTML/KaTeX 近似预览' : 'PDF 预览';
      previewTitle.textContent = previewLabel;
      frame.title = previewLabel;
      previewInfo.textContent =
        previewKind === 'html' ? '正在生成 HTML/KaTeX 预览…' : '正在生成 PDF…';
      overlay.classList.remove('hidden');
      frame.src = 'about:blank';
      try {
        const data = await postForExport('/preview', fd);
        frame.src = data.url;
        if (previewOpen) {
          previewOpen.href = data.url;
          previewOpen.style.display = 'inline-block';
        }
        previewInfo.textContent = previewKind === 'html'
          ? 'HTML/KaTeX 近似预览（不等同 PDF 分页）'
          : '所见即所得（与导出一致）';
      } catch (e) {
        previewInfo.textContent = '';
        alert('预览失败：' + e.message);
        closePreview();
      }
    });
    document.getElementById('preview-close').addEventListener('click', closePreview);
    overlay.addEventListener('click', e => { if (e.target === overlay) closePreview(); });
  }

  // 导出：接管表单提交 —— 提交只登记后台任务（编译交给「导出任务」面板），
  // 立即把焦点还给题库。连点几次导出会按导出并发槽排队，用户可以继续选题、
  // 整理别的卷子；产出后从左侧「导出任务」面板打开 PDF / 打开文件夹 / 下载。
  form.addEventListener('submit', async e => {
    e.preventDefault();
    const btn = form.querySelector('button[type="submit"]');
    const label = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.textContent = '正在创建任务…'; }
    try {
      await postForExport('/export', new FormData(form));
      notify('已创建导出任务，进度见左侧「导出任务」面板');
    } catch (err) {
      alert('导出失败：' + err.message);
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = label; }
    }
  });

  // 「修改配置重新导出」：导出任务面板跳转时携带 ?export_task=<id>，这里把原
  // 任务的完整配置回填到表单。pinned_ids 用隐藏域固定当初那批题目——回填时
  // 用户的选题篮可能已经完全变了，重导出必须还是原来那批题。
  async function prefillExportFromTask() {
    const taskId = new URL(window.location.href).searchParams.get('export_task');
    if (!taskId) return;
    let payload;
    try {
      const res = await fetch('/export-tasks/' + encodeURIComponent(taskId) + '/config');
      const data = await res.json();
      if (!res.ok || !data.ok) throw new Error(data.error || '读取配置失败');
      payload = data.payload || {};
    } catch (err) {
      notify('读取导出配置失败：' + err.message, 'err');
      return;
    }
    const setValue = (name, value) => {
      if (value === undefined || value === null) return;
      const el = form.elements[name];
      if (el && typeof el.value !== 'undefined') el.value = String(value);
    };
    const check = (name, value) => {
      const el = form.elements[name];
      if (el && typeof el.checked !== 'undefined') el.checked = !!value;
    };
    // 先设 mode 再设模板：模板下拉按模式过滤选项，顺序反了兼容模板会被清掉。
    setValue('mode', payload.mode);
    setValue('tags', (payload.tags || []).join(','));
    setValue('match', payload.match);
    setValue('type', payload.type);
    // fmt='tex' 是旧版下拉里的一档（新 UI 只留 PDF / Word / LaTeX.zip）：
    // 回填时补一个临时 option，保证「修改配置重导出」与原任务格式一致。
    if (payload.fmt === 'tex' && exportFormatSel
        && !exportFormatSel.querySelector('option[value="tex"]')) {
      const opt = document.createElement('option');
      opt.value = 'tex';
      opt.textContent = 'LaTeX 源码（.tex，旧任务）';
      exportFormatSel.appendChild(opt);
      window.QFSelect?.refresh(exportFormatSel);
    }
    ['fmt', 'title', 'keypoints', 'cjk_font', 'latin_font', 'tex_backend'
    ].forEach(name => setValue(name, payload[name]));
    setRadio('solution_mode', payload.solution_mode || 'none');
    check('wimath_logo', payload.wimath_logo);
    check('show_source', payload.show_source);
    // 四维与卷头开关：新 payload 直接回填（mode 由折算恢复——提交后后端总按
    // 四维重算，payload 里的 mode='list' 只是归一占位）；旧 payload（导出抽屉
    // 之前的任务）只有 mode，按预设表反推四维，mode 保留原字面值。
    if (payload.layout) {
      setRadio('layout', payload.layout);
      setRadio('grouped', String(payload.grouped ?? '1'));
      setRadio('columns', String(payload.columns ?? '1'));
      setRadio('ratio', payload.ratio || 'a4');
      if (stdExamInput) stdExamInput.value = payload.std_exam ? '1' : '';
      highlightPresetFromDims();
    } else {
      const def = PRESETS[payload.mode];
      if (def) {
        setRadio('layout', def.dims.layout);
        setRadio('grouped', def.dims.grouped);
        setRadio('columns', def.dims.columns);
        setRadio('ratio', def.dims.ratio);
        if (stdExamInput) stdExamInput.value = def.stdExam ? '1' : '';
      }
      setRadio('preset', def ? payload.mode : 'custom');
      syncDimsAvailability();
      syncModeUI();
      syncExportTemplateOptions();
    }
    applyTone(payload.paper_tone || 'white');
    const hf = payload.header_footer || {};
    Object.keys(hf).forEach(name => setValue(name, hf[name]));
    const so = payload.std_opts || {};
    setValue('subject', so.subject);
    check('info_bar', so.info_bar);
    setValue('secret_notice', so.secret_notice);
    setValue('exam_notes', so.exam_notes);
    const points = so.section_points || {};
    setValue('points_single', points.single);
    setValue('points_multi', points.multi);
    setValue('points_blank', points.blank);
    setValue('points_solve', points.solve);
    // 固定当初那批题目（pinned_ids 只在 scope=selected 时使用；页面无 scope
    // 控件后恒为后端默认 selected，重导出必是原批次）。
    form.querySelectorAll('input[name="pinned_ids"]').forEach(el => el.remove());
    (payload.pinned_ids || []).forEach(id => {
      const hidden = document.createElement('input');
      hidden.type = 'hidden';
      hidden.name = 'pinned_ids';
      hidden.value = id;
      form.appendChild(hidden);
    });
    setValue('template_id', payload.template_id);
    // 手动过一遍各联动：模板兼容过滤（含 template_id 选中项的保留/清退）、
    // Word 禁用项、字体预览、专属面板（知识要点 / 标准试卷说明）。
    syncModeUI();
    syncExportTemplateOptions();
    syncExportFormatUI();
    syncExportFontPreviews();
    setExportPanelOpen(true);
    notify('已载入任务配置，确认后点「导出」重新生成');
    // 参数只用于一次性回填；留在地址栏会让之后每次刷新都跳回同一个旧任务。
    const url = new URL(window.location.href);
    url.searchParams.delete('export_task');
    history.replaceState(null, '', url.toString());
  }

  // —— 初始化：先刷联动，再尝试任务配置回填（回填内部会再刷一遍受影响项）——
  syncDimsAvailability();
  syncModeFromDims();
  syncModeUI();
  syncExportTemplateOptions();
  syncExportFormatUI();
  syncExportFontPreviews();
  void prefillExportFromTask();

  // 题卡是 AJAX 动态替换的（inline 编辑 / 新建 / 无限滚动 / 文件夹切换），
  // 内联脚本在这些调用点重跑 syncModeUI 刷新新卡上「独占整页」的显隐；总览
  // 视图切换会收起导出配置。挂 window 保持这些既有调用点零改动。
  window.syncModeUI = syncModeUI;
  window.setExportPanelOpen = setExportPanelOpen;
})();
