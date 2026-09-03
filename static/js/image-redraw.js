// AI 重绘配图的前端：发起任务 → 轮询 → 预览对比 → 应用 / 重新生成 / 还原原图。
//
// 三个不太显然的决定：
//
// 1. **应用或还原之后绝不 location.reload()。** 一页上可能有好几道题同时在重绘，
//    刷新会把其它卡片正在跑的轮询循环连同它们的 job_id 一起销毁，那些任务在后端
//    照样跑完，但前端再也拿不到结果了。所以只整块替换这张卡的题干 HTML
//    （QImgLayout.replaceBody），别的卡片不受影响。
//
// 2. **job_id 存 sessionStorage 而不是 localStorage。** 重绘要几十秒，用户很可能
//    切走再回来；页面一刷新内存里的轮询就断了，但任务在后端还活着，凭 job_id 能
//    重新接上。用 sessionStorage 是因为这种"接上"只在同一个标签页里有意义，
//    localStorage 会让另一个标签页也去认领同一个任务。
//
// 3. **readJSON 而不是直接 res.json()。** 反向代理超时会返回 HTML 的 502/504 页面，
//    res.json() 撞上它抛的是 `Unexpected token '<'`，用户完全看不懂。

(function () {
  'use strict';

  var POLL_INTERVAL_MS = 2500;
  var POLL_MAX_MS = 480000;              // 8 分钟：视觉模型 + xelatex 编译的上限
  var STORE_KEY = 'qf_redraw_jobs';
  var RESUME_MAX_MS = 25 * 60 * 1000;    // 比后端的 _REDRAW_JOB_TTL(30min) 略短

  // ---- 未完成任务的登记表（跨刷新续上）--------------------------------------

  function loadJobs() {
    try {
      return JSON.parse(sessionStorage.getItem(STORE_KEY) || '{}') || {};
    } catch (e) { return {}; }
  }

  function saveJobs(jobs) {
    try { sessionStorage.setItem(STORE_KEY, JSON.stringify(jobs)); } catch (e) {}
  }

  function rememberJob(qid, index, jobId) {
    var jobs = loadJobs();
    jobs[qid + ':' + index] = {job: jobId, ts: Date.now()};
    saveJobs(jobs);
  }

  function forgetJob(qid, index) {
    var jobs = loadJobs();
    delete jobs[qid + ':' + index];
    saveJobs(jobs);
  }

  // ---- 小工具 ---------------------------------------------------------------

  function readJSON(res) {
    // 先读文本再自己解析：网关的 502/504 是 HTML，res.json() 会抛看不懂的语法错误
    return res.text().then(function (txt) {
      try {
        return JSON.parse(txt);
      } catch (e) {
        return {ok: false, error: '服务端返回了非 JSON 响应（HTTP ' + res.status + '）。'};
      }
    });
  }

  function postJSON(url, payload) {
    return fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload || {})
    }).then(readJSON);
  }

  // 控制条当前作用的是第几张图。排版脚本把 .picked 打在 .qimg-resizable 上，
  // 但单图时它**不打** .picked（见 index.html 里 imgs.length > 1 的条件），
  // 故找不到就退回 0。
  function pickedIndex(bar) {
    var card = bar.closest('.card');
    if (!card) return 0;
    var boxes = card.querySelectorAll('.body .qimg-resizable');
    for (var i = 0; i < boxes.length; i++) {
      if (boxes[i].classList.contains('picked')) return i;
    }
    return 0;
  }

  // data-originals（img_originals 的 JSON）→ {index: 原文件名}
  var barOriginals = new WeakMap();

  function originalsOf(bar) {
    if (barOriginals.has(bar)) return barOriginals.get(bar);
    var out = {};
    try {
      (JSON.parse(bar.dataset.originals || '[]') || []).forEach(function (it) {
        var i = parseInt(it.i, 10);
        if (isFinite(i) && i >= 0 && it.orig) out[i] = it.orig;
      });
    } catch (e) { /* 脏数据当作没有备份，别让按钮整个失灵 */ }
    barOriginals.set(bar, out);
    return out;
  }

  // 版本列表按图片序号缓存；正文局部替换后控制条仍是同一个节点，可以继续复用。
  var barVersions = new WeakMap();

  function setVersions(bar, index, rows) {
    var all = barVersions.get(bar) || {};
    all[index] = Array.isArray(rows) ? rows : [];
    barVersions.set(bar, all);
  }

  function versionsOf(bar, index) {
    var all = barVersions.get(bar);
    return all && Object.prototype.hasOwnProperty.call(all, index)
      ? all[index] : null;
  }

  // "还原原图"只在当前选中图有原图版本且当前不是原图时露出。
  function syncRestore(bar) {
    var btn = bar.querySelector('.img-restore-btn');
    if (!btn) return;
    var index = pickedIndex(bar);
    var rows = versionsOf(bar, index);
    if (rows) {
      var hasOriginal = rows.some(function (row) { return row.kind === 'original'; });
      var current = rows.find(function (row) { return row.current; });
      btn.hidden = !hasOriginal || !current || current.kind === 'original';
      return;
    }
    btn.hidden = !originalsOf(bar)[index];
  }

  function getJSON(url) {
    return fetch(url).then(readJSON);
  }

  function addIcon(button, name) {
    if (!window.QFIcon) return;
    var markup = window.QFIcon(name);
    if (markup) button.insertAdjacentHTML('afterbegin', markup);
  }

  function makeVersionAction(label, icon, className, handler) {
    var button = document.createElement('button');
    button.type = 'button';
    button.className = className || '';
    button.appendChild(document.createTextNode(label));
    if (icon) addIcon(button, icon);
    button.addEventListener('click', handler);
    return button;
  }

  function renderVersionList(dialog, bar, index, rows) {
    setVersions(bar, index, rows);
    var body = dialog.querySelector('.image-versions-body');
    body.replaceChildren();
    if (!rows.length) {
      var empty = document.createElement('div');
      empty.className = 'image-versions-empty';
      empty.textContent = '还没有登记的图片版本。';
      body.appendChild(empty);
      syncRestore(bar);
      return;
    }

    var list = document.createElement('div');
    list.className = 'image-versions-list';
    rows.forEach(function (row) {
      var item = document.createElement('article');
      item.className = 'image-version-item' + (row.current ? ' current' : '');

      var preview = document.createElement('div');
      preview.className = 'image-version-preview';
      if (row.exists) {
        var image = document.createElement('img');
        image.src = row.src;
        image.alt = row.kind === 'original' ? '原图' : 'AI 生成版本';
        preview.appendChild(image);
      } else {
        var missing = document.createElement('span');
        missing.className = 'image-version-missing';
        missing.textContent = '文件不存在';
        preview.appendChild(missing);
      }
      item.appendChild(preview);

      var name = document.createElement('div');
      name.className = 'image-version-name';
      name.title = row.name || '';
      name.textContent = row.name || '未命名版本';
      item.appendChild(name);

      var meta = document.createElement('div');
      meta.className = 'image-version-meta';
      var kind = document.createElement('span');
      kind.textContent = row.kind === 'original' ? '原图' : 'AI 生成';
      meta.appendChild(kind);
      if (row.model) {
        var model = document.createElement('span');
        model.textContent = '模型：' + row.model;
        model.title = model.textContent;
        meta.appendChild(model);
      }
      if (row.created) {
        var created = document.createElement('span');
        created.textContent = '时间：' + row.created;
        meta.appendChild(created);
      }
      if (row.prompt) {
        var prompt = document.createElement('span');
        prompt.className = 'image-version-prompt';
        prompt.textContent = '附加要求：' + row.prompt;
        meta.appendChild(prompt);
      }
      if (row.current) {
        var state = document.createElement('span');
        state.className = 'image-version-state';
        state.textContent = '当前使用';
        meta.appendChild(state);
      }
      item.appendChild(meta);

      var actions = document.createElement('div');
      actions.className = 'image-version-actions';
      if (!row.current && row.exists) {
        actions.appendChild(makeVersionAction('切换', 'check', '', function () {
          switchVersion(dialog, bar, index, row.name);
        }));
      }
      if (row.can_delete) {
        actions.appendChild(makeVersionAction('删除', 'trash', 'danger', function () {
          deleteVersion(dialog, bar, index, row);
        }));
      }
      item.appendChild(actions);
      list.appendChild(item);
    });
    body.appendChild(list);
    syncRestore(bar);
  }

  function openVersions(bar, index) {
    if (bar.dataset.versionBusy === '1') return;
    bar.dataset.versionBusy = '1';
    var dialog = document.createElement('dialog');
    dialog.className = 'redraw-dialog image-versions-dialog';

    var head = document.createElement('div');
    head.className = 'redraw-head image-versions-head';
    var title = document.createElement('span');
    title.textContent = '图片版本历史';
    head.appendChild(title);
    var close = makeVersionAction('', 'x', 'image-versions-close', function () {
      dialog.close();
    });
    close.setAttribute('aria-label', '关闭版本历史');
    close.title = '关闭';
    head.appendChild(close);
    dialog.appendChild(head);

    var body = document.createElement('div');
    body.className = 'image-versions-body';
    body.textContent = '正在读取版本历史…';
    dialog.appendChild(body);
    dialog.addEventListener('close', function () {
      bar.dataset.versionBusy = '0';
      dialog.remove();
    });
    document.body.appendChild(dialog);
    dialog.showModal();

    getJSON('/question/' + encodeURIComponent(bar.dataset.id) +
            '/redraw/versions?index=' + encodeURIComponent(index))
      .then(function (data) {
        if (!data.ok) {
          dialog.close();
          alert(data.error || '读取图片版本失败');
          return;
        }
        renderVersionList(dialog, bar, index, data.versions || []);
      })
      .catch(function (err) {
        dialog.close();
        alert('读取图片版本失败：' + err);
      });
  }

  function switchVersion(dialog, bar, index, name) {
    var buttons = dialog.querySelectorAll('button');
    buttons.forEach(function (button) { button.disabled = true; });
    postJSON('/question/' + encodeURIComponent(bar.dataset.id) +
             '/redraw/version/switch', {index: index, name: name})
      .then(function (data) {
        if (!data.ok) {
          alert(data.error || '切换图片版本失败');
          buttons.forEach(function (button) { button.disabled = false; });
          return;
        }
        setVersions(bar, index, data.versions || []);
        applyBodyHtml(bar, data.body_html);
        syncRestore(bar);
        dialog.close();
      })
      .catch(function (err) {
        buttons.forEach(function (button) { button.disabled = false; });
        alert('切换图片版本失败：' + err);
      });
  }

  function deleteVersion(dialog, bar, index, row) {
    if (!window.confirm('确定删除这个 AI 生成的图片版本吗？')) return;
    var buttons = dialog.querySelectorAll('button');
    buttons.forEach(function (button) { button.disabled = true; });
    postJSON('/question/' + encodeURIComponent(bar.dataset.id) +
             '/redraw/version/delete', {index: index, name: row.name})
      .then(function (data) {
        if (!data.ok) {
          alert(data.error || '删除图片版本失败');
          buttons.forEach(function (button) { button.disabled = false; });
          return;
        }
        renderVersionList(dialog, bar, index, data.versions || []);
      })
      .catch(function (err) {
        buttons.forEach(function (button) { button.disabled = false; });
        alert('删除图片版本失败：' + err);
      });
  }

  // 数正文里的图 —— 与后端 tikz_redraw.image_refs 的长度同源（都是正文图片引用
  // 的顺序），所以 index 在前后端指的是同一张。
  function imgCount(bar) {
    var card = bar.closest('.card');
    var body = card ? card.querySelector('.body') : null;
    return body ? body.querySelectorAll('img').length : 0;
  }

  function setBusy(bar, busy, text) {
    var btn = bar.querySelector('.img-redraw-btn');
    if (!btn) return;
    btn.classList.toggle('busy', !!busy);
    // 保留统一图标入口；状态文本走 text node，避免把服务端返回内容当 HTML 插入。
    btn.replaceChildren();
    if (!busy && window.QFIcon) {
      var markup = window.QFIcon('edit');
      if (markup) btn.insertAdjacentHTML('beforeend', markup);
    }
    btn.appendChild(document.createTextNode(busy ? (text || '重绘中…') : 'AI 重绘'));
  }

  function applyBodyHtml(bar, html) {
    // QImgLayout 由 index.html 的内联脚本提供。真拿不到就只能刷新——
    // 宁可打断别处的轮询，也不能让用户以为没生效。
    if (window.QImgLayout && window.QImgLayout.replaceBody(bar, html)) return;
    window.location.reload();
  }

  // Obsidian 运行在 Electron 里，内嵌 iframe 的 window.prompt() 不会可靠显示，
  // 表现就是点了“AI 重绘”完全没反应，而且请求根本还没发出。改用页面自己的
  // <dialog> 收集额外要求；它和结果预览同属当前文档，不依赖宿主原生对话框。
  function requestExtra(total, index) {
    return new Promise(function (resolve) {
      var old = document.querySelector('.redraw-request-dialog');
      if (old) {
        var oldInput = old.querySelector('textarea');
        if (oldInput) oldInput.focus();
        resolve(null);
        return;
      }

      var dlg = document.createElement('dialog');
      dlg.className = 'redraw-dialog redraw-request-dialog';

      var head = document.createElement('div');
      head.className = 'redraw-head';
      head.textContent = total > 1
        ? 'AI 重绘第 ' + (index + 1) + '/' + total + ' 张图'
        : 'AI 重绘图片';
      dlg.appendChild(head);

      var body = document.createElement('div');
      body.className = 'redraw-request-body';
      var hint = document.createElement('p');
      hint.textContent = '可填写额外要求，也可以留空直接开始。';
      var input = document.createElement('textarea');
      input.className = 'input redraw-request-input';
      input.rows = 4;
      input.placeholder = '例如：标出角 ABC / 用虚线画辅助线 / 坐标轴范围 -3 到 3';
      body.appendChild(hint);
      body.appendChild(input);
      dlg.appendChild(body);

      var actions = document.createElement('div');
      actions.className = 'redraw-actions';
      var cancel = document.createElement('button');
      cancel.type = 'button';
      cancel.className = 'btn btn-ghost';
      cancel.textContent = '取消';
      var submit = document.createElement('button');
      submit.type = 'button';
      submit.className = 'btn btn-primary';
      submit.textContent = '开始重绘';
      actions.appendChild(cancel);
      actions.appendChild(submit);
      dlg.appendChild(actions);

      var settled = false;
      function finish(value) {
        if (settled) return;
        settled = true;
        dlg.close();
        dlg.remove();
        resolve(value);
      }
      cancel.addEventListener('click', function () { finish(null); });
      submit.addEventListener('click', function () { finish(input.value.trim()); });
      dlg.addEventListener('cancel', function (ev) {
        ev.preventDefault();
        finish(null);
      });
      input.addEventListener('keydown', function (ev) {
        if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) finish(input.value.trim());
      });

      document.body.appendChild(dlg);
      dlg.showModal();
      input.focus();
    });
  }

  // ---- 预览对话框 -----------------------------------------------------------

  // 用原生 <dialog>：自带焦点陷阱和 Esc 关闭，不用自己实现遮罩与键盘处理。
  // 图片 src 和代码一律用 DOM 属性赋值，**不拼 innerHTML**：那段 TikZ 代码来自
  // 模型，拼进 HTML 就是一个 XSS 面。
  function showPreview(opts) {
    var dlg = document.createElement('dialog');
    dlg.className = 'redraw-dialog';

    var head = document.createElement('div');
    head.className = 'redraw-head';
    head.textContent = '重绘结果对比';
    dlg.appendChild(head);

    var cmp = document.createElement('div');
    cmp.className = 'redraw-compare';
    [['原图', opts.oldSrc], ['重绘', opts.newSrc]].forEach(function (pair) {
      var col = document.createElement('div');
      col.className = 'redraw-col';
      var cap = document.createElement('div');
      cap.className = 'redraw-cap';
      cap.textContent = pair[0];
      var img = document.createElement('img');
      img.src = pair[1];
      img.alt = pair[0];
      col.appendChild(cap);
      col.appendChild(img);
      cmp.appendChild(col);
    });
    dlg.appendChild(cmp);

    if (opts.code) {
      var det = document.createElement('details');
      det.className = 'redraw-code';
      var sum = document.createElement('summary');
      sum.textContent = 'TikZ 源码';
      var pre = document.createElement('pre');
      pre.textContent = opts.code;             // 不是 innerHTML
      det.appendChild(sum);
      det.appendChild(pre);
      dlg.appendChild(det);
    }

    var bar = document.createElement('div');
    bar.className = 'redraw-actions';

    function mkBtn(label, cls, fn) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = cls;
      b.textContent = label;
      b.addEventListener('click', fn);
      bar.appendChild(b);
      return b;
    }

    mkBtn('应用到题目', 'btn btn-primary', function () {
      dlg.close();
      opts.onApply();
    });
    mkBtn('重新生成', 'btn btn-ghost', function () {
      dlg.close();
      opts.onRegen();
    });
    mkBtn('放弃', 'btn btn-ghost', function () { dlg.close(); });
    dlg.appendChild(bar);

    dlg.addEventListener('close', function () { dlg.remove(); });
    document.body.appendChild(dlg);
    dlg.showModal();
  }

  // ---- 轮询 -----------------------------------------------------------------

  function poll(bar, qid, index, jobId, startedAt) {
    var url = '/question/' + encodeURIComponent(qid) +
              '/redraw/status/' + encodeURIComponent(jobId);
    fetch(url).then(readJSON).then(function (data) {
      if (!data.ok) {
        forgetJob(qid, index);
        setBusy(bar, false);
        alert(data.error || '重绘任务已失效，请重新发起。');
        return;
      }
      if (data.status === 'pending') {
        if (Date.now() - startedAt > POLL_MAX_MS) {
          forgetJob(qid, index);
          setBusy(bar, false);
          alert('重绘超时（超过 8 分钟）。任务可能仍在后台运行，稍后刷新页面看看。');
          return;
        }
        // 报已用秒数：视觉模型出图 + xelatex 编译要 1~3 分钟，按钮上只写
        // 「重绘中…」的话用户会以为卡死了去点第二次。
        var secs = Math.round((Date.now() - startedAt) / 1000);
        setBusy(bar, true, '生成中…（约需 1~3 分钟，已 ' + secs + ' 秒）');
        setTimeout(function () {
          poll(bar, qid, index, jobId, startedAt);
        }, POLL_INTERVAL_MS);
        return;
      }
      forgetJob(qid, index);
      setBusy(bar, false);
      if (data.status === 'error') {
        alert('重绘失败：' + (data.error || '未知错误'));
        return;
      }
      var r = data.result || {};
      showPreview({
        oldSrc: r.old_src,
        newSrc: r.src,
        code: r.code,
        onApply: function () { doApply(bar, qid, index, r.name); },
        onRegen: function () { start(bar, qid, index); }
      });
    }).catch(function (err) {
      // 网络抖动不该直接判死：还在时间窗内就继续轮
      if (Date.now() - startedAt <= POLL_MAX_MS) {
        setTimeout(function () {
          poll(bar, qid, index, jobId, startedAt);
        }, POLL_INTERVAL_MS);
        return;
      }
      forgetJob(qid, index);
      setBusy(bar, false);
      alert('查询重绘进度失败：' + err);
    });
  }

  // ---- 动作 -----------------------------------------------------------------

  function start(bar, qid, index) {
    // 多图时点明「第几张 / 共几张」：同一题里选错图是最常见的误操作，
    // 而重绘一次要花一次视觉模型调用 + 一次 xelatex。
    var total = imgCount(bar);
    requestExtra(total, index).then(function (extra) {
      if (extra === null) return;             // 点了取消或按 Esc
      setBusy(bar, true, '提交中…');
      postJSON('/question/' + encodeURIComponent(qid) + '/redraw',
               {index: index, extra: extra}).then(function (data) {
        if (!data.ok) {
          setBusy(bar, false);
          alert(data.error || '发起重绘失败');
          return;
        }
        setBusy(bar, true);
        rememberJob(qid, index, data.job_id);
        poll(bar, qid, index, data.job_id, Date.now());
      }).catch(function (err) {
        setBusy(bar, false);
        alert('发起重绘失败：' + err);
      });
    });
  }

  function doApply(bar, qid, index, name) {
    postJSON('/question/' + encodeURIComponent(qid) + '/redraw/apply',
             {index: index, name: name}).then(function (data) {
      if (!data.ok) { alert(data.error || '应用失败'); return; }
      // 前端自己记上这张图现在有备份了（后端已写进 frontmatter），
      // 这样不刷新也能立刻点亮"还原原图"
      originalsOf(bar)[index] = data.old;
      setVersions(bar, index, data.versions || []);
      applyBodyHtml(bar, data.body_html);
      syncRestore(bar);
    }).catch(function (err) { alert('应用失败：' + err); });
  }

  function doRestore(bar, qid, index) {
    if (!window.confirm('把这张图退回重绘前的原图？')) return;
    postJSON('/question/' + encodeURIComponent(qid) + '/redraw/restore',
             {index: index}).then(function (data) {
      if (!data.ok) { alert(data.error || '还原失败'); return; }
      delete originalsOf(bar)[index];
      setVersions(bar, index, data.versions || []);
      applyBodyHtml(bar, data.body_html);
      syncRestore(bar);
    }).catch(function (err) { alert('还原失败：' + err); });
  }

  // ---- 绑定 -----------------------------------------------------------------

  function bindBar(bar) {
    if (bar.dataset.redrawBound === '1') return;   // replaceBody 后会重进，别重复绑
    bar.dataset.redrawBound = '1';
    var qid = bar.dataset.id;

    var rb = bar.querySelector('.img-redraw-btn');
    if (rb) {
      rb.addEventListener('click', function () {
        if (rb.classList.contains('busy')) return;
        start(bar, qid, pickedIndex(bar));
      });
    }
    var vb = bar.querySelector('.img-version-btn');
    if (vb) {
      vb.addEventListener('click', function () {
        openVersions(bar, pickedIndex(bar));
      });
    }
    var sb = bar.querySelector('.img-restore-btn');
    if (sb) {
      sb.addEventListener('click', function () {
        doRestore(bar, qid, pickedIndex(bar));
      });
    }
    // 点正文里的图会切换"当前选中图"，"还原原图"的显隐要跟着变
    var card = bar.closest('.card');
    if (card) {
      card.addEventListener('click', function (ev) {
        if (ev.target.closest('.qimg-resizable')) setTimeout(function () { syncRestore(bar); }, 0);
      });
    }
    syncRestore(bar);

    // 刷新前发起的任务：只要还在时间窗内就接着轮
    var rec = loadJobs()[qid + ':' + pickedIndex(bar)];
    if (rec && rec.job && Date.now() - rec.ts < RESUME_MAX_MS) {
      setBusy(bar, true);
      poll(bar, qid, pickedIndex(bar), rec.job, rec.ts);
    }
  }

  function bindAll(scope) {
    (scope || document).querySelectorAll('.img-layout-bar').forEach(bindBar);
  }

  window.QImgRedraw = {bindAll: bindAll, syncRestore: syncRestore,
    openVersions: openVersions};
  document.addEventListener('DOMContentLoaded', function () { bindAll(document); });
})();
