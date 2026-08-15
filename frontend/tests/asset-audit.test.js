import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {JSDOM} from 'jsdom';


function flush() {
  return new Promise(resolve => setTimeout(resolve, 0));
}


test('共享图片体检先展示扫描结果，再经确认永久删除', async () => {
  const dom = new JSDOM(`<!doctype html><html><body>
    <button id="asset-audit-start">扫描</button>
    <div id="asset-audit-status"></div>
    <div id="asset-audit-result" hidden></div>
    <button id="asset-audit-delete" hidden>删除</button>
  </body></html>`, {runScripts: 'outside-only'});
  const requests = [];
  dom.window.confirm = () => true;
  dom.window.fetch = async (url, options = {}) => {
    requests.push({url, options});
    if (url.endsWith('/start')) {
      return {ok: true, json: async () => ({ok: true, job_id: 'job1'})};
    }
    if (url.endsWith('/delete')) {
      return {ok: true, json: async () => ({
        ok: true, removed: 3, removed_bytes: 2048, changed_or_skipped: 1,
      })};
    }
    return {ok: true, json: async () => ({
      ok: true, status: 'done', asset_dir: 'D:/vault/_assets',
      bank_count: 2, markdown_files: 20, asset_files: 10,
      referenced_files: 7, orphan_count: 3, orphan_bytes: 2048,
      recent_unreferenced: 1, missing_references: 0, ignored_files: 0,
    })};
  };
  const source = fs.readFileSync(
    new URL('../../static/js/dedup-scan.js', import.meta.url), 'utf8');
  dom.window.eval(source);

  dom.window.document.getElementById('asset-audit-start').click();
  await flush();
  await flush();

  const result = dom.window.document.getElementById('asset-audit-result');
  const remove = dom.window.document.getElementById('asset-audit-delete');
  assert.equal(result.hidden, false);
  assert.match(result.textContent, /可永久删除 3 个/);
  assert.equal(remove.hidden, false);

  remove.click();
  await flush();
  await flush();

  assert.equal(remove.hidden, true);
  assert.match(dom.window.document.getElementById('asset-audit-status').textContent,
               /已永久删除 3 个文件/);
  assert.equal(requests.at(-1).url, '/api/assets/orphans/job1/delete');
  assert.deepEqual(JSON.parse(requests.at(-1).options.body), {confirm: true});
});
