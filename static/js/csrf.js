// 本地写请求保护：普通表单补隐藏字段，fetch 写请求补同源请求头。
(() => {
  'use strict';
  const unsafe = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);
  const token = document.querySelector('meta[name="csrf-token"]')?.content || '';
  const sameOrigin = url => {
    try { return new URL(url, location.href).origin === location.origin; }
    catch { return false; }
  };
  function protectForm(form) {
    const method = (form.method || 'GET').toUpperCase();
    if (!unsafe.has(method) || !sameOrigin(form.action || location.href)) return;
    let input = form.querySelector('input[name="_csrf_token"]');
    if (!input) {
      input = document.createElement('input');
      input.type = 'hidden';
      input.name = '_csrf_token';
      form.appendChild(input);
    }
    input.value = token;
  }
  document.querySelectorAll('form').forEach(protectForm);
  document.addEventListener('submit', event => protectForm(event.target), true);
  const nativeSubmit = HTMLFormElement.prototype.submit;
  HTMLFormElement.prototype.submit = function submitWithToken() {
    protectForm(this);
    return nativeSubmit.call(this);
  };
  const nativeFetch = window.fetch.bind(window);
  window.fetch = function fetchWithToken(input, init = {}) {
    const method = (init.method || (input instanceof Request ? input.method : 'GET')).toUpperCase();
    const url = input instanceof Request ? input.url : input;
    if (!unsafe.has(method) || !sameOrigin(url)) return nativeFetch(input, init);
    const headers = new Headers(init.headers || (input instanceof Request ? input.headers : undefined));
    if (!headers.has('X-CSRF-Token')) headers.set('X-CSRF-Token', token);
    return nativeFetch(input, {...init, headers});
  };
})();
