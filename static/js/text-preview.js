// 编辑/导入/拆题审核的轻量实时预览：安全转义正文，同时渲染图片与表格。
// 已入库题卡走后端 qrender（结构更完整）；这里不重做选项/图片布局，只保证用户
// 修改源码时表格不会退回成标签文本。所有单元格只取 textContent，再重新生成标签，
// 绝不把 OCR 返回的原始 HTML 直接插进页面。
(function () {
  'use strict';

  const HTML_TABLE_RE = /<table\b[^>]*>[\s\S]*?<\/table\s*>/gi;
  const PIPE_SEP_RE = /^\s*\|?(?:\s*:?-{2,}:?\s*\|)+\s*:?-{2,}:?\s*\|?\s*$/;
  const TOKEN_RE = /QJSPREVIEWTABLE(\d+)END/g;

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, ch => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[ch]));
  }

  function decodeEntities(value) {
    const area = document.createElement('textarea');
    area.innerHTML = value;
    return area.value;
  }

  function tableHtml(rows) {
    if (!rows.length || !rows[0].length) return '';
    const ncol = Math.max(...rows.map(row =>
      row.reduce((sum, cell) => sum + cell.span, 0)));

    function rowHtml(row, tag) {
      let used = 0;
      const cells = row.map(cell => {
        const span = Math.min(Math.max(1, cell.span), ncol);
        used += span;
        const attr = span > 1 ? ` colspan="${span}"` : '';
        return `<${tag}${attr}>${escapeHtml(cell.text)}</${tag}>`;
      });
      while (used++ < ncol) cells.push(`<${tag}></${tag}>`);
      return `<tr>${cells.join('')}</tr>`;
    }

    const head = rowHtml(rows[0], 'th');
    const body = rows.slice(1).map(row => rowHtml(row, 'td')).join('');
    return '<div class="q-table-wrap" role="region" aria-label="题目表格" tabindex="0">'
      + `<table class="q-table"><thead>${head}</thead>`
      + (body ? `<tbody>${body}</tbody>` : '') + '</table></div>';
  }

  function htmlTable(raw) {
    const template = document.createElement('template');
    template.innerHTML = raw;
    const table = template.content.querySelector('table');
    if (!table) return '';
    const rows = Array.from(table.rows).map(row => Array.from(row.cells).map(cell => ({
      // 只取纯文本：script/style/事件属性均不会进入返回 HTML。
      text: cell.textContent.replace(/\s+/g, ' ').trim(),
      span: Math.max(1, Number.parseInt(cell.getAttribute('colspan') || '1', 10) || 1),
    }))).filter(row => row.length);
    return tableHtml(rows);
  }

  function pipeCells(line) {
    let text = line.trim();
    if (text.startsWith('|')) text = text.slice(1);
    if (text.endsWith('|')) text = text.slice(0, -1);
    return text.split('|').map(cell => ({
      text: decodeEntities(cell).replace(/\s+/g, ' ').trim(), span: 1,
    }));
  }

  function stashTables(source) {
    const tables = [];
    let text = source.replace(HTML_TABLE_RE, raw => {
      const html = htmlTable(raw);
      if (!html) return raw;
      const index = tables.push(html) - 1;
      return `\n\nQJSPREVIEWTABLE${index}END\n\n`;
    });

    const lines = text.split('\n');
    const out = [];
    for (let i = 0; i < lines.length;) {
      const head = lines[i].trim();
      if (head.startsWith('|') && i + 1 < lines.length && PIPE_SEP_RE.test(lines[i + 1])) {
        const rows = [pipeCells(lines[i])];
        let j = i + 2;
        while (j < lines.length && lines[j].trim().startsWith('|')) {
          rows.push(pipeCells(lines[j++]));
        }
        const html = tableHtml(rows);
        if (html) {
          const index = tables.push(html) - 1;
          out.push(`QJSPREVIEWTABLE${index}END`);
          i = j;
          continue;
        }
      }
      out.push(lines[i++]);
    }
    return {text: out.join('\n'), tables};
  }

  function render(source, options) {
    const opts = options || {};
    const stashed = stashTables(String(source || ''));
    let html = escapeHtml(stashed.text);
    if (opts.imageMode === 'mineru') {
      const base = opts.imageBase || '';
      html = html.replace(/!\[([^\]]*)\]\(\s*(?:images\/)?([^)\s]+)\s*\)/g,
        (match, alt, file) => `<img src="${escapeHtml(base + encodeURIComponent(file))}" `
          + `alt="${escapeHtml(alt)}" style="max-width:100%;height:auto;display:block;margin:6px 0">`);
    } else {
      html = html.replace(/!\[\[([^\]\|]+)(?:\|[^\]]*)?\]\]/g,
        (match, file) => `<img src="/assets/${encodeURIComponent(file)}" alt="" `
          + 'style="max-width:100%;height:auto;display:block;margin:6px 0">');
    }
    html = html.replace(/\n/g, '<br>');
    return html.replace(TOKEN_RE, (match, index) => stashed.tables[Number(index)] || '');
  }

  function normalizeLibraryPath(raw, basePath) {
    const value = String(raw || '').trim().replace(/^<|>$/g, '').replace(/\\/g, '/');
    if (!value || /^[a-z][a-z0-9+.-]*:/i.test(value) || value.startsWith('//')) return '';
    const parts = value.startsWith('/') ? [] : String(basePath || '').split('/').filter(Boolean);
    for (const part of value.split('/')) {
      if (!part || part === '.') continue;
      if (part === '..') {
        if (!parts.length) return '';
        parts.pop();
      } else if (part.startsWith('.')) {
        return '';
      } else {
        parts.push(part);
      }
    }
    return parts.join('/');
  }

  function libraryKind(path) {
    const ext = (String(path).match(/\.[^.\/]+$/) || [''])[0].toLowerCase();
    if (['.md', '.markdown'].includes(ext)) return 'markdown';
    if (ext === '.pdf') return 'pdf';
    if (['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg'].includes(ext)) return 'image';
    return '';
  }

  function mediaHtml(rawPath, alt, opts, obsidianEmbed) {
    let target = String(rawPath || '').split('|')[0].trim();
    if (obsidianEmbed && !target.includes('/')) target = `_assets/${target}`;
    const path = normalizeLibraryPath(target, obsidianEmbed ? '' : opts.basePath);
    if (!path || libraryKind(path) !== 'image') return escapeHtml(`![${alt || ''}](${rawPath})`);
    return `<img class="library-md-image" src="/library/raw?path=${encodeURIComponent(path)}" `
      + `alt="${escapeHtml(alt || '')}" loading="lazy">`;
  }

  function linkHtml(rawTarget, label, opts) {
    const target = String(rawTarget || '').trim().replace(/^<|>$/g, '');
    if (/^https?:\/\//i.test(target)) {
      return `<a href="${escapeHtml(target)}" target="_blank" rel="noopener noreferrer">`
        + `${escapeHtml(label || target)}</a>`;
    }
    if (target.startsWith('#')) {
      return `<span class="library-anchor-link">${escapeHtml(label || target)}</span>`;
    }
    const path = normalizeLibraryPath(target.split('#')[0], opts.basePath);
    if (!path || !libraryKind(path)) return escapeHtml(label || target);
    return `<button type="button" class="library-doc-link" data-library-path="${escapeHtml(path)}">`
      + `${escapeHtml(label || path)}</button>`;
  }

  function inlineRich(raw, options) {
    const opts = options || {};
    const tokens = [];
    const stash = html => {
      const index = tokens.push(html) - 1;
      return `QJSPREVIEWINLINE${index}END`;
    };
    let text = String(raw || '');
    text = text.replace(/`([^`\n]+)`/g, (match, code) =>
      stash(`<code>${escapeHtml(code)}</code>`));
    text = text.replace(/!\[\[([^\]]+)\]\]/g, (match, target) =>
      stash(mediaHtml(target, '', opts, true)));
    text = text.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, (match, alt, target) =>
      stash(mediaHtml(target, alt, opts, false)));
    text = text.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (match, label, target) =>
      stash(linkHtml(target, label, opts)));
    text = text.replace(/\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g, (match, target, label) => {
      let path = target.trim();
      if (!/\.[^.\/]+$/.test(path)) path += '.md';
      return stash(linkHtml(path, label || target, opts));
    });
    text = escapeHtml(text)
      .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
      .replace(/~~([^~\n]+)~~/g, '<del>$1</del>')
      .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
    return text.replace(/QJSPREVIEWINLINE(\d+)END/g,
      (match, index) => tokens[Number(index)] || match);
  }

  function stripFrontmatter(source) {
    if (!source.startsWith('---\n')) return source;
    const end = source.indexOf('\n---\n', 4);
    return end === -1 ? source : source.slice(end + 5);
  }

  function renderRich(source, options) {
    const opts = options || {};
    let text = stripFrontmatter(String(source || '').replace(/\r\n?/g, '\n'));
    const codeBlocks = [];
    text = text.replace(/^```([^\n]*)\n([\s\S]*?)^```\s*$/gm, (match, language, code) => {
      const html = `<pre class="library-code"><code data-language="${escapeHtml(language.trim())}">`
        + `${escapeHtml(code.replace(/\n$/, ''))}</code></pre>`;
      const index = codeBlocks.push(html) - 1;
      return `\n\nQJSPREVIEWCODE${index}END\n\n`;
    });

    const stashed = stashTables(text);
    const lines = stashed.text.split('\n');
    const blocks = [];
    for (let i = 0; i < lines.length;) {
      const line = lines[i];
      const trimmed = line.trim();
      if (!trimmed) { i += 1; continue; }
      let match = trimmed.match(/^QJSPREVIEWCODE(\d+)END$/);
      if (match) { blocks.push(codeBlocks[Number(match[1])] || ''); i += 1; continue; }
      match = trimmed.match(/^QJSPREVIEWTABLE(\d+)END$/);
      if (match) { blocks.push(stashed.tables[Number(match[1])] || ''); i += 1; continue; }
      match = line.match(/^\s{0,3}(#{1,6})\s+(.+)$/);
      if (match) {
        const level = match[1].length;
        blocks.push(`<h${level}>${inlineRich(match[2].replace(/\s+#+\s*$/, ''), opts)}</h${level}>`);
        i += 1;
        continue;
      }
      if (/^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
        blocks.push('<hr>'); i += 1; continue;
      }
      if (/^\s*>/.test(line)) {
        const quote = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) {
          quote.push(lines[i].replace(/^\s*>\s?/, ''));
          i += 1;
        }
        blocks.push(`<blockquote>${quote.map(item => inlineRich(item, opts)).join('<br>')}</blockquote>`);
        continue;
      }
      match = line.match(/^\s*([-+*]|\d+[.)])\s+(.+)$/);
      if (match) {
        const ordered = /^\d/.test(match[1]);
        const tag = ordered ? 'ol' : 'ul';
        const items = [];
        while (i < lines.length) {
          const item = lines[i].match(/^\s*([-+*]|\d+[.)])\s+(.+)$/);
          if (!item || /^\d/.test(item[1]) !== ordered) break;
          let body = item[2];
          const task = body.match(/^\[([ xX])\]\s+(.*)$/);
          if (task) {
            body = `<input type="checkbox" disabled${task[1].toLowerCase() === 'x' ? ' checked' : ''}> `
              + inlineRich(task[2], opts);
          } else {
            body = inlineRich(body, opts);
          }
          items.push(`<li>${body}</li>`);
          i += 1;
        }
        blocks.push(`<${tag}>${items.join('')}</${tag}>`);
        continue;
      }
      const paragraph = [line];
      i += 1;
      while (i < lines.length && lines[i].trim()
             && !/^\s{0,3}(?:#{1,6})\s+/.test(lines[i])
             && !/^\s*(?:>|[-+*]\s+|\d+[.)]\s+)/.test(lines[i])
             && !/^QJSPREVIEW(?:CODE|TABLE)\d+END$/.test(lines[i].trim())) {
        paragraph.push(lines[i++]);
      }
      blocks.push(`<p>${paragraph.map(item => inlineRich(item, opts)).join('<br>')}</p>`);
    }
    return blocks.join('\n');
  }

  window.QTextPreview = {render, renderRich};
})();
