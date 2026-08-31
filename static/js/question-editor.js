(function () {
  'use strict';

  const editor = document.querySelector('.standalone-question-editor');
  if (!editor) return;
  const body = editor.querySelector('[name="body"]');
  const solution = editor.querySelector('[name="solution"]');
  const note = editor.querySelector('[name="note"]');
  const status = editor.querySelector('.inline-preview-status');
  const bodyPreview = editor.querySelector('.inline-preview-body');
  const solutionBox = editor.querySelector('.inline-preview-solution');
  const solutionPreview = editor.querySelector('.inline-preview-solution-body');
  const noteBox = editor.querySelector('.inline-preview-note');
  const notePreview = editor.querySelector('.inline-preview-note-body');
  let timer = 0;
  let generation = 0;

  function autoSize(textarea) {
    if (!textarea?.matches('.standalone-auto-textarea')) return;
    textarea.style.height = 'auto';
    const height = Math.max(84, Math.min(280, textarea.scrollHeight));
    textarea.style.height = `${height}px`;
    textarea.style.overflowY = textarea.scrollHeight > 280 ? 'auto' : 'hidden';
  }

  function previewEnabled(field) {
    return Boolean(editor.querySelector(`[data-preview-field="${field}"]`)?.checked);
  }

  async function renderPreview() {
    const current = ++generation;
    if (!body.value.trim()) {
      bodyPreview.replaceChildren();
      solutionPreview.replaceChildren();
      notePreview.replaceChildren();
      solutionBox.hidden = true;
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
      const response = await fetch(editor.dataset.previewUrl, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          body: body.value,
          solution: solution.value,
          note: note.value,
          type: editor.elements.type?.value || '',
          difficulty: editor.elements.difficulty?.value || '',
          source: editor.elements.source?.value || '',
          tags: editor.elements.tags?.value || '',
        }),
      });
      const data = await response.json().catch(() => ({}));
      if (current !== generation) return;
      if (!response.ok || !data.ok) throw new Error(data.error || '预览失败');
      bodyPreview.innerHTML = data.body_html || '';
      solutionPreview.innerHTML = data.solution_html || '';
      notePreview.innerHTML = data.note_html || '';
      solutionBox.hidden = !data.solution_html || !previewEnabled('solution');
      noteBox.hidden = !data.note_html || !previewEnabled('note');
      status.hidden = true;
      window.QMath?.typeset(editor.querySelector('.standalone-preview-pane'));
    } catch (error) {
      if (current !== generation) return;
      status.hidden = false;
      status.textContent = error.message || '预览失败';
      status.classList.add('is-error');
    }
  }

  function schedule(delay = 220) {
    window.clearTimeout(timer);
    timer = window.setTimeout(renderPreview, delay);
  }

  editor.querySelectorAll('.inline-preview-toggle').forEach(toggle => {
    toggle.addEventListener('click', event => event.stopPropagation());
    toggle.addEventListener('keydown', event => event.stopPropagation());
  });
  editor.addEventListener('input', event => {
    if (!event.target.matches('textarea, input, select')) return;
    autoSize(event.target);
    schedule();
  });
  editor.addEventListener('change', event => {
    if (event.target.matches('select, .standalone-preview-enabled')) schedule(0);
  });
  editor.querySelectorAll('.standalone-auto-textarea').forEach(autoSize);
  schedule(0);
}());
