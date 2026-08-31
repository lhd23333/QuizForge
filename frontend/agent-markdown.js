import MarkdownIt from 'markdown-it';

window.markdownit = options => {
  const renderer = new MarkdownIt({
    html: false,
    linkify: true,
    breaks: true,
    ...(options || {}),
  });
  renderer.validateLink = value => {
    const link = String(value || '').trim();
    if (!link) return false;
    if (/^(?:#|\/|\.\/|\.\.\/)/.test(link)) return true;
    return /^(?:https?:|mailto:)/i.test(link);
  };
  return renderer;
};
