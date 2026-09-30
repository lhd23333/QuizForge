@echo off
rem Switch the bank used by the QuizForge agent (qf).
rem Usage: qf-bank [name|path] [-List] [-NoStart]
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\qf_bank.ps1" %*
exit /b %ERRORLEVEL%
