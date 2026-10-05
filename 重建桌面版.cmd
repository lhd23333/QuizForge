@echo off
rem ==============================================================
rem QuizForge - rebuild packaged desktop build from current sources
rem (for release / installing elsewhere / faster cold start).
rem Output: build\desktop\QuizForge\QuizForge.exe
rem Not needed for daily use - the source-mode launcher is always
rem up to date. Close QuizForge before rebuilding.
rem ==============================================================
cd /d "%~dp0"
pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_desktop.ps1" %*
set QF_EC=%ERRORLEVEL%
echo.
echo [rebuild] done, exit=%QF_EC%  output: build\desktop\QuizForge\QuizForge.exe
pause
