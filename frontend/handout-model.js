export const PAGE_BREAK_MARKER = '<!-- quizforge:page-break -->';

const QUESTION_OPEN = /^\s*<!--\s*quizforge:question\s+([A-Za-z0-9_-]{6,80})\s*-->\s*$/;
const QUESTION_SOLUTION = /^\s*<!--\s*quizforge:solution\s+([A-Za-z0-9_-]{6,80})\s*-->\s*$/;
const QUESTION_END = /^\s*<!--\s*quizforge:end\s+([A-Za-z0-9_-]{6,80})\s*-->\s*$/;

function splitPlain(text, blocks) {
  const parts = String(text || '').split(PAGE_BREAK_MARKER);
  parts.forEach((part, index) => {
    if (part) blocks.push({kind: 'markdown', text: part});
    if (index < parts.length - 1) blocks.push({kind: 'pageBreak'});
  });
}

export function parseHandoutBody(body, questionMeta = {}) {
  const lines = String(body || '').replace(/\r\n?/g, '\n').match(/.*(?:\n|$)/g) || [];
  if (lines.length && !lines[lines.length - 1]) lines.pop();
  const blocks = [];
  const warnings = [];
  const plain = [];
  const seen = new Set();

  const flushPlain = () => {
    if (!plain.length) return;
    splitPlain(plain.join(''), blocks);
    plain.length = 0;
  };

  let index = 0;
  while (index < lines.length) {
    const opening = lines[index].replace(/\n$/, '').match(QUESTION_OPEN);
    if (!opening) {
      plain.push(lines[index]);
      index += 1;
      continue;
    }
    const blockId = opening[1];
    let solutionIndex = -1;
    let endIndex = -1;
    let cursor = index + 1;
    while (cursor < lines.length) {
      const line = lines[cursor].replace(/\n$/, '');
      const solution = line.match(QUESTION_SOLUTION);
      const ending = line.match(QUESTION_END);
      if (solution?.[1] === blockId && solutionIndex < 0) solutionIndex = cursor;
      else if (ending?.[1] === blockId) {
        endIndex = cursor;
        break;
      } else if (line.match(QUESTION_OPEN)) {
        break;
      }
      cursor += 1;
    }
    if (endIndex < 0) {
      warnings.push(`题目块 ${blockId} 缺少结束标记，已保留原始 Markdown`);
      plain.push(lines[index]);
      index += 1;
      continue;
    }
    flushPlain();
    const bodyEnd = solutionIndex < 0 ? endIndex : solutionIndex;
    const meta = questionMeta[blockId] || {};
    blocks.push({
      kind: 'question',
      blockId,
      body: lines.slice(index + 1, bodyEnd).join('').replace(/^\n+|\n+$/g, ''),
      solution: solutionIndex < 0 ? ''
        : lines.slice(solutionIndex + 1, endIndex).join('').replace(/^\n+|\n+$/g, ''),
      numberOverride: meta.number_override ?? null,
      solutionPlacement: meta.solution_placement || 'inherit',
    });
    if (seen.has(blockId)) warnings.push(`题目块 id ${blockId} 重复`);
    seen.add(blockId);
    index = endIndex + 1;
  }
  flushPlain();
  const orphaned = Object.keys(questionMeta).filter(blockId => !seen.has(blockId));
  if (orphaned.length) warnings.push(`有 ${orphaned.length} 个未引用的题目快照`);
  return {blocks, warnings};
}

export function questionMarker(attrs) {
  const parts = [
    `<!-- quizforge:question ${attrs.blockId} -->`,
    String(attrs.body || '').trim(),
  ];
  if (attrs.solution) {
    parts.push(`<!-- quizforge:solution ${attrs.blockId} -->`, String(attrs.solution).trim());
  }
  parts.push(`<!-- quizforge:end ${attrs.blockId} -->`);
  return parts.join('\n\n');
}

export function numberLabels(questionAttrs) {
  return questionAttrs.map((attrs, index) => {
    const value = attrs.numberOverride;
    return value === null || value === undefined || value === '' ? String(index + 1) : String(value);
  });
}

export function sourceIsLocallyEdited(attrs, snapshot) {
  if (!snapshot) return false;
  return String(attrs.body || '') !== String(snapshot.body || '')
    || String(attrs.solution || '') !== String(snapshot.solution || '');
}

export function hasUnsavedWork(documentDirty, inspectorDirty) {
  return Boolean(documentDirty || inspectorDirty);
}

export function reconcileSaveSuccess({
  saveRevision, currentRevision, currentMetadata, savedMetadata,
}) {
  const current = saveRevision === currentRevision;
  return {
    current,
    dirty: !current,
    metadata: current ? savedMetadata : currentMetadata,
    reschedule: !current,
  };
}

export function createAutosave(callback, delay = 1000, timers = globalThis) {
  let timer = null;
  let composing = false;
  let pending = false;
  const cancel = () => {
    if (timer !== null) timers.clearTimeout(timer);
    timer = null;
  };
  const schedule = () => {
    pending = true;
    cancel();
    if (composing) return;
    timer = timers.setTimeout(async () => {
      timer = null;
      pending = false;
      await callback();
    }, delay);
  };
  return {
    schedule,
    flush: async () => {
      cancel();
      if (!pending) return;
      pending = false;
      await callback();
    },
    cancel,
    beginComposition: () => {
      composing = true;
      cancel();
    },
    endComposition: () => {
      composing = false;
      if (pending) schedule();
    },
  };
}
