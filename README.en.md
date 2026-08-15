# QuizForge — Standalone Edition

A single-machine math question bank manager: flat-file storage, no database, no accounts, available as a Windows desktop app as well as in a browser or Obsidian.

A complete toolchain for "PDF/image exam → OCR → AI normalization → question bank management → exam export", built for a single person (e.g. a teacher) organizing a personal math question bank locally. All data lives on disk as plain Markdown files, and the bank directory is directly openable as an Obsidian vault.

> 中文说明见 [README.md](README.md)

## Desktop prototype

Install `build/installer/QuizForge-0.15.3-beta-Setup.exe` to try the latest generated Windows beta installer. The setup provides Start Menu and optional desktop shortcuts plus a standard uninstaller. Python and Obsidian are not required.

The current source and daily installed build are version `0.17.0-beta` (file version `0.17.0.0`). This update built only the desktop directory and copied it over the local installation, so no `0.17.0-beta` installer was generated. The latest installer remains `0.15.3-beta`: 61,605,232 bytes, SHA-256 `D09BC2E10CB0237E0B3A9544E0341014274F64043426B35B4A16BC0F269E5DC7`.

Updates do not require uninstalling first: run the newer setup and keep the existing installation directory. Program files live in that directory, while bank registration, conversion tasks, encrypted credentials, device identity, and the licence remain under `%LOCALAPPDATA%\QuizForge` or the user-selected bank directories. The closed beta intentionally has no online auto-updater; setup packages are rebuilt only for an explicit release.

The desktop build uses a frameless native window with custom title-bar controls, free resizing from all four edges and corners, and compact app-style navigation. Its question-bank sidebar has separate Files and Filters tabs, keeps the file tree visible at the 1024×680 minimum size, and remembers the last tab only in local storage. Question cards keep type, difficulty, star, and inline edit actions in a compact header, while source and deletion live in the more menu. Browser and Obsidian views keep their existing shell and card controls. Desktop markup is emitted in the initial HTML response, preventing a browser-style header from flashing between sections.

QuizForge starts the existing Flask backend on a random loopback port and hosts it in the system WebView2 runtime. The first launch asks for a bank directory. If that directory contains no Markdown files, three original demo questions are created in a separate sample folder; an existing bank is never modified this way. The desktop Bank menu then remembers multiple Obsidian vaults or ordinary folders, can create an empty bank under a chosen parent, and switches the local backend to the selected bank. Removing an entry only forgets its path and never deletes files. Legacy single-bank configuration is migrated automatically. The Welcome/About page checks the current bank and local environment. App state and logs are stored under `%LOCALAPPDATA%\QuizForge`, while questions remain ordinary Markdown files in each bank directory.

No licensing server, automatic updater, or remote export service is used. The standalone desktop build shows a request code derived from a random local identity protected by Windows DPAPI; each offline-signed `.qflicense` is bound to that code and defaults to seven calendar days, including the issue date. The request code contains no hardware or question-bank information. An unlicensed copy can still read and organize the bank, while preview/export requires a valid matching license imported under Settings. Existing optional MinerU, Doc2X, and cloud-LLM integrations remain available only when the user configures and invokes them. Pandoc 3.9.0.2 is bundled, including its GPL notice, copyright file, and corresponding source archive, so TeX and `tex.zip` export work without a separate install. A TeX distribution is not bundled: install MiKTeX/TeX Live for direct PDF output, or upload `tex.zip` to Overleaf.

Build from the development environment with `.\build_desktop.ps1`, then `.\build_installer.ps1`. The desktop build prefers Nuitka where a compatible compiler is available and falls back to PyInstaller for the current Python 3.13 environment without MSVC. `tools/verify_desktop_bundle.py` blocks source, private-key, license, and runtime-data leakage before packaging. The signing private key must remain outside the distribution; replacing the bundled public key invalidates every previously issued license. Before a paid release, replace the beta EULA, obtain the appropriate Inno Setup commercial-use licence, add Windows code signing, rotate the unencrypted internal beta key to a password-protected offline key, and review the compilation/obfuscation strategy.

## Features

- **Import**: paste Markdown directly, upload PDF/Word/images for AI recognition, or batch-import multiple exam files at once. An exam-compilation PDF and its optional answer-compilation PDF can be marked as a collection. Bookmarked books use the fast pre-OCR split path. Unbookmarked books are not cropped: each whole book is OCRed once. Reliable structural headings are preferred but are not required. When fewer than two reliable headings exist, repeated near-continuous number runs can define units, so `1…19 → 1…19` splits at `19 | 1`. Each run must start at 1, contain at least five distinct numbers, reach 85% number coverage, and overlap the adjacent run by at least 80%; the answer book must produce the same number of units with matching number sets. Explicit but conflicting headings, duplicate topics, incompatible counts, or ambiguous correspondence stop the collection instead of pairing answers by position. A bounded single-page MinerU recovery may restore a few A–D option texts beside right-hand figures only when the original shell, complete candidate, and layout coordinates are unique; the original stem and figures are retained, and ambiguous cases remain for review. Review cards can attach up to 20 additional local images per question, preview them immediately, reorder by dragging, and choose placement plus vertical/horizontal flow before confirmation. Choice questions default to option-only split, solution questions to subquestion-only split, fill-ins to stem split, and multi-image groups to vertical flow. Files can also be dragged and dropped or pulled from a folder with exam/answer files paired automatically by filename. The destination-folder picker loads one directory level at a time, so opening Import no longer walks the full bank
- **Concurrent batches**: queue many task groups at once, recognized in the background a few at a time (3 by default); the "转换任务" page tracks progress across batches, and each group can be reviewed as soon as it finishes
- **Manual block review**: per-question recognition can pause after splitting so you can merge/split/reorder blocks before they go to the AI, or skip the AI entirely and render mechanically (no token cost)
- **AI recognition**: PDF/Word/image → selectable MinerU or Doc2X OCR → DeepSeek (or any OpenAI-compatible model), with a no-LLM mechanical path also available
- **Recoverable MinerU result downloads**: completed result archives are streamed to a partial file and resumed with validated HTTP Range requests for up to six connections. The task workspace retains only the batch ID, source digest, parsing parameters, and a Token fingerprint—never the Token or signed result URL—so Retry can query the same server-side batch and continue downloading instead of uploading and OCRing the whole document again. Tasks created by an older build without a saved batch ID cannot be recovered this way
- **Bank management and inline editing**: folder-based organization, tags, difficulty, question-type filters, duplicate detection, and a recycle bin (soft delete, restorable). Folders can be dragged under another folder or back onto “All questions” to make them top-level; self and descendant drops are rejected. The floating plus button creates an inline draft at the end of the current folder; no Markdown is written until it is saved. New and existing cards share source, live-render, and reading modes, and saving replaces only that card. Choice images can sit in an option-only or whole-question split, between stem and options, or after the question; solution questions mirror this with subquestions, while fill-ins allow stem split or after-question placement. Arbitrary image counts can flow vertically or horizontally and be reordered by dragging. Solution images have an independent wrapped-text layout: text flows beside the image and reclaims the full width below it. Cards keep solutions collapsed by default behind a Solution toggle, and the expanded field is shown without injecting a `【解析】` prefix. Question cards and PDF/TeX export share the same layout plan
- **Multiple banks and one shared image directory**: the desktop app remembers up to 100 local bank paths and one global `assets_dir` used by every bank window. When you explicitly change that directory, old per-bank `_assets` files are copied without deleting their sources; identical files are reused and same-name/different-content conflicts abort the switch. Banks under one Obsidian vault should select that vault's common `_assets`. Open an existing vault/folder, create an empty bank, switch with an automatic local-backend restart, or forget a non-current entry without deleting any files. Disconnected drives are shown as unavailable rather than silently recreated
- **Image lifecycle and orphan audit**: soft-deleting a question keeps its images so the question can be restored. The Deduplication page can audit the shared directory against every registered bank, including recycle bins, handouts, and safety backups. Any unavailable bank or unreadable Markdown blocks deletion. After an explicit second confirmation, the server scans again and permanently deletes only old, unchanged files that remain unreferenced everywhere
- **In-app library**: lazily browse the current bank directory and open UTF-8 Markdown, PDFs, and common images in persistent tabs. PDF panels use WebView2's built-in viewer and remain alive while switching tabs or top-level sections. The workspace supports horizontal or vertical two-pane splits, resizable proportions, and moving tabs between panes; layout and tabs are restored for the current window session. Markdown can be edited and saved directly in source view, while rendered view previews the current draft with local KaTeX. Saves use file-version conflict detection, so an external Obsidian edit is never silently overwritten. The file API is restricted to visible allow-listed files under the bank root; PDFs and images remain read-only
- **Handout composer**: a dedicated workspace combines the persistent selected-question rail with a fixed paginated Markdown canvas. A dropped question opens in the right inspector for Markdown, solution placement, and exact custom numbering; “Confirm and compile” renders it through the shared Pandoc/XeLaTeX template, leaving only a blue outlined result card until it is clicked again. A4 one/two-column and 16:9 layouts support explicit page breaks. Solutions render their field content directly and can wrap text around their images, continuing at full width below. An optional WIMath mark appears top-left on A4 with safe top spacing or bottom-left on slides. Slide questions use a 70%-wide left-aligned content area, with per-image sizes preserved inside it. `_handouts/*.md` remains the only source of truth, whole handouts can be safely deleted with mtime protection, and SVG/automatic pagination stay disposable; the full PDF preview remains authoritative
- **Exam export**: filter questions and export standard papers, a compact A4 two-column practice sheet, or 16:9 classroom slides. Every mode can use a pure-white or warm-cream paper background, six-position headers and footers spanning the full page, and the optional WIMath mark. Slide questions use a 70%-wide left-aligned content area and preserve card-level image sizing. In the practice layout, short questions continue flowing through columns; the first solution question stays in the current column only when TeX measures enough remaining height for the whole question and answer area, while each later solution question starts a fresh column. Image/text splits in this narrow layout automatically become right-side wrapped figures so text can reclaim the full column below each image; other modes retain their existing split and automatic image layouts. Per-question solutions do not receive an injected `【解析】` prefix, while section headings such as “Reference solutions” remain
- **Local-first storage**: one `.md` file per question (YAML frontmatter + body), folders are real directories — the bank folder can be opened directly in Obsidian as a vault
  - Questions whose original paper number was recognised at import are named after it (`第3题.md`), so they're easy to find in a folder; the rest (hand-written, or split out during proofreading) fall back to a random id. **Filenames carry no identity** — that comes from the `id` in the frontmatter, so renaming a file breaks nothing
- **Zero silent mangling of user content**: LaTeX formulas and custom sections (`## heading`) are preserved verbatim, never stripped or rewritten behind your back

## Quick start

### 1. Setup

Requires Python 3.11+ (developed against 3.13).

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

When running from source, export additionally requires detectable executables:

- **Pandoc** (Markdown → LaTeX): https://pandoc.org/installing.html
- **XeLaTeX** (LaTeX → PDF, MiKTeX or TeX Live recommended, must support Chinese): https://miktex.org/

The current Windows installer already includes Pandoc. Only direct PDF compilation needs XeLaTeX on the target machine; TeX and ZIP export do not.

`config.py` auto-detects `pandoc`/`xelatex` from your system `PATH`. If detection fails, edit the `PANDOC`/`XELATEX` constants in `config.py` to point at the executables' absolute paths.

### 2. Run

```powershell
python app.py
```

Open http://127.0.0.1:5000 in your browser.

### 3. (Optional) Configure AI recognition

Every core feature works without this step. Auto-recognition needs at least one OCR credential; an LLM credential is only needed when AI normalization is selected:

1. **MinerU or Doc2X** (OCR): configure either provider on the Settings page; each import batch can choose its backend, with MinerU kept as the default
2. **DeepSeek or any OpenAI-compatible model** (optional normalization): not required for the mechanical no-AI path

Two ways to configure:

- **Settings page** (recommended): maintain MinerU tokens, the Doc2X key, and LLM configs independently. All credentials are encrypted locally.
- **Or**: copy `vendor/project_alpha/.env.example` to `vendor/project_alpha/.env` and fill in `MINERU_API_TOKEN` and `DEEPSEEK_API_KEY`.

OCR backend and downstream normalization are independent choices: MinerU and Doc2X can both feed whole-document AI, block AI, or mechanical splitting. The DeepSeek/LLM step can be swapped for any compatible model on the Settings page.

The normalization step can also point at a **local inference server** (Ollama, LM Studio, vLLM, …) — just enter a loopback base URL such as `http://127.0.0.1:11434/v1`. Loopback is explicitly allowed in the standalone edition. Non-loopback private addresses (e.g. `http://192.168.x.x`) and non-HTTPS public addresses are still rejected.

## Security notice

**This tool only listens on `127.0.0.1` and has no authentication whatsoever. It is designed for local, single-user use only. Do not modify it to expose the server to the public internet or a shared LAN, and do not run it on a machine shared with other users — anyone who can reach this port can read and write your question bank.**

API keys are encrypted with Fernet and stored under `data/` in `providers.json`, `mineru.json`, and `doc2x.json`, with `data/.enc_key` as the encryption key. **If that key file is deleted or rotated, all saved credentials become unrecoverable** and must be re-entered.

## Directory structure

```
软件版/ (standalone edition)
  app.py                 Flask app and routes
  desktop.py             Windows shell (pywebview + random loopback port)
  service_ports.py       Offline defaults and future service boundaries
  config.py               Paths and constants
  filestore.py             File-based bank storage layer (one .md per question)
  importer.py              Markdown question-splitting logic
  exporter.py               PDF export (pandoc + xelatex)
  converter.py             Unified OCR-backend and downstream splitting adapter
  pdf_collection.py        Pre-OCR splitting and pairing for bookmarked collections
  collection_structure.py  Post-OCR grouping and strict pairing for unbookmarked collections
  doc2x_client.py          Doc2X v2/v3 API, ZIP export, and layout repair
  doc2x_store.py           Encrypted Doc2X-key storage
  qrender.py                Page body rendering (option columns / image placement, shared with PDF)
  dedup.py                 Duplicate question detection
  crypto_utils.py           Encrypted API key storage
  llm_client.py             Generic OpenAI-compatible LLM client
  providers.py              Multi-provider LLM config management (cc-switch style)
  blockpipe.py / blocksplit.py / blocknorm.py / mechfix.py
                             Alternate "mechanical block-splitting" recognition engine (optional)
  qualcheck.py / optcheck.py  Recognition quality checks, option-completeness checks
  corpus.py                 Recognition corpus archive (OCR intermediates in data/corpus)
  templates/                 Jinja2 page templates
  static/                    CSS
  tools/eval_doc2x.py         Cached real-PDF Doc2X regression tool
  vendor/project_alpha/       Bundled MinerU and whole-document LLM implementation
  data/                       Bank data (gitignored, machine-local)
  output/                    Export artifacts (gitignored)
```

## About `vendor/project_alpha`

This is the bundled implementation used by the MinerU path (PDF/Word/image → MinerU OCR → DeepSeek → normalized Markdown). The Doc2X path calls its official API through `doc2x_client.py`, then feeds the same Markdown intermediate format into the existing splitting pipeline.

MinerU, Doc2X, and cloud LLM calls may incur usage fees. MinerU and Doc2X are alternative OCR backends and are not automatically run together. Bank management, manual import, and PDF export remain available without them.

## License

This project is for personal learning and local use only; no open-source license is attached. Contact the author before redistributing or using it commercially.
