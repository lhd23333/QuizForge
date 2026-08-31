// 导入校对页：逐题附加图片、可视化预览与拖动排序。
(function () {
  'use strict';

  const MODES = {
    '单选题': [['pair', '一图一选项'], ['opts', '仅选项分栏'], ['full', '整体分栏'],
               ['between', '题干与选项间'], ['after', '题目后']],
    '多选题': [['pair', '一图一选项'], ['opts', '仅选项分栏'], ['full', '整体分栏'],
               ['between', '题干与选项间'], ['after', '题目后']],
    '解答题': [['sub', '仅小问分栏'], ['full', '整体分栏'],
               ['between', '题干与小问间'], ['after', '题目后']],
    '填空题': [['full', '题干分栏'], ['between', '题干与小问间'], ['after', '题目后']],
  };
  const subject = document.getElementById('prev-cards')?.dataset.subject || 'math';
  const DEFAULTS = {
    '单选题': 'opts', '多选题': 'opts',
    '解答题': subject === 'physics' ? 'after' : 'sub',
    '填空题': subject === 'physics' ? 'between' : 'full',
  };
  const MAX_IMAGE_BYTES = Number(
    document.getElementById('prev-cards')?.dataset.maxImageBytes || 25 * 1024 * 1024);
  const IMAGE_EXTS = new Set(['.png', '.jpg', '.jpeg', '.webp', '.bmp']);
  const MIME_EXTS = {
    'image/png': '.png', 'image/jpeg': '.jpg',
    'image/webp': '.webp', 'image/bmp': '.bmp',
  };
  const states = new WeakMap();

  function extension(name) {
    const match = String(name || '').toLowerCase().match(/\.[^.\\/]+$/);
    return match ? match[0] : '';
  }

  function normalizedFile(file, index) {
    const ext = extension(file.name);
    if (IMAGE_EXTS.has(ext)) return file;
    const inferred = MIME_EXTS[String(file.type || '').toLowerCase()];
    if (!inferred) return null;
    return new File([file], `clipboard-${Date.now()}-${index + 1}${inferred}`,
      {type: file.type, lastModified: Date.now()});
  }

  function looksPairable(card, total) {
    if (total !== 4) return false;
    const type = card.querySelector('.type-sel')?.value || '';
    if (!['单选题', '多选题'].includes(type)) return false;
    const body = card.querySelector('textarea[name^="body_"]')?.value || '';
    return ['A', 'B', 'C', 'D'].every(letter =>
      new RegExp(`(?:^|\\s)${letter}[.．、]`).test(body));
  }

  function autoDefaults(card, total) {
    const type = card.querySelector('.type-sel')?.value || '解答题';
    if (looksPairable(card, total)) return {mode: 'pair', flow: 'column'};
    if (type === '单选题' || type === '多选题') {
      return total > 1
        ? {mode: 'between', flow: 'row'}
        : {mode: 'opts', flow: 'column'};
    }
    if (type === '填空题') {
      return subject === 'physics'
        ? {mode: 'between', flow: 'row'}
        : {mode: 'full', flow: 'column'};
    }
    return subject === 'physics'
      ? {mode: 'after', flow: 'row'}
      : {mode: 'sub', flow: 'column'};
  }

  function applyAutoDefaults(state) {
    const defaults = autoDefaults(state.card, state.existing + state.items.length);
    if (state.modeTouched.value !== '1') syncMode(state.card, defaults.mode);
    if (state.flowTouched.value !== '1') state.flow.value = defaults.flow;
  }

  function syncInput(state) {
    const transfer = new DataTransfer();
    state.items.forEach(item => transfer.items.add(item.file));
    state.input.files = transfer.files;
  }

  function syncMode(card, preferred) {
    const select = card.querySelector('.qcard-img-mode');
    const type = card.querySelector('.type-sel')?.value || '解答题';
    const options = MODES[type] || [];
    const current = preferred || select.value || select.dataset.value || DEFAULTS[type] || '';
    select.replaceChildren(...options.map(([value, label]) => {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = label;
      return option;
    }));
    select.value = options.some(([value]) => value === current)
      ? current : (DEFAULTS[type] || options[0]?.[0] || '');
    select.disabled = !options.length;
  }

  function addFiles(state, files) {
    const accepted = [];
    for (const [index, raw] of Array.from(files || []).entries()) {
      const file = normalizedFile(raw, index);
      if (!file) {
        alert('只支持 PNG/JPG/WEBP/BMP 图片');
        return false;
      }
      if (file.size > MAX_IMAGE_BYTES) {
        alert(`「${file.name}」过大（单图上限 ${Math.round(MAX_IMAGE_BYTES / 1024 / 1024)}MB）`);
        return false;
      }
      accepted.push(file);
    }
    if (!accepted.length) return false;
    if (state.existing + state.items.length + accepted.length > 20) {
      alert('每道题一次最多附加 20 张图片');
      return false;
    }
    accepted.forEach(file => state.items.push({file, url: URL.createObjectURL(file)}));
    syncInput(state);
    render(state);
    return true;
  }

  function render(state) {
    // 首次绑定保留服务端按完整正文算出的值，尤其是四图一项一图；只有后续增删图
    // 或改题型时才在客户端按最终数量重算。前端不重复猜服务端已有结论。
    if (state.initialized) applyAutoDefaults(state);
    else state.initialized = true;
    state.preview.replaceChildren();
    state.preview.dataset.flow = state.flow.value;
    state.items.forEach((item, index) => {
      const tile = document.createElement('div');
      tile.className = 'qcard-image-tile';
      tile.draggable = true;
      tile.dataset.index = String(index);
      const image = document.createElement('img');
      image.src = item.url;
      image.alt = item.file.name;
      const label = document.createElement('span');
      label.textContent = `${index + 1}. ${item.file.name}`;
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'qcard-image-remove';
      remove.title = '移除这张图片';
      remove.setAttribute('aria-label', remove.title);
      remove.innerHTML = window.QFIcon ? window.QFIcon('x') : '';
      remove.addEventListener('click', () => {
        URL.revokeObjectURL(item.url);
        state.items.splice(index, 1);
        syncInput(state);
        render(state);
      });
      tile.append(image, label, remove);
      state.preview.appendChild(tile);
    });
    const total = state.existing + state.items.length;
    state.count.textContent = total
      ? `共 ${total} 张（新增图片可拖动排序）`
      : '可为本题附加图片';
  }

  function bindOne(box) {
    if (states.has(box)) return;
    const card = box.closest('.qcard');
    const input = box.querySelector('.qcard-image-input');
    const state = {
      card,
      input,
      mode: box.querySelector('.qcard-img-mode'),
      preview: box.querySelector('.qcard-image-preview'),
      flow: box.querySelector('.qcard-img-flow'),
      modeTouched: box.querySelector('.qcard-img-mode-touched'),
      flowTouched: box.querySelector('.qcard-img-flow-touched'),
      count: box.querySelector('.qcard-image-count'),
      existing: Number(box.dataset.existing || 0),
      items: [],
      dragIndex: -1,
      initialized: false,
    };
    states.set(box, state);
    syncMode(card);

    box.querySelector('.qcard-image-pick').addEventListener('click', () => input.click());
    input.addEventListener('change', () => {
      const picked = Array.from(input.files || []);
      if (!addFiles(state, picked)) syncInput(state);
    });
    state.mode.addEventListener('change', () => { state.modeTouched.value = '1'; });
    state.flow.addEventListener('change', () => {
      state.flowTouched.value = '1';
      render(state);
    });
    card.querySelector('.type-sel')?.addEventListener('change', () => {
      if (state.modeTouched.value === '1') syncMode(card);
      else applyAutoDefaults(state);
      render(state);
    });

    state.preview.addEventListener('dragstart', event => {
      const tile = event.target.closest('.qcard-image-tile');
      state.dragIndex = tile ? Number(tile.dataset.index) : -1;
      if (tile) tile.classList.add('is-dragging');
    });
    state.preview.addEventListener('dragover', event => {
      if (state.dragIndex >= 0 && event.target.closest('.qcard-image-tile')) event.preventDefault();
    });
    state.preview.addEventListener('drop', event => {
      const tile = event.target.closest('.qcard-image-tile');
      const target = tile ? Number(tile.dataset.index) : -1;
      if (state.dragIndex < 0 || target < 0 || target === state.dragIndex) return;
      event.preventDefault();
      const [moved] = state.items.splice(state.dragIndex, 1);
      state.items.splice(target, 0, moved);
      state.dragIndex = -1;
      syncInput(state);
      render(state);
    });
    state.preview.addEventListener('dragend', () => {
      state.dragIndex = -1;
      state.preview.querySelectorAll('.is-dragging').forEach(el => el.classList.remove('is-dragging'));
    });
    render(state);
  }

  function bind(scope) {
    (scope || document).querySelectorAll('.qcard-image-import').forEach(bindOne);
  }

  bind();
  document.addEventListener('paste', event => {
    const clipboardFiles = Array.from(event.clipboardData?.files || []);
    const images = clipboardFiles.filter(file =>
      String(file.type || '').toLowerCase().startsWith('image/')
      || IMAGE_EXTS.has(extension(file.name)));
    if (!images.length) return;
    let card = document.activeElement?.closest?.('.qcard') || null;
    if (!card) {
      card = Array.from(document.querySelectorAll('.qcard')).find(
        candidate => candidate.matches(':hover')) || null;
    }
    const box = card?.querySelector('.qcard-image-import');
    const state = box ? states.get(box) : null;
    if (!state) return;
    event.preventDefault();
    addFiles(state, images);
  });
  window.QImportImages = {bind};
})();
