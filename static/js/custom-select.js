// QuizForge 通用选择器：保留原生 select 作为唯一表单值源，只增强可视交互。
(function () {
  'use strict';

  const enhanced = new WeakMap();
  let opened = null;

  function icon(name) {
    return window.QFIcon ? window.QFIcon(name) : '';
  }

  function optionDescription(option) {
    return String(option.dataset.description || option.title || '').trim();
  }

  function selectableRows(state) {
    return [...state.menu.querySelectorAll('[role="option"]:not([disabled])')]
      .filter(row => !row.hidden);
  }

  function focusRow(state, target) {
    const rows = selectableRows(state);
    if (!rows.length) return;
    let index = target;
    if (target === 'selected') {
      index = rows.findIndex(row => row.getAttribute('aria-selected') === 'true');
      if (index < 0) index = 0;
    } else if (target === 'last') {
      index = rows.length - 1;
    }
    rows[Math.max(0, Math.min(Number(index) || 0, rows.length - 1))].focus();
  }

  function position(state) {
    if (state.menu.hidden) return;
    const rect = state.button.getBoundingClientRect();
    const gap = 6;
    const edge = 10;
    const viewportHeight = document.documentElement.clientHeight;
    const viewportWidth = document.documentElement.clientWidth;
    const below = viewportHeight - rect.bottom - edge;
    const above = rect.top - edge;
    const openAbove = below < 210 && above > below;
    const maxHeight = Math.max(120, Math.min(360, (openAbove ? above : below) - gap));
    const width = Math.min(Math.max(rect.width, 220), viewportWidth - edge * 2);
    const left = Math.max(edge, Math.min(rect.left, viewportWidth - width - edge));
    state.menu.style.width = `${width}px`;
    state.menu.style.maxHeight = `${maxHeight}px`;
    state.menu.style.left = `${left}px`;
    if (openAbove) {
      state.menu.style.top = 'auto';
      state.menu.style.bottom = `${viewportHeight - rect.top + gap}px`;
      state.menu.dataset.side = 'top';
    } else {
      state.menu.style.top = `${rect.bottom + gap}px`;
      state.menu.style.bottom = 'auto';
      state.menu.dataset.side = 'bottom';
    }
  }

  function close(state, restoreFocus) {
    if (!state || state.menu.hidden) return;
    state.menu.hidden = true;
    state.button.setAttribute('aria-expanded', 'false');
    state.shell.classList.remove('is-open');
    if (opened === state) opened = null;
    if (restoreFocus) state.button.focus();
  }

  function filter(state, query) {
    const needle = String(query || '').trim().toLocaleLowerCase();
    state.menu.querySelectorAll('[role="option"]').forEach(row => {
      row.hidden = row.dataset.sourceHidden === '1' ||
        (Boolean(needle) && !row.dataset.searchText.includes(needle));
    });
    state.menu.querySelectorAll('.qf-select-group').forEach(group => {
      let next = group.nextElementSibling;
      let visible = false;
      while (next && !next.classList.contains('qf-select-group')) {
        if (next.matches('[role="option"]') && !next.hidden) visible = true;
        next = next.nextElementSibling;
      }
      group.hidden = !visible;
    });
  }

  function buildMenu(state) {
    const {select, menu} = state;
    const fragment = document.createDocumentFragment();
    const searchable = select.dataset.search === '1' || select.options.length > 8;
    if (searchable) {
      const searchWrap = document.createElement('div');
      searchWrap.className = 'qf-select-search';
      searchWrap.innerHTML = icon('search');
      const search = document.createElement('input');
      search.type = 'search';
      search.placeholder = select.dataset.searchPlaceholder || '搜索选项';
      search.setAttribute('aria-label', search.placeholder);
      search.addEventListener('input', () => filter(state, search.value));
      searchWrap.appendChild(search);
      fragment.appendChild(searchWrap);
      state.search = search;
    } else {
      state.search = null;
    }

    let currentGroup = null;
    [...select.options].forEach((option, index) => {
      const group = option.parentElement?.tagName === 'OPTGROUP'
        ? option.parentElement.label : '';
      if (group && group !== currentGroup) {
        const heading = document.createElement('div');
        heading.className = 'qf-select-group';
        heading.textContent = group;
        fragment.appendChild(heading);
      }
      currentGroup = group;
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'qf-select-option';
      row.dataset.index = String(index);
      row.dataset.searchText = `${option.textContent} ${optionDescription(option)}`.toLocaleLowerCase();
      row.dataset.sourceHidden = option.hidden ? '1' : '0';
      row.setAttribute('role', 'option');
      row.setAttribute('aria-selected', String(option.selected));
      row.disabled = option.disabled;
      row.hidden = option.hidden;

      const copy = document.createElement('span');
      copy.className = 'qf-select-option-copy';
      const label = document.createElement('strong');
      label.textContent = option.textContent;
      copy.appendChild(label);
      const description = optionDescription(option);
      if (description) {
        const detail = document.createElement('small');
        detail.textContent = description;
        copy.appendChild(detail);
      }
      const check = document.createElement('span');
      check.className = 'qf-select-check';
      check.setAttribute('aria-hidden', 'true');
      check.innerHTML = icon('check');
      row.append(copy, check);
      row.addEventListener('click', () => {
        if (option.disabled) return;
        select.selectedIndex = index;
        select.dispatchEvent(new Event('input', {bubbles: true}));
        select.dispatchEvent(new Event('change', {bubbles: true}));
        sync(state);
        close(state, true);
      });
      fragment.appendChild(row);
    });
    menu.replaceChildren(fragment);
  }

  function sync(state) {
    const {select, button, shell} = state;
    if (!select.isConnected) {
      dispose(state);
      return;
    }
    const option = select.options[select.selectedIndex] || select.options[0];
    state.label.textContent = option?.textContent || select.dataset.placeholder || '请选择';
    button.disabled = select.disabled;
    shell.hidden = select.hidden || select.classList.contains('hidden');
    shell.classList.toggle('is-disabled', select.disabled);
    buildMenu(state);
  }

  function dispose(state) {
    if (!state) return;
    if (opened === state) opened = null;
    state.menu.remove();
    if (!state.select.isConnected) state.shell.remove();
    enhanced.delete(state.select);
  }

  function open(state, keyboard) {
    if (state.select.disabled) return;
    if (opened && opened !== state) close(opened, false);
    sync(state);
    state.menu.hidden = false;
    state.button.setAttribute('aria-expanded', 'true');
    state.shell.classList.add('is-open');
    opened = state;
    filter(state, '');
    if (state.search) state.search.value = '';
    position(state);
    if (state.search) state.search.focus();
    else if (keyboard) focusRow(state, 'selected');
  }

  function handleMenuKeydown(state, event) {
    const rows = selectableRows(state);
    const activeIndex = rows.indexOf(document.activeElement);
    if (event.key === 'Escape') {
      event.preventDefault();
      close(state, true);
    } else if (event.key === 'ArrowDown') {
      event.preventDefault();
      focusRow(state, activeIndex < 0 ? 0 : activeIndex + 1);
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      focusRow(state, activeIndex < 0 ? 'last' : activeIndex - 1);
    } else if (event.key === 'Home') {
      event.preventDefault();
      focusRow(state, 0);
    } else if (event.key === 'End') {
      event.preventDefault();
      focusRow(state, 'last');
    }
  }

  function enhance(select) {
    if (!(select instanceof HTMLSelectElement) || select.multiple || select.size > 1
        || select.dataset.nativeSelect === '1' || enhanced.has(select)) return;
    const shell = document.createElement('span');
    shell.className = 'qf-select';
    if (select.style.width) shell.style.width = select.style.width;
    else if (select.classList.contains('input')) shell.classList.add('is-fluid');
    select.parentNode.insertBefore(shell, select);
    shell.appendChild(select);
    select.classList.add('qf-select-native');
    select.tabIndex = -1;
    select.setAttribute('aria-hidden', 'true');

    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'qf-select-trigger';
    button.setAttribute('aria-haspopup', 'listbox');
    button.setAttribute('aria-expanded', 'false');
    const label = document.createElement('span');
    label.className = 'qf-select-value';
    const chevron = document.createElement('span');
    chevron.className = 'qf-select-chevron';
    chevron.setAttribute('aria-hidden', 'true');
    chevron.innerHTML = icon('chevron-down');
    button.append(label, chevron);
    shell.appendChild(button);

    const menu = document.createElement('div');
    menu.className = 'qf-select-menu';
    menu.setAttribute('role', 'listbox');
    menu.hidden = true;
    document.body.appendChild(menu);
    const state = {select, shell, button, label, menu, search: null};
    enhanced.set(select, state);

    button.addEventListener('click', () => state.menu.hidden ? open(state, false) : close(state, false));
    button.addEventListener('keydown', event => {
      if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
        event.preventDefault();
        open(state, true);
        if (!state.search) focusRow(state, event.key === 'End' || event.key === 'ArrowUp' ? 'last' : 0);
      } else if (event.key === 'Escape') {
        close(state, false);
      }
    });
    menu.addEventListener('keydown', event => handleMenuKeydown(state, event));
    select.addEventListener('change', () => sync(state));
    select.addEventListener('focus', () => button.focus());
    select.form?.addEventListener('reset', () => setTimeout(() => sync(state), 0));
    sync(state);
  }

  function enhanceAll(scope) {
    if (scope?.matches?.('select')) enhance(scope);
    scope?.querySelectorAll?.('select').forEach(enhance);
  }

  document.addEventListener('click', event => {
    if (opened && !opened.shell.contains(event.target) && !opened.menu.contains(event.target)) {
      close(opened, false);
    }
  });
  window.addEventListener('resize', () => opened && position(opened));
  window.addEventListener('scroll', () => opened && position(opened), true);

  const observer = new MutationObserver(records => {
    const dirtySelects = new Set();
    records.forEach(record => {
      record.addedNodes.forEach(node => node.nodeType === 1 && enhanceAll(node));
      record.removedNodes.forEach(node => {
        if (node.nodeType !== 1) return;
        const selects = node.matches?.('select')
          ? [node] : [...(node.querySelectorAll?.('select') || [])];
        selects.forEach(select => {
          if (!select.isConnected && enhanced.has(select)) dispose(enhanced.get(select));
        });
      });
      const owner = record.target instanceof HTMLSelectElement
        ? record.target : record.target.closest?.('select');
      if (owner && enhanced.has(owner)) dirtySelects.add(owner);
    });
    dirtySelects.forEach(select => {
      const state = enhanced.get(select);
      if (state) sync(state);
    });
  });

  function init() {
    enhanceAll(document);
    observer.observe(document.documentElement, {
      childList: true, subtree: true, attributes: true,
      attributeFilter: ['class', 'disabled', 'hidden', 'label'],
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, {once: true});
  else init();
  window.QFSelect = {
    enhance,
    refresh(select) { if (enhanced.has(select)) sync(enhanced.get(select)); else enhance(select); },
    enhanceAll,
    closeAll() { if (opened) close(opened, false); },
  };
})();
