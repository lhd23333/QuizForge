@echo off
rem QuizForge own terminal UI (zero external dependency, works offline).
rem Usage: qf-tui [--resume] [--permissions full|read-only|standard] [--plain]
setlocal
"%~dp0.venv\Scripts\python.exe" "%~dp0cli.py" %*
exit /b %ERRORLEVEL%
