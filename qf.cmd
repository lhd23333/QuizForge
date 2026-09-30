@echo off
rem QuizForge Agent (qf) - a standalone pi instance with its own agent directory.
rem It does NOT share mcp.json / extensions / skills / prompts / sessions / models
rem with the user's general pi setup under ~/.pi/agent.
rem
rem NOTE: do NOT wrap this in `setlocal`. npm's pi.cmd shim ends its own SETLOCAL
rem scope, which drops variables set inside our setlocal before node starts, so
rem PI_CODING_AGENT_DIR would silently fall back to ~/.pi/agent. We set the
rem variable, call pi, then restore it ourselves.
set "QF_AGENT_DIR=%QUIZFORGE_AGENT_DIR%"
if not defined QF_AGENT_DIR set "QF_AGENT_DIR=%LOCALAPPDATA%\QuizForge\agent"
if not exist "%QF_AGENT_DIR%\settings.json" (
  echo [qf] QuizForge agent is not installed yet.
  echo [qf] Run:  pwsh -File "%~dp0integrations\qf-agent\install.ps1"
  exit /b 1
)
where pi >nul 2>nul
if errorlevel 1 (
  echo [qf] pi is not on PATH. Install it with:
  echo [qf]   npm install -g --ignore-scripts @earendil-works/pi-coding-agent
  exit /b 1
)
set "QF_PREV_AGENT_DIR=%PI_CODING_AGENT_DIR%"
set "PI_CODING_AGENT_DIR=%QF_AGENT_DIR%"
call pi %*
set "QF_EC=%ERRORLEVEL%"
if defined QF_PREV_AGENT_DIR (
  set "PI_CODING_AGENT_DIR=%QF_PREV_AGENT_DIR%"
) else (
  set "PI_CODING_AGENT_DIR="
)
set "QF_PREV_AGENT_DIR="
exit /b %QF_EC%
