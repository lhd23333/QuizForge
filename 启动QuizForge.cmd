@echo off
rem ==============================================================
rem QuizForge - source-mode launcher (created 2026-10-04)
rem Runs the LIVE code in this directory - always in sync with
rem source edits (Python changes apply on restart, static/js on
rem window refresh). Close any packaged QuizForge.exe first.
rem The console window shows startup logs / errors; keep it open
rem while the app is running.
rem ==============================================================
cd /d "%~dp0"
".venv\Scripts\python.exe" desktop.py %*
