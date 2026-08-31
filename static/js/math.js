// 公式渲染：KaTeX（2026-08-04 起；此前是 MathJax 3 tex-svg-full）
//
// 为什么换：同一份题库语料（4459 段公式，含 63 道真题 + 规范化 md 产物）实测
// 两者渲染结果都是 4459/4459 全成功，但单公式耗时 MathJax 1198µs vs KaTeX 117µs
// （约 10 倍），主脚本 2.0MB vs 277KB。列表页一屏几十道题、满页上千个公式时，
// MathJax 那几秒是用户能直接感觉到的「整个题库在重新渲染」。
//
// 换之前扫过语料里全部 70 个命令：`\displaystyle`/`\dfrac`/`\sqrt`/`\left\right`/
// `\overrightarrow`/`\atop`/`\pmb`/`\tag`/`\textcircled` 以及 array/cases/aligned
// 三个环境 KaTeX 全支持。base.html 原注释担心的 `\color`/`\cancel`/`\mhchem`
// 在语料里出现 0 次，所以当初为它们选 full 版的理由这里不适用；真要用 mhchem，
// KaTeX 也有 contrib/mhchem.min.js 可以再补。
//
// 唯一暴露的全局是 QMath（对齐 setupEmojiContextMenu 的例外约定）：局部重排的
// 调用方（image-layout.js 换正文、edit.html / post-new.js 实时预览）都走它，
// 别在别处直接调 renderMathInElement —— 定界符和 macros 只应该有一份配置。
(function () {
  // 定界符顺序不能反：auto-render 是顺序匹配的，`$` 排在 `$$` 前面会把 `$$x$$`
  // 拆成「空行内公式 + x + 空行内公式」。
  const DELIMS = [
    { left: '$$', right: '$$', display: true },
    { left: '$', right: '$', display: false },
  ];

  const OPTS = {
    delimiters: DELIMS,
    // 单条公式写错只把那一条标红，不要中断整个容器的渲染——题库正文来自 OCR+AI
    // 规范化，偶发不合法的片段是常态，为它牺牲同一张卡里其余公式不值得。
    throwOnError: false,
    // strict:false 放过 KaTeX 认为「可疑但能渲」的输入（如中文直接出现在数学区）。
    // 语料里这类片段不少，strict 默认的 'warn' 只是刷控制台，不如显式关掉。
    strict: false,
    // 闭曲面/闭体积分。MathJax 版本用的是 `\unicode{x222F}`，KaTeX 没有 \unicode，
    // 改成直接写字符——KaTeX 本身就认 ∯/∰ 是数学模式下的算符。
    // 别再套一层 \text{}：那两个字符在 KaTeX 里是 function，进 text 模式直接报
    // "Can't use function '∯' in text mode"。
    macros: {
      '\\oiint': '\\mathop{∯}',    // 闭曲面积分
      '\\oiiint': '\\mathop{∰}',   // 闭合体积分
      // 别在这里把 \dfrac/\frac/\cfrac 映射到 \tfrac 去「治分数太大」。试过一版，
      // 撤了：真题的排法正相反——2018–2024 全国卷/新课标真题里题干和选项的分数
      // 一律是 display 尺寸的大分数，没有「选项用小分数」这回事。真卷不显得挤是
      // 靠行距，不是靠小字号，所以改的是 exam_template.tex 里选项的 after-item-skip，
      // 公式尺寸两侧都不动（网页与 PDF 必须一致，否则所见非所得）。详见该文件开头。
    },
    // 正文容器里可能嵌 <code>/<pre>（题干贴代码），别把里面的 $ 当公式。
    ignoredTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code', 'option'],
  };

  const preferredOptionColumns = new WeakMap();

  function normalizeOptionColumns(value) {
    const cols = Number(value);
    return cols === 4 || cols === 2 ? cols : 1;
  }

  function optionIntrinsicWidth(option) {
    const previous = {
      whiteSpace: option.style.whiteSpace,
      wordBreak: option.style.wordBreak,
      overflowX: option.style.overflowX,
    };
    option.style.whiteSpace = 'nowrap';
    option.style.wordBreak = 'normal';
    option.style.overflowX = 'visible';
    const width = option.scrollWidth;
    Object.assign(option.style, previous);
    return width;
  }

  function fitOptionGrid(grid) {
    if (!preferredOptionColumns.has(grid)) {
      preferredOptionColumns.set(grid, normalizeOptionColumns(grid.dataset.cols));
    }
    if (grid.clientWidth <= 0) return;

    let cols = preferredOptionColumns.get(grid);
    grid.dataset.cols = String(cols);
    while (cols > 1) {
      const overflow = [...grid.querySelectorAll('.q-opt')]
        .some(option => optionIntrinsicWidth(option) > option.clientWidth + 1);
      if (!overflow) break;
      cols = cols === 4 ? 2 : 1;
      grid.dataset.cols = String(cols);
      // 下一轮读取尺寸会触发布局刷新，再检查降列后的真实结果。
    }
  }

  const optionResizeObserver = typeof window.ResizeObserver === 'function'
    ? new window.ResizeObserver(entries => entries.forEach(entry => fitOptionGrid(entry.target)))
    : null;

  function optionGridsIn(node) {
    if (!node || node.nodeType !== 1) return [];
    const grids = [...node.querySelectorAll('.q-opts')];
    if (node.matches('.q-opts')) grids.unshift(node);
    return grids;
  }

  function fitOptionColumns(scope) {
    // 服务端先按公式源码宽度选首选列数；KaTeX 完成后按当前容器真实宽度复核。
    // 临时禁用换行才能量到选项固有宽度，否则换行后的 scrollWidth 会掩盖拥挤。
    const root = scope || document;
    optionGridsIn(root).forEach(grid => {
      fitOptionGrid(grid);
      if (optionResizeObserver) optionResizeObserver.observe(grid);
    });
  }

  function typeset(el) {
    if (!el || !window.renderMathInElement) return;
    try {
      window.renderMathInElement(el, OPTS);
    } catch (e) {
      // 走到这里说明是定界符切分层面的错误（不是单条公式的语法错误，那个被
      // throwOnError:false 兜住了）。吞掉：正文已经在 DOM 里，源码可读总比整页挂掉好。
      console.error(e);
    }
    fitOptionColumns(el);
  }

  window.QMath = { typeset: typeset, fitOptionColumns: fitOptionColumns };

  // 首屏：KaTeX 是同步的，直接排完整篇再摘 .math-pending（见 style.css）。
  // 本文件用 defer 引入，跑到这里 DOM 已完整，不必再等 DOMContentLoaded。
  typeset(document.body);
  if (optionResizeObserver && typeof window.MutationObserver === 'function') {
    new window.MutationObserver(records => records.forEach(record => {
      record.removedNodes.forEach(node => {
        optionGridsIn(node).forEach(grid => optionResizeObserver.unobserve(grid));
      });
      record.addedNodes.forEach(node => {
        if (node.isConnected) fitOptionColumns(node);
      });
    })).observe(document.body, {childList: true, subtree: true});
  }
  document.documentElement.classList.remove('math-pending');
})();
