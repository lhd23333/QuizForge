// 题卡原地编辑：源码、实时编译、阅读三个模式共享同一份未保存文本。
// 预览交给后端 qrender，避免浏览器端另写一套题型/公式/图片规则而产生偏差。
(function () {
  'use strict';

  const previewTimers = new WeakMap();
  const previewGenerations = new WeakMap();
  const snapshots = new WeakMap();

  function controls(editor) {
    return {
      body: editor.querySelector('.inline-body-source'),
      solution: editor.querySelector('.inline-solution-source'),
      note: editor.querySelector('.inline-note-source'),
      type: editor.querySelector('.inline-type'),
      difficulty: editor.querySelector('.inline-difficulty'),
      source: editor.querySelector('.inline-source'),
      tags: editor.querySelector('.inline-tags'),
    };
  }

  function payload(editor) {
    const fields = controls(editor);
    return {
      body: fields.body.value,
      solution: fields.solution.value,
      note: fields.note.value,
      type: fields.type.value,
      difficulty: fields.difficulty.value,
      source: fields.source.value,
      tags: fields.tags.value,
      collection: editor.dataset.collection || '',
      card_sort: document.getElementById('q-list')?.dataset.customSort === '1'
        ? 'custom' : 'browse',
    };
  }

  function signature(editor) {
    return JSON.stringify(payload(editor));
  }

  function setStatus(editor, message, isError) {
    const status = editor.querySelector('.inline-save-status');
    status.textContent = message || '';
    status.classList.toggle('is-error', Boolean(isError));
  }

  function setMode(editor, mode) {
    if (!['source', 'live', 'read'].includes(mode)) return;
    editor.querySelector('.inline-editor-workbench').dataset.mode = mode;
    editor.querySelectorAll('.inline-mode-btn').forEach(button => {
      const active = button.dataset.mode === mode;
      button.classList.toggle('active', active);
      button.setAttribute('aria-selected', String(active));
    });
    if (mode !== 'source') schedulePreview(editor, 0);
  }

  async function refreshPreview(editor) {
    const status = editor.querySelector('.inline-preview-status');
    const body = editor.querySelector('.inline-preview-body');
    const solutionBox = editor.querySelector('.inline-preview-solution');
    const solutionBody = editor.querySelector('.inline-preview-solution-body');
    const noteBox = editor.querySelector('.inline-preview-note');
    const noteBody = editor.querySelector('.inline-preview-note-body');
    const generation = (previewGenerations.get(editor) || 0) + 1;
    previewGenerations.set(editor, generation);
    if (editor.dataset.new === '1' && !controls(editor).body.value.trim()) {
      body.innerHTML = '';
      solutionBody.innerHTML = '';
      solutionBox.hidden = true;
      noteBody.innerHTML = '';
      noteBox.hidden = true;
      status.hidden = false;
      status.textContent = '输入题目后显示预览';
      status.classList.remove('is-error');
      return;
    }
    status.hidden = false;
    status.textContent = '正在编译预览…';
    status.classList.remove('is-error');

    try {
      const previewUrl = editor.dataset.new === '1'
        ? '/question/inline-preview'
        : `/question/${encodeURIComponent(editor.dataset.id)}/preview`;
      const response = await fetch(previewUrl, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload(editor)),
      });
      const data = await response.json().catch(() => ({}));
      if (previewGenerations.get(editor) !== generation) return;
      if (!response.ok || !data.ok) throw new Error(data.error || '预览失败');
      body.innerHTML = data.body_html || '';
      solutionBody.innerHTML = data.solution_html || '';
      solutionBox.hidden = !data.solution_html;
      noteBody.innerHTML = data.note_html || '';
      noteBox.hidden = !data.note_html;
      status.hidden = true;
      window.QMath?.typeset(editor.querySelector('.inline-preview-pane'));
    } catch (error) {
      if (previewGenerations.get(editor) !== generation) return;
      status.hidden = false;
      status.textContent = error.message || '预览失败';
      status.classList.add('is-error');
    }
  }

  function schedulePreview(editor, delay) {
    clearTimeout(previewTimers.get(editor));
    previewTimers.set(editor, setTimeout(() => refreshPreview(editor), delay ?? 260));
  }

  function openEditor(card, focusField = 'body') {
    const editor = card.querySelector('.inline-editor');
    if (!editor) return;
    const focusTarget = focusField === 'note' ? controls(editor).note : controls(editor).body;
    if (!editor.hidden) {
      focusTarget.focus();
      return;
    }
    editor.hidden = false;
    card.classList.add('inline-editing');
    card.draggable = false;
    snapshots.set(editor, signature(editor));
    setStatus(editor, '');
    setMode(editor, 'live');
    focusTarget.focus();
  }

  function closeEditor(editor, force) {
    if (!force && snapshots.get(editor) !== signature(editor)
        && !window.confirm('放弃尚未保存的修改？')) return;
    const card = editor.closest('.card');
    if (editor.dataset.new === '1') {
      clearTimeout(previewTimers.get(editor));
      card.remove();
      const list = document.getElementById('q-list');
      if (!list?.querySelector('.card[data-id]')) {
        const empty = list?.querySelector('.empty-state');
        if (empty) empty.hidden = false;
      }
      return;
    }
    editor.hidden = true;
    card.classList.remove('inline-editing');
    card.draggable = true;
    clearTimeout(previewTimers.get(editor));
    setStatus(editor, '');
  }

  async function saveEditor(editor) {
    const card = editor.closest('.card');
    const save = editor.querySelector('.inline-save');
    save.disabled = true;
    setStatus(editor, '正在保存…');
    try {
      const isNew = editor.dataset.new === '1';
      const saveUrl = isNew
        ? '/question/inline-create'
        : `/question/${encodeURIComponent(editor.dataset.id)}/inline`;
      const response = await fetch(saveUrl, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload(editor)),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data.ok) throw new Error(data.error || '保存失败');
      if (isNew && typeof window.QQuestionCreated !== 'function') {
        throw new Error('新增题卡组件尚未就绪，请刷新页面后重试');
      }
      if (!isNew && typeof window.QReplaceQuestionCard !== 'function') {
        throw new Error('题卡刷新组件尚未就绪，请刷新页面后重试');
      }
      if (isNew) window.QQuestionCreated(card, data.card_html);
      else window.QReplaceQuestionCard(card, data.card_html);
    } catch (error) {
      save.disabled = false;
      setStatus(editor, error.message || '保存失败', true);
    }
  }

  document.addEventListener('click', event => {
    const trigger = event.target.closest('.inline-edit-trigger');
    if (trigger) {
      openEditor(trigger.closest('.card'), trigger.dataset.inlineFocus || 'body');
      return;
    }
    const editor = event.target.closest('.inline-editor');
    if (!editor) return;
    const mode = event.target.closest('.inline-mode-btn');
    if (mode) setMode(editor, mode.dataset.mode);
    else if (event.target.closest('.inline-save')) saveEditor(editor);
    else if (event.target.closest('.inline-cancel')) closeEditor(editor, false);
  });

  document.addEventListener('input', event => {
    const editor = event.target.closest('.inline-editor');
    if (!editor || !event.target.matches('textarea, input, select')) return;
    if (editor.querySelector('.inline-editor-workbench').dataset.mode !== 'source') {
      schedulePreview(editor, 260);
    }
  });

  document.addEventListener('change', event => {
    const editor = event.target.closest('.inline-editor');
    if (editor && event.target.matches('select')) schedulePreview(editor, 0);
  });

  document.addEventListener('keydown', event => {
    const editor = event.target.closest('.inline-editor');
    if (!editor) return;
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
      event.preventDefault();
      saveEditor(editor);
    } else if (event.key === 'Escape') {
      event.preventDefault();
      closeEditor(editor, false);
    }
  });

  window.QInlineEditor = {open: openEditor};
})();
