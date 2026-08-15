// 题目图片排版控制条（index.html 里含图题目才渲染 .img-layout-bar）：
// 对齐 chips、宽度拖拽手柄、图文分栏 chips。三者都直接 POST 到 /question/<id>/xxx。
//
// 多图：正文里每张图都套一个 .qimg-resizable + 自己的手柄，逐图独立设宽度/对齐，
// 序号 idx = `.body img` 的出现顺序，与后端 img_layouts 里的 i、导出时
// exporter._extract_mark 返回列表的下标一一对应（三者同源于正文图片引用顺序）。
// 控制条只有一套 chips 和一个宽度读数，多图时它作用于「当前选中的那张」——点正文
// 里的图即切换选中，条上显示「第 n/N 张」。
//
// 注意：这里改的是「导出到 PDF 时的排版」，页面上的显示只是所见即所得的近似。
// 改完必须去 /preview 验证 PDF —— 页面看着变了但导出没变过，说明浏览器缓存了旧
// HTML（app.py 的 _no_cache_html 就是为此加的）。
//
// 正文结构变了怎么办（分栏切换、AI 重绘替换图片）：后端把 qrender 渲好的
// body_html 一起回来，前端换掉 .body 再重跑一遍包盒/手柄初始化，**不整页
// reload**。这两处原先都是 reload，代价是把同页其它题正在跑的重绘轮询一起
// 杀掉，于是多张图没法同时重绘（见 image-redraw.js 顶部）。所以本模块把
// 「按 bar 初始化」拆成了可重入的 bindBody，并暴露唯一的全局符号 QImgLayout
// （全站第二个全局，另一个是 setupEmojiContextMenu）。
(function () {
  // 未自定义宽度时的默认占比，与 exporter._DEFAULT_IMG_FRAC 保持一致
  const DEFAULT_IMG_W = 35;

  // bar 元素 -> 该条的状态。chips 的事件只绑一次、闭包里现读这个对象，
  // 所以正文重绑时不会给 chips 叠第二份监听（叠上去一次点击会发两个请求）。
  const states = new WeakMap();

  // 把 data-layouts（img_layouts 的 JSON 串）解析成 idx -> {w, align}。
  // 老题该列为空，退回 data-width/data-align 当首图设置——与后端
  // exporter._layout_at 的兜底逻辑对称。
  function parseLayouts(bar) {
    const out = {};
    const raw = (bar.dataset.layouts || '').trim();
    if (raw) {
      try {
        (JSON.parse(raw) || []).forEach(it => {
          const i = parseInt(it.i, 10);
          if (Number.isFinite(i) && i >= 0) out[i] = {w: it.w, align: it.align || ''};
        });
      } catch (_) { /* 脏数据当作没设置，别让整条控件挂掉 */ }
    }
    if (!(0 in out)) {
      const w = parseInt(bar.dataset.width, 10);          // '' → NaN（未自定义）
      out[0] = {w: Number.isFinite(w) ? w : null, align: bar.dataset.align || ''};
    }
    return out;
  }
  // 图文分栏两列占比：与 exporter._split_fracs 同一条公式（图列夹在 [0.1,0.7]，
  // 文列取 0.96 减去图列，余 4% 给列间距）。**这是唯一一处刻意重复的规则**——
  // 拖动要在松手前就实时改两列宽度，没法等后端回包；选项切分/列数那两套复杂启发式
  // 仍只在 Python 里（exporter.split_choice_options / choice_cols）。
  // 改这里必须同步 exporter._split_fracs，反之亦然。
  function splitFracs(width) {
    const w = parseInt(width, 10);
    if (!Number.isFinite(w)) return [0.48, 0.48];
    const img = Math.min(0.7, Math.max(0.1, w / 100));
    return [Math.round((0.96 - img) * 1e4) / 1e4, img];
  }

  // data-groups（连续两图的分组，见 app.py qfig_groups / qrender.fig_groups）→
  // [{ids: [1, 2], row: true}]。分组规则**不在这里重算**：相邻判定要看正文里两个
  // 图引用之间有没有别的文字，那是 exporter.plan_figs 的活，前端只消费结果。
  function parseGroups(bar) {
    const raw = (bar.dataset.groups || '').trim();
    if (!raw) return [];
    try {
      return (JSON.parse(raw) || [])
        .map(g => ({ids: (g.ids || []).map(Number), row: !!g.row}))
        .filter(g => g.ids.length > 1 && g.ids.every(Number.isFinite));
    } catch (_) { return []; }        // 脏数据当作没有分组，别让整条控件挂掉
  }

  const groupOf = (st, i) => st.groups.find(g => g.ids.includes(i)) || null;

  function post(id, path, payload) {
    return fetch(`/question/${id}/${path}`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    }).then(res => res.json());
  }

  // 控制条作用的正文容器：题干版是 .body，解析版（data-field="solution"）是
  // .solution-body —— 两者结构同源（都出自 qrender，见 app.py 的 qbody/qsolution），
  // 只是挂在题卡里的不同位置，故除了选哪个容器，其余逐图逻辑完全复用。
  function bodyElOf(bar) {
    const card = bar.closest('.card');
    if (!card) return null;
    return bar.dataset.field === 'solution'
      ? card.querySelector('.solution-body')
      : card.querySelector('.body');
  }

  // 按当前正文容器里的图重建包盒与手柄。正文被换掉后（分栏切换 / 重绘替换）
  // 再调一次即可 —— 新 DOM 里没有旧监听，不存在重复绑定。
  // 返回 false 表示这题正文里已经没有图了（重绘不会导致这种情况，但分栏切换后
  // 结构变了要能安全退出）。
  function bindBody(bar) {
    const st = states.get(bar);
    if (!st) return false;
    const body = bodyElOf(bar);
    const imgs = body ? Array.from(body.querySelectorAll('img')) : [];
    if (!imgs.length) return false;

    st.body = body;
    st.imgs = imgs;
    st.boxes = [];
    // 分栏渲染下的首图（qrender._choice_split_html 的右栏）：拖动改的是两列占比，
    // 不是图片自身宽度，故单独认出来。正文重渲后分栏可能开也可能关，每次现读。
    // 解析的“图文混排”用 float，不再有左右等高列；但缩放手柄
    // 仍应改浮图占整个解析区的比例。把它视为另一种 splitCol，后面的
    // 宽度换算、多图组内宽度和存盘逻辑即可完整复用。
    st.splitCol = body.querySelector('.q-split-img, .q-solution-flow-img');
    st.splitTxt = body.querySelector('.q-split-text');
    if (st.sel >= imgs.length) st.sel = 0;   // 图变少了（正文被改过）别留越界下标

    imgs.forEach((img, idx) => bindOne(bar, st, img, idx));
    syncBar(bar);
    return true;
  }

  // 控制条同步到当前选中图：宽度读数、宽度是否自定义、对齐 chips 高亮、第几张
  function syncBar(bar) {
    const st = states.get(bar);
    if (!st) return;
    const {sel, imgs, boxes} = st;
    if (st.valueEl) st.valueEl.textContent = widthOf(st, sel) + '%';
    if (st.wrap) st.wrap.classList.toggle('active', isCustom(st, sel));
    bar.querySelectorAll('.img-width-chip').forEach(chip => {
      chip.classList.toggle('active', Number(chip.dataset.width) === widthOf(st, sel));
    });
    bar.querySelectorAll('.img-align-chip').forEach(c => {
      c.classList.toggle('active', c.dataset.align === alignOf(st, sel));
    });
    if (imgs.length > 1 && st.pickEl) {
      st.pickEl.hidden = false;
      st.pickEl.textContent = `第 ${sel + 1}/${imgs.length} 张`;
    } else if (st.pickEl) {
      st.pickEl.hidden = true;
    }
    // 并排/堆叠 chip：只在选中图属于某个「连续两图」组时露出来。单图组切不了，
    // 显示一个点了必然被后端拒的按钮没有意义（同 img_stack 路由的前置校验）。
    if (st.stackEl) {
      const g = groupOf(st, sel);
      st.stackEl.hidden = !g;
      if (g) {
        // 文案说的是「点下去会变成什么」：并排时提示改堆叠，反之亦然。
        // .on 表示「当前是堆叠」——默认态是并排，亮起来才代表用户改过。
        st.stackEl.textContent = g.row ? '改为上下排列' : '改为并排';
        st.stackEl.classList.toggle('on', !g.row);
      }
    }
    boxes.forEach((b, i) => b.classList.toggle('picked', imgs.length > 1 && i === sel));
  }

  const at = (st, i) => (st.layouts[i] = st.layouts[i] || {w: null, align: ''});
  const rawW = (st, i) => parseInt(at(st, i).w, 10);
  const isCustom = (st, i) => Number.isFinite(rawW(st, i));
  const widthOf = (st, i) => (isCustom(st, i) ? rawW(st, i) : DEFAULT_IMG_W);
  // 解析混排浮动的是一个完整视觉组，后端/qrender 以组首图的 align 决定整组
  // 左右方向。用户即使先点了组内第二张图，对齐操作也必须归一到组首图；否则
  // 当前 DOM 会暂时变向，但只持久化第二张图，刷新后又按首图恢复旧方向。
  function alignTarget(st, i) {
    const col = st.splitCol;
    if (!col?.classList.contains('q-solution-flow-img')
        || !st.imgs[i] || !col.contains(st.imgs[i])) return i;
    const lead = Number(col.dataset.splitLead);
    return Number.isInteger(lead) && lead >= 0 && lead < st.imgs.length ? lead : i;
  }
  const alignOf = (st, i) => at(st, alignTarget(st, i)).align || '';
  const clamp = v => Math.max(10, Math.min(100, Math.round(v)));

  // 非分栏单图由 qrender 输出 .q-fig-left/center/right。对齐接口只回元数据，
  // 不会重渲正文；点击 chip 后若只改内存状态与高亮，图片容器仍保留旧 class，
  // 用户看到的就是“左/中/右按钮无效”。这里局部同步 class，空值与后端一致回中。
  function applyAlign(st, i) {
    const fig = st.imgs[i]?.closest('.q-fig');
    if (!fig) {
      // 解析图文混排不再用 .q-fig，而是整组图片浮在文字一侧。对齐接口只更新
      // 元数据、不回渲题卡，所以这里必须同步浮动方向；居中沿用混排的右图语义。
      const flow = st.imgs[i]?.closest('.q-solution-flow');
      if (flow) {
        const side = alignOf(st, i) === 'left' ? 'left' : 'right';
        flow.classList.remove('q-solution-flow-left', 'q-solution-flow-right');
        flow.classList.add(`q-solution-flow-${side}`);
      }
      return;  // 其余分栏、多图行的位置由各自容器决定，不在这里强改
    }
    fig.classList.remove('q-fig-left', 'q-fig-center', 'q-fig-right');
    fig.classList.add(`q-fig-${alignOf(st, i) || 'center'}`);
  }

  // 单张图：包盒 + 手柄 + 拖拽。从 bindBody 里拆出来只为让那边短一点，
  // 逻辑与原先逐图那段一字不差。
  function bindOne(bar, st, img, idx) {
    const {body, splitCol, splitTxt} = st;
    // 题干硬分栏与解析浮动混排都由 splitCol 承载；只查 .q-split-img 会漏掉后者，
    // 让 35% 的图片在 35% 浮动盒内再缩一次，实际只剩 12.25%。
    const splitMember = !!splitCol && splitCol.contains(img);
    const flowSplit = !!splitCol?.classList.contains('q-solution-flow-img');
    const groupedCell = img.closest('.q-fig-cell');
    const splitLead = splitCol ? Number(splitCol.dataset.splitLead) : -1;
    const splitUnitCount = splitCol ? Number(splitCol.dataset.unitCount || 1) : 0;
    const box = document.createElement('span');
    box.className = 'qimg-resizable';
    img.parentNode.insertBefore(box, img);
    box.appendChild(img);
    const handle = document.createElement('span');
    handle.className = 'qimg-handle';
    handle.title = '拖动等比缩放';
    box.appendChild(handle);
    st.boxes.push(box);
    // 宽度归包裹盒（.qimg-resizable img 是 width:100%）。qrender 已在 <img> 上写了
    // 行内 width，若留着就成了「盒子的 35% 里再取 35%」——图片会缩成一小块。
    const unitWidth = parseFloat(img.dataset.unitWidth);
    img.style.width = '';
    // 普通横排的 cell 已经承载图片宽度；分栏视觉组里的宽度是“组内相对比例”。
    // 这两种情况盒子都不能再套一次数据库里的正文百分比，否则图片会二次缩小。
    box.style.width = groupedCell ? '100%'
      : (splitMember && Number.isFinite(unitWidth) ? unitWidth + '%'
        : (splitMember ? '100%' : widthOf(st, idx) + '%'));

    // 点图片（不含手柄）= 把控制条切到这张
    box.addEventListener('click', e => {
      if (e.target === handle) return;
      st.sel = idx;
      syncBar(bar);
    });

    bindDrag(bar, st, box, img, handle, idx);

    // 分栏右栏的首图：拖动改的是「两列占比」，不是图片自身宽度。
    // 换算基准必须取 .body 而非 box.parentNode —— 后者就是 .q-split-img，
    // 它的宽度正随拖动变化，拿它当基准会自我反馈、越拖越飘。
    // 单图分栏仍沿用“拖图片即改栏宽”；多图视觉组则拖各自图片宽度，松手后由后端
    // 重算组宽和整栏宽，避免横排组把第一张的宽度误当成整个右栏宽度。
    const isSplit = splitMember && splitUnitCount === 1 && idx === splitLead;
    const isGrouped = !!groupedCell || (splitMember && splitUnitCount > 1);
    let dragging = false, startX = 0, startW = widthOf(st, idx), baseW = 1;
    handle.addEventListener('pointerdown', e => {
      e.preventDefault();
      dragging = true;
      st.sel = idx;                                       // 拖哪张就选中哪张
      syncBar(bar);
      startX = e.clientX;
      baseW = (isSplit || isGrouped ? body.offsetWidth
                                    : box.parentNode.offsetWidth) || 1;
      startW = isSplit ? splitCol.offsetWidth / baseW * 100
                       : (isGrouped
                         ? (groupedCell || box).offsetWidth / baseW * 100
                         : box.offsetWidth / baseW * 100);
      handle.setPointerCapture(e.pointerId);
    });
    handle.addEventListener('pointermove', e => {
      if (!dragging) return;
      const pct = clamp(startW + (e.clientX - startX) / baseW * 100);
      if (isSplit) {
        // 硬分栏同步改两列；混排只有浮图宽度，正文会由浏览器自动环绕并在图下
        // 恢复整行，因此不能给浮图写无效的 flex-basis。
        const [txt, img2] = splitFracs(pct);
        if (flowSplit) splitCol.style.width = `${(img2 * 100).toFixed(1)}%`;
        else {
          splitCol.style.flex = `0 0 ${(img2 * 100).toFixed(1)}%`;
          if (splitTxt) splitTxt.style.flex = `0 0 ${(txt * 100).toFixed(1)}%`;
        }
      } else if (isGrouped) {
        if (splitMember) {
          const colPct = splitCol.offsetWidth / (body.offsetWidth || 1) * 100 || 1;
          const rel = Math.min(100, pct / colPct * 100);
          if (groupedCell) groupedCell.style.flex = `0 0 ${rel}%`;
          else box.style.width = rel + '%';
        } else if (groupedCell) {
          groupedCell.style.flex = `0 0 ${pct}%`;
        }
      } else {
        box.style.width = pct + '%';
      }
      st.valueEl.textContent = pct + '%';
    });
    const finish = async e => {
      if (!dragging) return;
      dragging = false;
      try { handle.releasePointerCapture(e.pointerId); } catch (_) {}
      const pct = clamp(isSplit
        ? splitCol.offsetWidth / (body.offsetWidth || 1) * 100
        : (isGrouped
          ? (groupedCell || box).offsetWidth / (body.offsetWidth || 1) * 100
          : box.offsetWidth / box.parentNode.offsetWidth * 100));
      const data = await post(st.id, 'img_width',
        {width: String(pct), index: idx, field: st.field});
      if (data.ok) {
        at(st, idx).w = pct;
        applyResult(bar, data);
      }
    };
    handle.addEventListener('pointerup', finish);
    handle.addEventListener('pointercancel', finish);
  }

  // 多图拖动只负责交换顺序；上下/左右方向由控制条显式选择。
  //
  // 用 pointer events 而不是 HTML5 drag：`.card` 上有 draggable="true"、`#q-list`
  // 委托着题卡拖动排序（folder-tree.js），HTML5 的 dragstart 会冒上去被当成
  // 「拖题目换顺序」。pointerdown 里 preventDefault + <img draggable="false">
  // 两道一起用，才能既拦住原生图片拖拽、又不惊动题卡排序。
  //
  // 横排沿左右拖、竖排沿上下拖，松手后与相邻图片交换；任意数量图片均适用。
  const DRAG_MIN = 8;             // 主导轴位移不到这么多像素当作点击，不发请求

  function bindDrag(bar, st, box, img, handle, idx) {
    const group = groupOf(st, idx);
    if (!group) return;
    box.classList.add('qimg-draggable');
    img.draggable = false;

    let dragging = false, x0 = 0, y0 = 0, dx = 0, dy = 0, peerIdx = null;

    const peerBox = () => (peerIdx === null ? null : st.boxes[peerIdx]);
    const clearHint = () => {
      box.classList.remove('dragging', 'drag-x', 'drag-y', 'drag-y-row');
      peerBox()?.classList.remove('drag-peer');
    };

    box.addEventListener('pointerdown', e => {
      if (e.target === handle) return;         // 手柄是缩放，不是拖动
      if (e.button !== 0) return;
      e.preventDefault();                      // 拦原生图片拖拽 + 题卡 dragstart
      dragging = true;
      dx = dy = 0;
      peerIdx = null;
      x0 = e.clientX;
      y0 = e.clientY;
      st.sel = idx;
      syncBar(bar);
      box.setPointerCapture(e.pointerId);
      box.classList.add('dragging');
    });

    box.addEventListener('pointermove', e => {
      if (!dragging) return;
      dx = e.clientX - x0;
      dy = e.clientY - y0;
      const g = groupOf(st, idx) || group;
      const delta = g.row ? dx : dy;
      const pos = g.ids.indexOf(idx);
      const nextPos = pos + (delta < 0 ? -1 : 1);
      const oldPeer = peerIdx;
      peerIdx = Math.abs(delta) >= DRAG_MIN && nextPos >= 0 && nextPos < g.ids.length
        ? g.ids[nextPos] : null;
      if (oldPeer !== peerIdx && oldPeer !== null) st.boxes[oldPeer]?.classList.remove('drag-peer');
      box.classList.toggle('drag-x', g.row && peerIdx !== null);
      box.classList.toggle('drag-y', !g.row && peerIdx !== null);
      peerBox()?.classList.add('drag-peer');
    });

    const finish = async e => {
      if (!dragging) return;
      dragging = false;
      try { box.releasePointerCapture(e.pointerId); } catch (_) {}
      const target = peerIdx;
      clearHint();
      peerIdx = null;
      if (target === null) return;              // 没拖够或已经到边界
      const data = await post(st.id, 'img_swap',
                              {index: idx, with: target, field: st.field});
      // 失败要出声：这两个开关唯一的可见反馈就是版式变没变，静默 return 与
      // 「点了没反应」无法区分（同 img_split chip 那段注释）。
      if (!data.ok) {
        if (data.error) alert(data.error);
        return;
      }
      applyResult(bar, data);
    };
    box.addEventListener('pointerup', finish);
    box.addEventListener('pointercancel', finish);
  }

  // 后端回包的公共善后：刷新分组、换正文、重绑。
  // 分组必须用后端回的那份：交换/堆叠都可能改变分组本身（交换后序号换了、堆叠改的
  // 是 row），前端自己推会与 exporter.plan_figs 漂移 —— 分组规则只有 Python 那一份。
  function applyResult(bar, data) {
    const st = states.get(bar);
    // 交换顺序时前端这份 layouts 也要跟着换：后端 db.swap_images 换了
    // img_layouts / img_original 里的序号，不同步的话宽度/对齐读数会张冠李戴
    // （序号是五处共享的不变量，见 db.swap_images 的注释）。
    if (st && data.swapped_with !== undefined) {
      const a = Number(data.index), b = Number(data.swapped_with);
      const tmp = at(st, a);
      st.layouts[a] = at(st, b);
      st.layouts[b] = tmp;
    }
    if (st && Array.isArray(data.groups)) {
      st.groups = data.groups
        .map(g => ({ids: (g.ids || []).map(Number), row: !!g.row}))
        .filter(g => g.ids.length > 1);
      bar.dataset.groups = JSON.stringify(st.groups);   // 与 dataset 保持一致
    }
    if (data.body_html !== undefined) replaceBody(bar, data.body_html);
    else syncBar(bar);
  }

  // 一条控制条的初始化：建状态 + 绑只需绑一次的 chips + 绑正文。
  function initBar(bar) {
    // 同一张卡可能先由无限滚动初始化，随后又被某个局部刷新入口交给本函数。
    // WeakMap 已有状态时只重绑新正文，不能再给每个 chip 叠一层监听。
    if (states.has(bar)) {
      bindBody(bar);
      return;
    }
    const body = bodyElOf(bar);
    if (!body || !body.querySelector('img')) return;
    // 题干版缺省、解析版显式标了 data-field="solution"——两者各自独立编号
    // （见 db.set_img_layout 的 field 参数），POST 体里带上这个字段才能落对列。
    const field = bar.dataset.field === 'solution' ? 'solution' : 'body';

    states.set(bar, {
      id: bar.dataset.id,
      field,
      layouts: parseLayouts(bar),
      groups: parseGroups(bar),                 // 连续两图的分组（并排/堆叠用）
      wrap: bar.querySelector('.img-width-wrap'),
      valueEl: bar.querySelector('.img-width-value'),
      pickEl: bar.querySelector('.img-pick-label'),
      stackEl: bar.querySelector('.img-stack-chip'),
      sel: 0,                                   // 当前控制条作用的图片序号
      boxes: [], imgs: [], body: null,
      splitCol: null, splitTxt: null
    });

    // 对齐切换（作用于当前选中图）：再点一次已选项 = 清空对齐设置
    bar.querySelectorAll('.img-align-chip').forEach(chip => {
      chip.addEventListener('click', async () => {
        const st = states.get(bar);
        const target = alignTarget(st, st.sel);
        const newAlign = alignOf(st, st.sel) === chip.dataset.align
          ? '' : chip.dataset.align;
        const data = await post(st.id, 'img_align',
          {align: newAlign, index: target, field: st.field});
        if (data.ok) {
          at(st, target).align = data.align || '';
          applyAlign(st, target);
          syncBar(bar);
        }
      });
    });

    // 重置：清除当前选中图的自定义宽度，回默认 35%
    bar.querySelector('.img-width-reset')?.addEventListener('click', async () => {
      const st = states.get(bar);
      const data = await post(st.id, 'img_width', {width: '', index: st.sel, field: st.field});
      if (!data.ok) return;
      at(st, st.sel).w = null;
      applyResult(bar, data);
    });

    // 百分比档位直接写入与拖拽手柄相同的字段。屏幕像素会随窗口变化，但这个
    // “占版心百分比”同时供题卡与 PDF 使用，用户无需靠调整窗口猜最终大小。
    bar.querySelectorAll('.img-width-chip').forEach(chip => {
      chip.addEventListener('click', async () => {
        const st = states.get(bar);
        const width = parseInt(chip.dataset.width, 10);
        if (!Number.isFinite(width)) return;
        const data = await post(st.id, 'img_width',
          {width: String(width), index: st.sel, field: st.field});
        if (!data.ok) {
          if (data.error) alert(data.error);
          return;
        }
        at(st, st.sel).w = width;
        applyResult(bar, data);
      });
    });

    bindBody(bar);
  }

  // 换掉一张题卡的正文（或解析），并把该卡恢复成「可交互」状态。三件事缺一不可：
  //   1. 塞新 HTML（后端 qrender 渲的，与导出同源，前端不重搭结构）
  //   2. 重跑 bindBody —— 新 DOM 里没有包盒/手柄
  //   3. 只对这张卡排公式 —— 传整个 document 会重排全页，几百道题时是明显卡顿
  //      （这正是「改一道题全库重渲」的来源）
  // QMath 由 math.js 定义（defer，文档顺序在本文件之前），正常一定已就位；
  // 万一没有（脚本 404）也只是这张卡的公式停在源码状态，其余交互不受影响。
  function replaceBody(bar, html) {
    const body = bodyElOf(bar);
    if (!body) return false;
    body.innerHTML = html;
    bindBody(bar);
    window.QMath?.typeset(body);
    return true;
  }

  // 图文分栏 / 并排堆叠 / 四图配选项三组 chip 的绑定见下方 bindSplitChips /
  // bindStackChips / bindQuadChips —— 拆成可重入函数是为了让 folder-tree.js
  // 局部换入新卡片后也能重新绑一遍，行为与原来逐条内联绑定完全一致。

  // scope 默认整份文档；folder-tree.js 局部换掉 #q-list 后传入新插入的容器，
  // 只初始化新卡片里的控制条——旧卡片的 states 一直挂在 WeakMap 上，不受影响。
  function initBars(scope) {
    (scope || document).querySelectorAll('.img-layout-bar').forEach(initBar);
  }
  initBars();

  // 分栏/并排/四图配选项三组 chip 当时是绑在 document 上的一次性 querySelectorAll，
  // AJAX 换局部内容后新卡片里的同名 chip 不会自动生效，故也拆成可重入函数。
  function bindSplitChips(scope) {
    (scope || document).querySelectorAll('.img-split-chip').forEach(chip => {
      if (chip.dataset.qfBound === '1') return;
      chip.dataset.qfBound = '1';
      chip.addEventListener('click', async () => {
        if (chip.classList.contains('busy')) return;
        const bar = chip.closest('.img-layout-bar');
        const st = states.get(bar);
        const id = bar.dataset.id;
        const wasOn = chip.classList.contains('on');
        const mode = wasOn ? '' : (chip.dataset.mode || 'opts');
        bar.querySelectorAll('.img-split-chip').forEach(c => c.classList.add('busy'));
        try {
          const data = await post(id, 'img_split', {mode, field: st?.field || 'body'});
          if (!data.ok) {
            if (data.error) alert(data.error);
            return;
          }
          bar.querySelectorAll('.img-split-chip').forEach(c => {
            c.classList.toggle('on', !!data.mode && c.dataset.mode === data.mode);
          });
          applyResult(bar, data);
        } catch (error) {
          // 网络错误或后端重启时也要给明确反馈；否则 Promise 只在控制台报错，
          // 用户看到的仍然像“按钮点了没反应”。
          alert(`图文分栏更新失败：${error?.message || '请求失败'}`);
        } finally {
          bar.querySelectorAll('.img-split-chip').forEach(c => c.classList.remove('busy'));
        }
      });
    });
  }
  function bindStackChips(scope) {
    (scope || document).querySelectorAll('.img-stack-chip').forEach(chip => {
      if (chip.dataset.qfBound === '1') return;
      chip.dataset.qfBound = '1';
      chip.addEventListener('click', async () => {
        const bar = chip.closest('.img-layout-bar');
        const st = states.get(bar);
        if (!st) return;
        const g = groupOf(st, st.sel);
        if (!g) return;
        const data = await post(st.id, 'img_stack',
                                {index: st.sel, stack: !!g.row, field: st.field});
        if (!data.ok) {
          if (data.error) alert(data.error);
          return;
        }
        applyResult(bar, data);
      });
    });
  }
  function bindQuadChips(scope) {
    (scope || document).querySelectorAll('.img-quad-chip').forEach(chip => {
      if (chip.dataset.qfBound === '1') return;
      chip.dataset.qfBound = '1';
      chip.addEventListener('click', async () => {
        const bar = chip.closest('.img-layout-bar');
        const wasOn = chip.classList.contains('on');
        const data = await post(bar.dataset.id, 'img_split',
                                {mode: wasOn ? '' : 'pair'});
        if (!data.ok) {
          if (data.error) alert(data.error);
          return;
        }
        chip.classList.toggle('on', data.mode === 'pair');
        bar.querySelectorAll('.img-split-chip').forEach(c => {
          c.classList.toggle('on', !!data.mode && c.dataset.mode === data.mode);
        });
        applyResult(bar, data);
      });
    });
  }
  bindSplitChips();
  bindStackChips();
  bindQuadChips();

  // 供 image-redraw.js 换正文用、供 folder-tree.js 给局部换入的新卡片重新初始化。
  // 全站第二个全局符号，理由见文件头。
  window.QImgLayout = {replaceBody, initBars, bindSplitChips, bindStackChips, bindQuadChips};
})();
