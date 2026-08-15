import {Editor, Extension, Node, mergeAttributes} from '@tiptap/core';
import StarterKit from '@tiptap/starter-kit';
import {Markdown} from '@tiptap/markdown';
import {Mathematics} from '@tiptap/extension-mathematics';
import {NodeSelection, Plugin, PluginKey} from '@tiptap/pm/state';
import {Decoration, DecorationSet} from '@tiptap/pm/view';
import {
  PAGE_BREAK_MARKER,
  createAutosave,
  hasUnsavedWork,
  numberLabels,
  parseHandoutBody,
  questionMarker,
  reconcileSaveSuccess,
  sourceIsLocallyEdited,
} from './handout-model.js';
import {insertBlockAt, moveQuestionBefore} from './editor-operations.js';
import {PAGE_LAYOUTS, layoutPaginatedBlocks} from './pagination.js';

const app = document.getElementById('handout-app');
if (!app) throw new Error('讲义工作台挂载点不存在');

const elements = {
  documentList: document.getElementById('handout-document-list'),
  questionList: document.getElementById('handout-question-list'),
  title: document.getElementById('handout-title'),
  pageFormat: document.getElementById('handout-page-format'),
  solutionDefault: document.getElementById('handout-solution-default'),
  paperTone: document.getElementById('handout-paper-tone'),
  wimathLogo: document.getElementById('handout-wimath-logo'),
  saveState: document.getElementById('handout-save-state'),
  editor: document.getElementById('handout-editor'),
  paper: document.getElementById('handout-paper'),
  pageGuides: document.getElementById('handout-page-guides'),
  raw: document.getElementById('handout-raw-fallback'),
  conflict: document.getElementById('handout-conflict'),
  warning: document.getElementById('handout-warning'),
  previewDialog: document.getElementById('handout-preview-dialog'),
  previewFrame: document.getElementById('handout-preview-frame'),
  inspector: document.getElementById('handout-inspector'),
  inspectorSource: document.getElementById('handout-inspector-source'),
  inspectorNumber: document.getElementById('handout-inspector-number'),
  inspectorSolutionMode: document.getElementById('handout-inspector-solution-mode'),
  inspectorBody: document.getElementById('handout-inspector-body'),
  inspectorSolution: document.getElementById('handout-inspector-solution'),
  inspectorStatus: document.getElementById('handout-inspector-status'),
};

const state = {
  path: '',
  mtime: '',
  metadata: {},
  dirty: false,
  saving: false,
  loading: false,
  conflicted: false,
  readOnly: false,
  rawMode: false,
  selectedBlockId: '',
  inspectorDirty: false,
  inspectorSourceMeta: null,
  suppressAutoRender: false,
  revision: 0,
};

const questionViews = new Map();
const questionRenders = new Map();
const renderRequests = new Map();
let paginationFrame = 0;
let paginationRunning = false;

const paginationPluginKey = new PluginKey('handoutPagination');
const HandoutPagination = Extension.create({
  name: 'handoutPagination',
  addProseMirrorPlugins() {
    return [new Plugin({
      key: paginationPluginKey,
      state: {
        init: () => DecorationSet.empty,
        apply(transaction, previous) {
          const attrs = transaction.getMeta(paginationPluginKey);
          if (attrs) {
            const decorations = attrs.map(item => Decoration.node(
              item.from, item.to, item.attrs,
            ));
            return DecorationSet.create(transaction.doc, decorations);
          }
          return previous.map(transaction.mapping, transaction.doc);
        },
      },
      props: {
        decorations(editorState) { return paginationPluginKey.getState(editorState); },
      },
    })];
  },
});

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok || !data.ok) {
    const error = new Error(data.error || `请求失败（${response.status}）`);
    error.status = response.status;
    error.data = data;
    throw error;
  }
  return data;
}

function jsonOptions(payload) {
  return {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)};
}

function escapeText(value) {
  const span = document.createElement('span');
  span.textContent = String(value || '');
  return span.innerHTML;
}

function metadataFor(blockId) {
  return state.metadata.question_blocks?.[blockId] || {};
}

function renderMath(element) {
  if (!element || typeof window.renderMathInElement !== 'function') return;
  try {
    window.renderMathInElement(element, {
      delimiters: [
        {left: '$$', right: '$$', display: true},
        {left: '$', right: '$', display: false},
      ],
      throwOnError: false,
    });
  } catch (_error) { /* 非法 LaTeX 留源码，编辑不能因此中断。 */ }
}

function updateNodeAttributes(editor, getPos, attrs) {
  const pos = getPos();
  if (typeof pos !== 'number') return;
  const current = editor.state.doc.nodeAt(pos);
  if (!current) return;
  editor.view.dispatch(editor.state.tr.setNodeMarkup(pos, undefined, {...current.attrs, ...attrs}));
}

const HandoutQuestion = Node.create({
  name: 'handoutQuestion',
  group: 'block',
  atom: true,
  draggable: true,
  selectable: true,
  isolating: true,
  addAttributes() {
    return {
      blockId: {default: ''},
      body: {default: ''},
      solution: {default: ''},
      numberOverride: {default: null},
      solutionPlacement: {default: 'inherit'},
      confirmed: {default: false},
    };
  },
  parseHTML() { return [{tag: 'section[data-type="handout-question"]'}]; },
  renderHTML({HTMLAttributes}) {
    return ['section', mergeAttributes(HTMLAttributes, {'data-type': 'handout-question'})];
  },
  renderMarkdown(node) { return questionMarker(node.attrs); },
  addNodeView() {
    return ({node, editor, getPos}) => {
      let current = node;
      const dom = document.createElement('section');
      dom.className = 'handout-question-node';
      dom.dataset.questionNode = '1';
      dom.dataset.blockId = node.attrs.blockId;
      const handle = document.createElement('button');
      handle.type = 'button';
      handle.className = 'handout-question-handle';
      handle.dataset.dragHandle = '';
      handle.draggable = true;
      handle.title = '拖动题目重新排序';
      handle.setAttribute('aria-label', handle.title);
      handle.textContent = '⠿';
      handle.addEventListener('mousedown', () => {
        const pos = getPos();
        if (typeof pos !== 'number') return;
        editor.view.dispatch(editor.state.tr.setSelection(
          NodeSelection.create(editor.state.doc, pos)));
      });
      handle.addEventListener('dragstart', event => {
        event.dataTransfer.effectAllowed = 'move';
        event.dataTransfer.setData('application/x-quizforge-handout-block', current.attrs.blockId);
        // 后续移动由工作台统一处理；不再让 ProseMirror 同时复制一次节点。
        event.stopPropagation();
      });
      const render = document.createElement('div');
      render.className = 'handout-question-render';
      dom.append(handle, render);

      const redraw = (label = dom.dataset.numberLabel || '') => {
        const compiled = questionRenders.get(current.attrs.blockId);
        dom.classList.toggle('is-pending', !current.attrs.confirmed);
        dom.classList.toggle('is-rendering', Boolean(current.attrs.confirmed && compiled?.status === 'loading'));
        dom.classList.toggle('is-render-error', Boolean(compiled?.status === 'error'));
        if (current.attrs.confirmed && compiled?.url) {
          if (render.firstElementChild?.dataset?.renderUrl !== compiled.url) {
            const image = document.createElement('img');
            image.src = compiled.url;
            image.alt = `题卡 ${label}`;
            image.dataset.renderUrl = compiled.url;
            image.addEventListener('load', schedulePagination, {once: true});
            render.replaceChildren(image);
          }
        } else if (current.attrs.confirmed) {
          const status = document.createElement('div');
          status.className = 'handout-question-render-state';
          status.textContent = compiled?.status === 'error'
            ? `编译失败：${compiled.error || '请重新确认'}` : '正在编译 LaTeX…';
          render.replaceChildren(status);
        } else {
          const placeholder = document.createElement('div');
          placeholder.className = 'handout-question-placeholder';
          placeholder.textContent = `${label}${current.attrs.numberOverride ? ' ' : '. '}${current.attrs.body || '（空题干）'}`;
          renderMath(placeholder);
          render.replaceChildren(placeholder);
        }
      };
      dom.addEventListener('click', event => {
        if (event.target.closest('[data-drag-handle]')) return;
        const pos = getPos();
        if (typeof pos === 'number') {
          editor.view.dispatch(editor.state.tr.setSelection(NodeSelection.create(editor.state.doc, pos)));
        }
        openQuestionInspector(current.attrs.blockId);
      });
      questionViews.set(node.attrs.blockId, {dom, redraw, getNode: () => current});
      redraw();
      // setContent 创建 NodeView 与外层打开流程并非严格同一时序；以节点真正挂载完成
      // 为准再编号，避免初次打开时卡片只显示句点而缺少自动题号。
      queueMicrotask(renumberQuestions);
      setTimeout(renumberQuestions, 0);
      return {
        dom,
        update(updated) {
          if (updated.type.name !== 'handoutQuestion') return false;
          current = updated;
          dom.dataset.blockId = updated.attrs.blockId;
          redraw();
          return true;
        },
        stopEvent() { return true; },
        ignoreMutation() { return true; },
        destroy() {
          // setContent 可能先挂载同 blockId 的新 NodeView、再销毁旧实例；旧实例不能
          // 顺手把新实例刚登记的映射删掉，否则题号与编译结果都找不到当前卡片。
          if (questionViews.get(current.attrs.blockId)?.dom === dom) {
            questionViews.delete(current.attrs.blockId);
          }
        },
      };
    };
  },
});

const HandoutPageBreak = Node.create({
  name: 'handoutPageBreak',
  group: 'block',
  atom: true,
  selectable: true,
  parseHTML() { return [{tag: 'div[data-type="handout-page-break"]'}]; },
  renderHTML() { return ['div', {'data-type': 'handout-page-break'}]; },
  renderMarkdown() { return PAGE_BREAK_MARKER; },
  addNodeView() {
    return () => {
      const dom = document.createElement('div');
      dom.className = 'handout-page-break-node';
      dom.dataset.type = 'handout-page-break';
      dom.textContent = '显式分页';
      return {dom};
    };
  },
});

function schedulePagination() {
  if (paginationRunning) return;
  if (paginationFrame) cancelAnimationFrame(paginationFrame);
  paginationFrame = requestAnimationFrame(() => {
    paginationFrame = 0;
    paginateEditor();
  });
}

function renderPageGuides(layout) {
  const fragment = document.createDocumentFragment();
  for (let index = 0; index < layout.pageCount; index += 1) {
    const page = document.createElement('div');
    page.className = 'handout-page-surface';
    page.style.top = `${index * (layout.pageHeight + layout.pageGap)}px`;
    page.style.height = `${layout.pageHeight}px`;
    const number = document.createElement('span');
    number.textContent = `第 ${index + 1} 页`;
    page.append(number);
    fragment.append(page);
  }
  elements.pageGuides.replaceChildren(fragment);
}

function paginateEditor() {
  if (state.rawMode || elements.editor.hidden || paginationRunning) return;
  const root = elements.editor.querySelector('.handout-prosemirror');
  if (!root) return;
  paginationRunning = true;
  const layoutName = elements.pageFormat.value || 'a4-1';
  const spec = PAGE_LAYOUTS[layoutName] || PAGE_LAYOUTS['a4-1'];
  const contentWidth = spec.pageWidth - spec.left - spec.right;
  const columnWidth = (contentWidth - spec.columnGap * (spec.columns - 1)) / spec.columns;
  const docNodes = [];
  const measuredDoc = editor.state.doc;
  editor.state.doc.forEach((node, offset) => {
    docNodes.push({node, from: offset, to: offset + node.nodeSize});
  });
  const measureAttrs = docNodes.map(item => ({
    from: item.from,
    to: item.to,
    attrs: {
      style: `position:relative;width:${item.node.type.name === 'handoutPageBreak' ? contentWidth : columnWidth}px;`,
    },
  }));
  editor.view.dispatch(editor.state.tr.setMeta(paginationPluginKey, measureAttrs));

  requestAnimationFrame(() => {
    try {
      if (editor.state.doc !== measuredDoc) {
        requestAnimationFrame(schedulePagination);
        return;
      }
      const children = [...root.children];
      if (children.length !== docNodes.length) {
        requestAnimationFrame(schedulePagination);
        return;
      }
      const blocks = children.map((child, index) => {
        const computed = getComputedStyle(child);
        const node = docNodes[index].node;
        const meta = node.type.name === 'handoutQuestion'
          ? metadataFor(node.attrs.blockId) : {};
        return {
          kind: node.type.name === 'handoutPageBreak' ? 'pageBreak' : 'block',
          practiceSolve: node.type.name === 'handoutQuestion'
            && !['单选题', '多选题', '填空题'].includes(meta.question_type),
          height: child.offsetHeight,
          marginTop: parseFloat(computed.marginTop) || 0,
          marginBottom: parseFloat(computed.marginBottom) || 0,
        };
      });
      const layout = layoutPaginatedBlocks(blocks, layoutName);
      const finalAttrs = docNodes.map((item, index) => {
        const placement = layout.placements[index];
        return {
          from: item.from,
          to: item.to,
          attrs: {
            style: `position:absolute;left:${placement.x}px;top:${placement.y}px;width:${placement.width}px;margin-top:0;margin-bottom:0;`,
            'data-handout-page': String(placement.page + 1),
            'data-handout-column': String(placement.column + 1),
          },
        };
      });
      editor.view.dispatch(editor.state.tr.setMeta(paginationPluginKey, finalAttrs));
      elements.editor.style.height = `${layout.paperHeight}px`;
      elements.paper.style.height = `${layout.paperHeight}px`;
      renderPageGuides(layout);
    } finally {
      paginationRunning = false;
    }
  });
}

function editMath(kind, node, pos) {
  const next = window.prompt(kind === 'inline' ? '编辑行内公式 LaTeX' : '编辑块公式 LaTeX', node.attrs.latex || '');
  if (next === null) return;
  if (kind === 'inline') editor.commands.updateInlineMath({latex: next, pos});
  else editor.commands.updateBlockMath({latex: next, pos});
}

const editor = new Editor({
  element: elements.editor,
  content: '',
  extensions: [
    StarterKit.configure({heading: {levels: [1, 2, 3, 4, 5, 6]}}),
    Markdown,
    Mathematics.configure({
      katexOptions: {throwOnError: false},
      inlineOptions: {onClick: (node, pos) => editMath('inline', node, pos)},
      blockOptions: {onClick: (node, pos) => editMath('block', node, pos)},
    }),
    HandoutQuestion,
    HandoutPageBreak,
    HandoutPagination,
  ],
  editorProps: {attributes: {class: 'handout-prosemirror', spellcheck: 'false'}},
  onUpdate() {
    if (state.loading) return;
    markDirty();
    renumberQuestions();
    schedulePagination();
  },
  onSelectionUpdate: updateToolbarState,
});

function setStatus(message, kind = '') {
  elements.saveState.textContent = message;
  elements.saveState.dataset.kind = kind;
}

function markDirty() {
  if (!state.path || state.readOnly) return;
  state.revision += 1;
  state.dirty = true;
  setStatus(state.conflicted ? '自动保存已暂停' : '未保存', state.conflicted ? 'error' : 'dirty');
  if (!state.conflicted) autosave.schedule();
}

function applyPageLayout() {
  const value = elements.pageFormat.value;
  elements.paper.classList.toggle('is-slides', value === 'slides');
  elements.paper.classList.toggle('is-a4', value !== 'slides');
  elements.paper.classList.toggle('is-two-column', value === 'a4-2');
  elements.paper.classList.toggle('is-one-column', value !== 'a4-2');
  elements.paper.classList.toggle('is-cream', elements.paperTone.value === 'cream');
  questionRenders.clear();
  schedulePagination();
  queueMicrotask(renumberQuestions);
}

function applyAvailability() {
  const hasDocument = Boolean(state.path);
  const canEdit = hasDocument && !state.readOnly;
  editor.setEditable(canEdit && !state.rawMode);
  elements.raw.readOnly = !canEdit;
  [elements.title, elements.pageFormat, elements.solutionDefault, elements.paperTone, elements.wimathLogo,
    document.getElementById('handout-save'), ...document.querySelectorAll('[data-hf]')]
    .forEach(control => { control.disabled = !canEdit; });
  document.querySelectorAll('.handout-toolbar button, #handout-block-style').forEach(control => {
    control.disabled = !canEdit || state.rawMode;
  });
  document.getElementById('handout-preview').disabled = !hasDocument;
  document.getElementById('handout-export').disabled = !hasDocument;
  [elements.inspectorNumber, elements.inspectorSolutionMode, elements.inspectorBody,
    elements.inspectorSolution, document.getElementById('handout-inspector-refresh'),
    document.getElementById('handout-inspector-delete'),
    document.getElementById('handout-inspector-confirm')]
    .forEach(control => { control.disabled = !canEdit; });
}

function metadataFromControls() {
  const page = elements.pageFormat.value;
  state.metadata.title = elements.title.value.trim() || '新建讲义';
  state.metadata.page_format = page === 'slides' ? 'slides' : 'a4';
  state.metadata.columns = page === 'a4-2' ? 2 : 1;
  state.metadata.solution_default = elements.solutionDefault.value;
  state.metadata.paper_tone = elements.paperTone.value;
  state.metadata.wimath_logo = elements.wimathLogo.checked;
  state.metadata.header_footer ||= {};
  document.querySelectorAll('[data-hf]').forEach(input => {
    state.metadata.header_footer[input.dataset.hf] = input.value;
  });
}

function syncQuestionMetadata() {
  const current = state.metadata.question_blocks || {};
  const next = {};
  editor.state.doc.descendants(node => {
    if (node.type.name !== 'handoutQuestion') return;
    const base = current[node.attrs.blockId] || {};
    next[node.attrs.blockId] = {
      ...base,
      number_override: node.attrs.numberOverride || null,
      solution_placement: node.attrs.solutionPlacement || 'inherit',
      render_confirmed: Boolean(node.attrs.confirmed),
    };
  });
  state.metadata.question_blocks = next;
}

function serializeBody() {
  if (state.rawMode) return elements.raw.value.replace(/\r\n?/g, '\n');
  syncQuestionMetadata();
  return editor.getMarkdown().replace(/\r\n?/g, '\n');
}

async function saveDocument(force = false) {
  if (!state.path || state.readOnly || state.conflicted) return false;
  if (state.inspectorDirty && !state.dirty) {
    setStatus('题卡修改尚未确认，请先点击“确定并编译”', 'dirty');
    return false;
  }
  if (state.saving) {
    autosave.schedule();
    return false;
  }
  if (!state.dirty && !force) return true;
  state.saving = true;
  let rescheduleAfterSave = false;
  metadataFromControls();
  const saveRevision = state.revision;
  const payload = {
    path: state.path,
    mtime: state.mtime,
    metadata: state.metadata,
    body: serializeBody(),
  };
  setStatus('正在保存…');
  try {
    const data = await fetchJson('/api/handouts/write', jsonOptions(payload));
    state.mtime = data.mtime;
    const reconciled = reconcileSaveSuccess({
      saveRevision,
      currentRevision: state.revision,
      currentMetadata: state.metadata,
      savedMetadata: data.metadata,
    });
    state.metadata = reconciled.metadata;
    state.dirty = reconciled.dirty;
    rescheduleAfterSave = reconciled.reschedule;
    if (reconciled.current && !state.inspectorDirty) setStatus('已保存', 'saved');
    else setStatus(state.inspectorDirty ? '题卡修改尚未确认' : '仍有未保存修改', 'dirty');
    loadDocumentList();
    return reconciled.current && !state.inspectorDirty;
  } catch (error) {
    if (error.status === 409 || error.data?.conflict) {
      state.conflicted = true;
      elements.conflict.hidden = false;
      setStatus('外部修改冲突', 'error');
    } else {
      setStatus(`保存失败：${error.message}`, 'error');
    }
    return false;
  } finally {
    state.saving = false;
    if ((rescheduleAfterSave || (state.dirty && state.revision !== saveRevision))
        && state.dirty && !state.conflicted) autosave.schedule();
  }
}

const autosave = createAutosave(() => saveDocument(), 1000);

function blocksToJson(body, metadata) {
  const parsed = parseHandoutBody(body, metadata.question_blocks || {});
  const content = [];
  parsed.blocks.forEach(block => {
    if (block.kind === 'markdown') {
      const doc = editor.markdown.parse(block.text);
      content.push(...(doc.content || []));
    } else if (block.kind === 'pageBreak') {
      content.push({type: 'handoutPageBreak'});
    } else {
      content.push({type: 'handoutQuestion', attrs: {
        blockId: block.blockId,
        body: block.body,
        solution: block.solution,
        numberOverride: block.numberOverride,
        solutionPlacement: block.solutionPlacement,
        confirmed: Boolean(metadata.question_blocks?.[block.blockId]?.render_confirmed),
      }});
    }
  });
  if (!content.length) content.push({type: 'paragraph'});
  return {doc: {type: 'doc', content}, warnings: parsed.warnings};
}

function renderWarnings(warnings) {
  elements.warning.hidden = !warnings.length;
  elements.warning.textContent = warnings.join('；');
}

async function openDocument(path, {discardCurrent = false} = {}) {
  if (!path) return;
  if (state.inspectorDirty
      && !window.confirm('题卡修改尚未确认，确定放弃并切换讲义吗？')) return;
  if (state.inspectorDirty) {
    state.inspectorDirty = false;
    closeQuestionInspector(true);
  }
  if (!discardCurrent && state.conflicted && state.dirty) {
    window.alert('当前讲义存在外部修改冲突，请先“重新载入磁盘”或“另存新讲义”。');
    return;
  }
  if (!discardCurrent && state.dirty && !state.conflicted && !(await saveDocument())) return;
  autosave.cancel();
  state.loading = true;
  setStatus('正在打开…');
  try {
    const data = await fetchJson(`/api/handouts/read?path=${encodeURIComponent(path)}`);
    state.path = data.path;
    state.mtime = data.mtime;
    state.metadata = data.metadata;
    state.readOnly = Boolean(data.read_only);
    state.conflicted = false;
    state.dirty = false;
    state.revision = 0;
    elements.conflict.hidden = true;
    elements.title.value = data.metadata.title || '';
    elements.pageFormat.value = data.metadata.page_format === 'slides' ? 'slides'
      : Number(data.metadata.columns) === 2 ? 'a4-2' : 'a4-1';
    elements.solutionDefault.value = data.metadata.solution_default || 'hidden';
    elements.paperTone.value = data.metadata.paper_tone || 'white';
    elements.wimathLogo.checked = Boolean(data.metadata.wimath_logo);
    document.querySelectorAll('[data-hf]').forEach(input => {
      input.value = data.metadata.header_footer?.[input.dataset.hf] || '';
    });
    const parsed = blocksToJson(data.body, data.metadata);
    const structuralWarnings = parsed.warnings.filter(message => /缺少|重复/.test(message));
    state.rawMode = structuralWarnings.length > 0;
    elements.raw.hidden = !state.rawMode;
    elements.editor.hidden = state.rawMode;
    if (state.rawMode) elements.raw.value = data.body;
    else editor.commands.setContent(parsed.doc);
    applyAvailability();
    renderWarnings([...(data.warnings || []), ...parsed.warnings]);
    applyPageLayout();
    setStatus(state.readOnly ? '未来 schema，只读' : state.rawMode ? '结构损坏：原文保护模式' : '已保存',
      state.readOnly || state.rawMode ? 'warning' : 'saved');
    const url = new URL(location.href);
    url.searchParams.set('path', data.path);
    history.replaceState(null, '', url);
    renderDocumentSelection();
    questionRenders.clear();
    closeQuestionInspector(true);
    queueMicrotask(() => { renumberQuestions(); schedulePagination(); });
  } catch (error) {
    setStatus(`打开失败：${error.message}`, 'error');
  } finally {
    state.loading = false;
  }
}

async function loadDocumentList(openFirst = false) {
  try {
    const data = await fetchJson('/api/handouts');
    elements.documentList.replaceChildren();
    if (!data.documents.length) {
      if (!state.path) {
        state.loading = true;
        editor.commands.setContent({type: 'doc', content: [{type: 'paragraph'}]});
        state.loading = false;
        elements.title.value = '';
        elements.raw.value = '';
        elements.raw.hidden = true;
        elements.editor.hidden = false;
        renderWarnings([]);
        applyAvailability();
        applyPageLayout();
        setStatus('尚未创建讲义');
        const url = new URL(location.href);
        url.searchParams.delete('path');
        history.replaceState(null, '', url);
      }
      const empty = document.createElement('p');
      empty.className = 'muted';
      empty.textContent = '还没有讲义，点击“新建”开始。';
      elements.documentList.append(empty);
      return;
    }
    data.documents.forEach(documentInfo => {
      const row = document.createElement('div');
      row.className = 'handout-document-row';
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'handout-document-item';
      button.dataset.path = documentInfo.path;
      button.innerHTML = `<strong>${escapeText(documentInfo.title)}</strong><span>${documentInfo.page_format === 'slides' ? '16:9' : `A4 ${documentInfo.columns === 2 ? '双栏' : '单栏'}`}</span>`;
      button.addEventListener('click', () => openDocument(documentInfo.path));
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'handout-document-delete';
      remove.title = `删除讲义“${documentInfo.title}”`;
      remove.setAttribute('aria-label', remove.title);
      remove.textContent = '×';
      remove.addEventListener('click', async () => {
        if (documentInfo.path === state.path && hasUnsavedWork(state.dirty, state.inspectorDirty)) {
          window.alert('当前讲义还有未保存修改，请先保存或放弃修改后再删除。');
          return;
        }
        if (!window.confirm(`确定永久删除讲义“${documentInfo.title}”吗？此操作不会删除原题。`)) return;
        try {
          await fetchJson('/api/handouts/delete', jsonOptions({
            path: documentInfo.path,
            mtime: documentInfo.mtime,
          }));
          const wasCurrent = documentInfo.path === state.path;
          if (wasCurrent) {
            state.path = '';
            state.mtime = '';
            state.metadata = {};
            state.dirty = false;
            state.revision = 0;
            questionRenders.clear();
            closeQuestionInspector(true);
          }
          await loadDocumentList(wasCurrent);
        } catch (error) {
          window.alert(`删除失败：${error.message}`);
          if (error.status === 409) loadDocumentList();
        }
      });
      row.append(button, remove);
      elements.documentList.append(row);
    });
    renderDocumentSelection();
    if (openFirst && !state.path) openDocument(data.documents[0].path);
  } catch (error) {
    elements.documentList.textContent = `读取失败：${error.message}`;
  }
}

function renderDocumentSelection() {
  document.querySelectorAll('.handout-document-item').forEach(button => {
    button.classList.toggle('is-active', button.dataset.path === state.path);
  });
}

async function loadSelectedQuestions() {
  try {
    const data = await fetchJson('/api/handouts/selected');
    elements.questionList.replaceChildren();
    if (!data.questions.length) {
      const empty = document.createElement('p');
      empty.className = 'muted';
      empty.textContent = '选题篮为空。请先在题库勾选题目。';
      elements.questionList.append(empty);
      return;
    }
    data.questions.forEach(question => {
      const card = document.createElement('article');
      card.className = 'handout-source-question';
      card.draggable = true;
      card.dataset.qid = question.id;
      card.innerHTML = `<div><span>${escapeText(question.type)}</span><small>${escapeText(question.source)}</small></div><p>${escapeText(question.excerpt)}</p><button type="button">插入到光标</button>`;
      card.addEventListener('dragstart', event => {
        event.dataTransfer.effectAllowed = 'copy';
        event.dataTransfer.setData('application/x-quizforge-question', question.id);
        event.dataTransfer.setData('text/plain', question.id);
      });
      card.querySelector('button').addEventListener('click', () => insertQuestion(question.id));
      elements.questionList.append(card);
    });
  } catch (error) {
    elements.questionList.textContent = `读取失败：${error.message}`;
  }
}

function insertQuestionNode(snapshot, position) {
  state.metadata.question_blocks ||= {};
  state.metadata.question_blocks[snapshot.block_id] = {...snapshot.metadata, render_confirmed: false};
  const content = {type: 'handoutQuestion', attrs: {
    blockId: snapshot.block_id,
    body: snapshot.body,
    solution: snapshot.solution,
    numberOverride: null,
    solutionPlacement: 'inherit',
    confirmed: false,
  }};
  insertBlockAt(editor, position, content);
  markDirty();
  queueMicrotask(() => {
    renumberQuestions();
    openQuestionInspector(snapshot.block_id);
  });
}

async function insertQuestion(qid, position = editor.state.selection.from) {
  if (!state.path || state.readOnly || state.rawMode) return;
  try {
    const snapshot = await fetchJson(`/api/handouts/question/${encodeURIComponent(qid)}`);
    insertQuestionNode(snapshot, position);
  } catch (error) {
    window.alert(`插入题目失败：${error.message}`);
  }
}

function questionEntries() {
  const entries = [];
  editor.state.doc.descendants((node, pos) => {
    if (node.type.name === 'handoutQuestion') entries.push({node, pos, position: entries.length + 1});
  });
  return entries;
}

function questionEntry(blockId) {
  return questionEntries().find(entry => entry.node.attrs.blockId === blockId) || null;
}

function updateQuestionNode(blockId, attrs) {
  const entry = questionEntry(blockId);
  if (!entry) return false;
  editor.view.dispatch(editor.state.tr.setNodeMarkup(
    entry.pos, undefined, {...entry.node.attrs, ...attrs}));
  return true;
}

function renderInput(entry) {
  const attrs = entry.node.attrs;
  const meta = metadataFor(attrs.blockId);
  const question = {
    ...meta,
    block_id: attrs.blockId,
    body: attrs.body,
    solution: attrs.solution,
    number_override: attrs.numberOverride || null,
    solution_placement: attrs.solutionPlacement || 'inherit',
  };
  const signature = JSON.stringify({
    page_format: state.metadata.page_format,
    columns: state.metadata.columns,
    solution_default: state.metadata.solution_default,
    position: entry.position,
    question,
  });
  return {question, signature};
}

async function ensureQuestionRendered(blockId, force = false) {
  const entry = questionEntry(blockId);
  if (!entry || !entry.node.attrs.confirmed) return false;
  const {question, signature} = renderInput(entry);
  const previous = questionRenders.get(blockId);
  if (!force && previous?.signature === signature && (previous.url || previous.status === 'loading')) {
    return Boolean(previous.url);
  }
  const requestId = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}_${Math.random()}`;
  renderRequests.set(blockId, requestId);
  questionRenders.set(blockId, {status: 'loading', signature});
  questionViews.get(blockId)?.redraw();
  try {
    const data = await fetchJson('/api/handouts/render-question', jsonOptions({
      metadata: state.metadata,
      question,
      position: entry.position,
    }));
    if (renderRequests.get(blockId) !== requestId) return false;
    questionRenders.set(blockId, {
      status: 'ready', signature, url: data.url, cacheKey: data.cache_key,
    });
    questionViews.get(blockId)?.redraw();
    return true;
  } catch (error) {
    if (renderRequests.get(blockId) !== requestId) return false;
    questionRenders.set(blockId, {status: 'error', signature, error: error.message});
    questionViews.get(blockId)?.redraw();
    schedulePagination();
    if (state.selectedBlockId === blockId) setInspectorStatus(error.message, true);
    return false;
  }
}

function renumberQuestions() {
  // 绝对定位只改变视觉坐标，不改变 ProseMirror 顶层 DOM 的逻辑顺序；直接按 DOM
  // 顺序编号还能覆盖 NodeView 刚创建、文档 transaction 尚未完全收口的短暂窗口。
  const nodes = [...elements.editor.querySelectorAll('.handout-question-node')];
  const views = nodes.map(dom => questionViews.get(dom.dataset.blockId));
  const labels = numberLabels(views.map(view => ({
    numberOverride: view?.getNode().attrs.numberOverride,
  })));
  views.forEach((view, index) => {
    if (!view) return;
    const current = view.getNode();
    const blockId = current.attrs.blockId;
    if (view?.dom) view.dom.dataset.numberLabel = labels[index];
    view?.redraw(labels[index]);
    if (current.attrs.confirmed && !state.suppressAutoRender) ensureQuestionRendered(blockId);
  });
}

function setInspectorStatus(message, error = false) {
  elements.inspectorStatus.textContent = message;
  elements.inspectorStatus.classList.toggle('is-error', error);
}

function inspectorDraft() {
  return {
    numberOverride: elements.inspectorNumber.value.trim() || null,
    solutionPlacement: elements.inspectorSolutionMode.value || 'inherit',
    body: elements.inspectorBody.value,
    solution: elements.inspectorSolution.value,
  };
}

function markInspectorDirty() {
  if (!state.selectedBlockId) return;
  state.inspectorDirty = true;
  setInspectorStatus('有尚未确认的修改。');
  setStatus('题卡修改尚未确认', 'dirty');
}

function closeQuestionInspector(force = false) {
  if (!force && state.inspectorDirty
      && !window.confirm('题卡修改尚未确认，确定关闭并放弃这些修改吗？')) return false;
  state.selectedBlockId = '';
  state.inspectorDirty = false;
  state.inspectorSourceMeta = null;
  elements.inspector.hidden = true;
  app.classList.remove('has-inspector');
  schedulePagination();
  return true;
}

function openQuestionInspector(blockId) {
  if (!blockId) return;
  if (state.selectedBlockId === blockId) return;
  if (state.selectedBlockId && state.selectedBlockId !== blockId && state.inspectorDirty
      && !window.confirm('上一道题有尚未确认的修改，是否放弃并切换题卡？')) return;
  const entry = questionEntry(blockId);
  if (!entry) return;
  const attrs = entry.node.attrs;
  const meta = metadataFor(blockId);
  state.selectedBlockId = blockId;
  state.inspectorDirty = false;
  state.inspectorSourceMeta = {...meta};
  elements.inspectorSource.textContent = [
    meta.question_type || '未分类', meta.source || meta.source_path || '来源快照',
  ].filter(Boolean).join(' · ');
  elements.inspectorNumber.value = attrs.numberOverride || '';
  elements.inspectorSolutionMode.value = attrs.solutionPlacement || 'inherit';
  elements.inspectorBody.value = attrs.body || '';
  elements.inspectorSolution.value = attrs.solution || '';
  elements.inspector.hidden = false;
  app.classList.add('has-inspector');
  setInspectorStatus(attrs.confirmed ? '修改后点击“确定并编译”。' : '设置完成后点击“确定并编译”。');
  schedulePagination();
}

async function confirmQuestionInspector() {
  const blockId = state.selectedBlockId;
  const entry = questionEntry(blockId);
  if (!entry || state.readOnly) return;
  const draft = inspectorDraft();
  metadataFromControls();
  state.metadata.question_blocks ||= {};
  state.metadata.question_blocks[blockId] = {
    ...(state.inspectorSourceMeta || metadataFor(blockId)),
    number_override: draft.numberOverride,
    solution_placement: draft.solutionPlacement,
    render_confirmed: true,
  };
  state.suppressAutoRender = true;
  updateQuestionNode(blockId, {...draft, confirmed: true});
  state.suppressAutoRender = false;
  state.inspectorDirty = false;
  markDirty();
  setInspectorStatus('正在使用 XeLaTeX 编译…');
  const rendered = await ensureQuestionRendered(blockId, true);
  if (rendered && state.selectedBlockId === blockId && !state.inspectorDirty) {
    closeQuestionInspector(true);
  }
}

async function refreshInspectorSource() {
  const blockId = state.selectedBlockId;
  const meta = metadataFor(blockId);
  if (!meta.source_id) return;
  const draft = inspectorDraft();
  if (sourceIsLocallyEdited(draft, meta)
      && !window.confirm('这道题已在讲义内修改。刷新来源会替换题干和解析，是否继续？')) return;
  try {
    const fresh = await fetchJson(`/api/handouts/question/${encodeURIComponent(meta.source_id)}`);
    state.inspectorSourceMeta = {...fresh.metadata};
    elements.inspectorBody.value = fresh.body;
    elements.inspectorSolution.value = fresh.solution;
    state.inspectorDirty = true;
    setInspectorStatus('已载入最新来源，点击“确定并编译”后才会写入讲义。');
  } catch (error) {
    setInspectorStatus(`刷新失败：${error.message}`, true);
  }
}

function deleteInspectorQuestion() {
  const blockId = state.selectedBlockId;
  const entry = questionEntry(blockId);
  if (!entry || !window.confirm('确定从当前讲义删除这道题吗？原题和选题篮不会受影响。')) return;
  editor.view.dispatch(editor.state.tr.delete(entry.pos, entry.pos + entry.node.nodeSize));
  if (state.metadata.question_blocks) delete state.metadata.question_blocks[blockId];
  questionRenders.delete(blockId);
  renderRequests.delete(blockId);
  closeQuestionInspector(true);
  markDirty();
  queueMicrotask(renumberQuestions);
}

function updateToolbarState() {
  document.querySelectorAll('[data-command="bold"], [data-command="italic"]').forEach(button => {
    button.classList.toggle('is-active', editor.isActive(button.dataset.command));
  });
  const select = document.getElementById('handout-block-style');
  for (let level = 1; level <= 6; level += 1) {
    if (editor.isActive('heading', {level})) {
      select.value = `h${level}`;
      return;
    }
  }
  select.value = 'paragraph';
}

function runToolbar(command) {
  const chain = editor.chain().focus();
  if (command === 'bold') chain.toggleBold().run();
  else if (command === 'italic') chain.toggleItalic().run();
  else if (command === 'bullet') chain.toggleBulletList().run();
  else if (command === 'ordered') chain.toggleOrderedList().run();
  else if (command === 'quote') chain.toggleBlockquote().run();
  else if (command === 'undo') chain.undo().run();
  else if (command === 'redo') chain.redo().run();
  else if (command === 'inline-math') {
    const latex = window.prompt('输入行内公式 LaTeX', 'x');
    if (latex) chain.insertInlineMath({latex}).run();
  } else if (command === 'block-math') {
    const latex = window.prompt('输入块公式 LaTeX', String.raw`\sum_{i=1}^n i`);
    if (latex) chain.insertBlockMath({latex}).run();
  } else if (command === 'page-break') chain.insertContent({type: 'handoutPageBreak'}).run();
}

async function exportCurrent(preview = false) {
  if (!state.path) return;
  metadataFromControls();
  const button = document.getElementById(preview ? 'handout-preview' : 'handout-export');
  const oldText = button.textContent;
  button.disabled = true;
  button.textContent = '正在生成…';
  try {
    const fmt = preview ? 'pdf' : document.getElementById('handout-export-format').value;
    const data = await fetchJson(preview ? '/api/handouts/preview' : '/api/handouts/export',
      jsonOptions({metadata: state.metadata, body: serializeBody(), fmt}));
    if (preview) {
      elements.previewFrame.src = data.url;
      elements.previewDialog.showModal();
    } else if (window.parent !== window) {
      window.parent.postMessage({source: 'quizforge', type: 'download',
        url: new URL(data.url, location.href).href, filename: data.filename}, '*');
    } else {
      const anchor = document.createElement('a');
      anchor.href = data.url;
      anchor.download = data.filename || '';
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
    }
  } catch (error) {
    window.alert(`${preview ? '预览' : '导出'}失败：${error.message}`);
  } finally {
    button.disabled = false;
    button.textContent = oldText;
  }
}

document.getElementById('handout-create').addEventListener('click', async () => {
  if (state.inspectorDirty) {
    window.alert('请先确认题卡修改，或关闭右侧编辑框并放弃草稿。');
    return;
  }
  const title = window.prompt('新讲义标题', '新建讲义');
  if (title === null) return;
  try {
    const data = await fetchJson('/api/handouts', jsonOptions({title: title.trim() || '新建讲义'}));
    await loadDocumentList();
    await openDocument(data.path);
  } catch (error) {
    window.alert(`新建讲义失败：${error.message}`);
  }
});
document.getElementById('handout-refresh-selected').addEventListener('click', loadSelectedQuestions);
document.getElementById('handout-save').addEventListener('click', () => saveDocument(true));
document.getElementById('handout-preview').addEventListener('click', () => {
  if (state.inspectorDirty) window.alert('请先点击“确定并编译”，再预览讲义。');
  else exportCurrent(true);
});
document.getElementById('handout-export').addEventListener('click', () => {
  if (state.inspectorDirty) window.alert('请先点击“确定并编译”，再导出讲义。');
  else exportCurrent(false);
});
document.getElementById('handout-preview-close').addEventListener('click', () => elements.previewDialog.close());
document.getElementById('handout-inspector-close').addEventListener('click', () => closeQuestionInspector());
document.getElementById('handout-inspector-confirm').addEventListener('click', confirmQuestionInspector);
document.getElementById('handout-inspector-refresh').addEventListener('click', refreshInspectorSource);
document.getElementById('handout-inspector-delete').addEventListener('click', deleteInspectorQuestion);
document.getElementById('handout-reload').addEventListener('click', () => {
  if (window.confirm('确定放弃当前内存草稿并重新载入磁盘版本吗？')) {
    openDocument(state.path, {discardCurrent: true});
  }
});
document.getElementById('handout-save-as').addEventListener('click', async () => {
  if (state.inspectorDirty) {
    window.alert('请先确认题卡修改，或关闭右侧编辑框并放弃草稿。');
    return;
  }
  const title = window.prompt('副本标题', `${elements.title.value || '讲义'} 副本`);
  if (title === null) return;
  metadataFromControls();
  try {
    const data = await fetchJson('/api/handouts/save-as', jsonOptions({
      title, metadata: state.metadata, body: serializeBody(),
    }));
    // 当前内存草稿已经完整写进副本；否则 openDocument 会把它当成“原文件仍有
    // 未保存修改”，再次保存旧路径并立刻撞上同一个 mtime 冲突，导致副本打不开。
    state.dirty = false;
    state.conflicted = false;
    await loadDocumentList();
    await openDocument(data.path);
  } catch (error) {
    window.alert(`另存失败：${error.message}`);
  }
});

document.querySelectorAll('.handout-toolbar [data-command]').forEach(button => {
  button.addEventListener('mousedown', event => event.preventDefault());
  button.addEventListener('click', () => runToolbar(button.dataset.command));
});
document.getElementById('handout-block-style').addEventListener('change', event => {
  const value = event.target.value;
  if (value === 'paragraph') editor.chain().focus().setParagraph().run();
  else editor.chain().focus().setHeading({level: Number(value.slice(1))}).run();
});

[elements.title, elements.wimathLogo, ...document.querySelectorAll('[data-hf]')]
  .forEach(control => control.addEventListener('input', markDirty));
elements.solutionDefault.addEventListener('change', () => {
  metadataFromControls();
  questionRenders.clear();
  markDirty();
  renumberQuestions();
});
elements.paperTone.addEventListener('change', () => {
  elements.paper.classList.toggle('is-cream', elements.paperTone.value === 'cream');
  markDirty();
});
elements.pageFormat.addEventListener('change', () => {
  metadataFromControls();
  applyPageLayout();
  markDirty();
});
elements.raw.addEventListener('input', markDirty);
[elements.inspectorNumber, elements.inspectorSolutionMode,
  elements.inspectorBody, elements.inspectorSolution]
  .forEach(control => control.addEventListener('input', markInspectorDirty));

elements.editor.addEventListener('compositionstart', () => autosave.beginComposition(), true);
elements.editor.addEventListener('compositionend', () => autosave.endComposition(), true);
elements.editor.addEventListener('dragover', event => {
  if (event.dataTransfer.types.includes('application/x-quizforge-question')
      || event.dataTransfer.types.includes('application/x-quizforge-handout-block')) {
    event.preventDefault();
    event.dataTransfer.dropEffect = event.dataTransfer.types.includes('application/x-quizforge-handout-block')
      ? 'move' : 'copy';
  }
}, true);
elements.editor.addEventListener('drop', event => {
  const movingBlockId = event.dataTransfer.getData('application/x-quizforge-handout-block');
  if (movingBlockId) {
    event.preventDefault();
    event.stopPropagation();
    const targetBlockId = event.target.closest('.handout-question-node')?.dataset.blockId;
    if (!targetBlockId || targetBlockId === movingBlockId) return;
    moveQuestionBefore(editor, movingBlockId, targetBlockId);
    queueMicrotask(renumberQuestions);
    return;
  }
  const qid = event.dataTransfer.getData('application/x-quizforge-question');
  if (!qid) return;
  event.preventDefault();
  event.stopPropagation();
  const found = editor.view.posAtCoords({left: event.clientX, top: event.clientY});
  insertQuestion(qid, found?.pos ?? editor.state.selection.from);
}, true);

document.addEventListener('keydown', event => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
    event.preventDefault();
    saveDocument(true);
  }
});

window.addEventListener('beforeunload', event => {
  if (!hasUnsavedWork(state.dirty, state.inspectorDirty)) return;
  event.preventDefault();
  event.returnValue = '';
});

applyPageLayout();
applyAvailability();
loadSelectedQuestions();
const initialPath = app.dataset.initialPath;
loadDocumentList(!initialPath).then(() => {
  if (initialPath) openDocument(initialPath);
});
