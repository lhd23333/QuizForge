# QuizForge — Standalone Edition

A single-machine math question bank manager: flat-file storage, no database, no accounts, just open it in your browser.

A complete toolchain for "PDF/image exam → OCR → AI normalization → question bank management → exam export", built for a single person (e.g. a teacher) organizing a personal math question bank locally. All data lives on disk as plain Markdown files, and the bank directory is directly openable as an Obsidian vault.

> 中文说明见 [README.md](README.md)

## Features

- **Import**: paste Markdown directly, upload PDF/Word/images for AI recognition, or batch-import multiple exam files at once
- **AI recognition**: PDF/image → MinerU OCR → DeepSeek (or any OpenAI-compatible model) normalizes the OCR output into structured questions
- **Bank management**: folder-based organization, tags, difficulty, question-type filters, duplicate detection, and a recycle bin (soft delete, restorable)
- **Exam export**: filter questions by criteria and export to PDF (multi-column layout for multiple-choice options, automatic image layout)
- **Local-first storage**: one `.md` file per question (YAML frontmatter + body), folders are real directories — the bank folder can be opened directly in Obsidian as a vault
- **Zero silent mangling of user content**: LaTeX formulas and custom sections (`## heading`) are preserved verbatim, never stripped or rewritten behind your back

## Quick start

### 1. Setup

Requires Python 3.11+ (developed against 3.13).

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

PDF export additionally requires, on your machine:

- **Pandoc** (Markdown → LaTeX): https://pandoc.org/installing.html
- **XeLaTeX** (LaTeX → PDF, MiKTeX or TeX Live recommended, must support Chinese): https://miktex.org/

`config.py` auto-detects `pandoc`/`xelatex` from your system `PATH`. If detection fails, edit the `PANDOC`/`XELATEX` constants in `config.py` to point at the executables' absolute paths.

### 2. Run

```powershell
python app.py
```

Open http://127.0.0.1:5000 in your browser.

### 3. (Optional) Configure AI recognition

Every core feature works without this step — manual paste-import, bank management, and PDF export all work offline. Only "auto-recognize uploaded PDF/image" needs credentials for two paid cloud services:

1. **MinerU** (OCR): create an API token at https://mineru.net under "API Management" (paid)
2. **DeepSeek** (normalization, or any OpenAI-compatible model): create an API key at https://platform.deepseek.com (paid)

Two ways to configure:

- **Settings page** (recommended): open the "Settings" page after launch and add an LLM config (supports DeepSeek / Alibaba Bailian / SiliconFlow / relay endpoints / self-hosted — anything OpenAI-compatible). The key is stored encrypted locally.
- **Or**: copy `vendor/project_alpha/.env.example` to `vendor/project_alpha/.env` and fill in `MINERU_API_TOKEN` and `DEEPSEEK_API_KEY`.

Note: the MinerU token can currently only be set via `.env` (OCR is hardwired to MinerU, no alternative provider). The DeepSeek/LLM normalization step can be swapped for any other model via the settings page.

## Security notice

**This tool only listens on `127.0.0.1` and has no authentication whatsoever. It is designed for local, single-user use only. Do not modify it to expose the server to the public internet or a shared LAN, and do not run it on a machine shared with other users — anyone who can reach this port can read and write your question bank.**

API keys are encrypted with Fernet symmetric encryption and stored in `data/.enc_key` (the key file) + `data/providers.json` (the encrypted config). **If the key file is deleted or rotated, all previously saved keys become permanently unrecoverable** and must be re-entered on the settings page. This file is already excluded via `.gitignore` — never commit it to git or include it in any backup/sync tool.

## Directory structure

```
软件版/ (standalone edition)
  app.py                 Flask app and routes
  config.py               Paths and constants
  filestore.py             File-based bank storage layer (one .md per question)
  importer.py              Markdown question-splitting logic
  exporter.py               PDF export (pandoc + xelatex)
  converter.py             AI recognition layer (calls vendor/project_alpha)
  dedup.py                 Duplicate question detection
  crypto_utils.py           Encrypted API key storage
  llm_client.py             Generic OpenAI-compatible LLM client
  providers.py              Multi-provider LLM config management (cc-switch style)
  blockpipe.py / blocksplit.py / blocknorm.py / mechfix.py
                             Alternate "mechanical block-splitting" recognition engine (optional)
  templates/                 Jinja2 page templates
  static/                    CSS
  vendor/project_alpha/       Bundled PDF/image normalization engine (MinerU + DeepSeek)
  data/                       Bank data (gitignored, machine-local)
  output/                    Export artifacts (gitignored)
```

## About `vendor/project_alpha`

This is the bundled PDF/image normalization engine (PDF/Word/image → MinerU OCR → DeepSeek → normalized Markdown), vendored as an internal package with no external path dependency. It's a plain Python library, not a standalone service — `converter.py` calls its functions directly.

The two cloud APIs it depends on (MinerU, DeepSeek) are both paid services with no offline alternative — that's an inherent constraint of the "AI auto-recognition" feature specifically, and does not affect the rest of the app (bank management, manual import, PDF export), which all work fully offline without any credentials configured.

## License

This project is for personal learning and local use only; no open-source license is attached. Contact the author before redistributing or using it commercially.
