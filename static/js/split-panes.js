// 工作区竖向分栏统一拖动。宽度只保存在本机 localStorage，不进入题库或云端。
(function () {
  'use strict';

  const handles = [...document.querySelectorAll('[data-split-resizer]')];
  if (!handles.length) return;
  const states = new Map();
  const storagePrefix = 'quizforge.split.';

  function resolveState(handle) {
    const owner = handle.dataset.splitOwner === 'root'
      ? document.documentElement
      : document.querySelector(handle.dataset.splitOwner || '');
    const panel = document.querySelector(handle.dataset.splitPanel || '');
    if (!owner || !panel) return null;
    const property = handle.dataset.splitProperty;
    const cssWidth = Number.parseFloat(
      getComputedStyle(owner).getPropertyValue(property)
    );
    return {
      handle,
      owner,
      panel,
      property,
      side: handle.dataset.splitSide === 'right' ? 'right' : 'left',
      min: Number(handle.dataset.splitMin || 160),
      max: Number(handle.dataset.splitMax || 520),
      mainMin: Number(handle.dataset.splitMainMin || 360),
      key: storagePrefix + (handle.dataset.splitKey || handle.dataset.splitProperty),
      initial: panel.getBoundingClientRect().width || cssWidth,
      customized: false,
    };
  }

  function visible(state) {
    return state.handle.getClientRects().length > 0
      && state.panel.getClientRects().length > 0;
  }

  function availableMax(state) {
    const ownerWidth = state.owner === document.documentElement
      ? document.documentElement.clientWidth
      : state.owner.getBoundingClientRect().width;
    let occupied = 0;
    let separators = 0;
    states.forEach(other => {
      if (other === state || other.owner !== state.owner || !visible(other)) return;
      occupied += other.panel.getBoundingClientRect().width;
      separators += other.handle.getBoundingClientRect().width;
    });
    separators += state.handle.getBoundingClientRect().width;
    return Math.max(state.min, ownerWidth - occupied - separators - state.mainMin);
  }

  function clamp(state, value) {
    return Math.round(Math.max(state.min, Math.min(state.max, availableMax(state), value)));
  }

  function syncAria(state, value) {
    const next = Math.round(value || state.initial || state.min);
    state.handle.setAttribute('aria-valuenow', String(next));
    state.handle.setAttribute('aria-valuemin', String(state.min));
    state.handle.setAttribute(
      'aria-valuemax',
      String(Math.round(Math.min(state.max, availableMax(state))))
    );
  }

  function apply(state, value, persist) {
    const next = clamp(state, value);
    state.owner.style.setProperty(state.property, next + 'px');
    state.customized = true;
    syncAria(state, next);
    if (persist) {
      try { localStorage.setItem(state.key, String(next)); } catch (_) { /* 禁用存储时仅本次有效。 */ }
    }
  }

  handles.forEach(handle => {
    const state = resolveState(handle);
    if (!state) return;
    states.set(handle, state);
    let startX = 0;
    let startWidth = 0;

    handle.addEventListener('pointerdown', event => {
      if (event.button !== 0 || !visible(state)) return;
      event.preventDefault();
      startX = event.clientX;
      startWidth = state.panel.getBoundingClientRect().width;
      handle.setPointerCapture(event.pointerId);
      handle.classList.add('is-dragging');
      document.documentElement.classList.add('is-resizing-pane');
    });
    handle.addEventListener('pointermove', event => {
      if (!handle.hasPointerCapture(event.pointerId)) return;
      const delta = event.clientX - startX;
      apply(state, startWidth + (state.side === 'right' ? -delta : delta), false);
    });
    function finish(event) {
      if (!handle.hasPointerCapture(event.pointerId)) return;
      handle.releasePointerCapture(event.pointerId);
      handle.classList.remove('is-dragging');
      document.documentElement.classList.remove('is-resizing-pane');
      apply(state, state.panel.getBoundingClientRect().width, true);
    }
    handle.addEventListener('pointerup', finish);
    handle.addEventListener('pointercancel', finish);
    handle.addEventListener('keydown', event => {
      if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
      event.preventDefault();
      const direction = event.key === 'ArrowRight' ? 1 : -1;
      const signed = state.side === 'right' ? -direction : direction;
      apply(state, state.panel.getBoundingClientRect().width + signed * 12, true);
    });
    handle.addEventListener('dblclick', () => {
      state.owner.style.removeProperty(state.property);
      state.customized = false;
      try { localStorage.removeItem(state.key); } catch (_) { /* 无持久存储时无需处理。 */ }
      requestAnimationFrame(() => syncAria(
        state,
        state.panel.getBoundingClientRect().width || state.initial
      ));
    });
  });

  requestAnimationFrame(() => {
    states.forEach(state => {
      let stored = NaN;
      try { stored = Number(localStorage.getItem(state.key)); } catch (_) { /* 使用 CSS 默认宽度。 */ }
      if (Number.isFinite(stored) && stored > 0) {
        apply(state, stored, false);
      } else {
        // 隐藏中的右栏宽度为 0；未拖动时保留 CSS 默认值，才能继续响应媒体查询。
        syncAria(state, state.panel.getBoundingClientRect().width || state.initial);
      }
    });
  });

  window.addEventListener('resize', () => {
    states.forEach(state => {
      if (!visible(state)) return;
      if (state.customized) {
        apply(state, state.panel.getBoundingClientRect().width, false);
      } else {
        syncAria(state, state.panel.getBoundingClientRect().width);
      }
    });
  });
})();
