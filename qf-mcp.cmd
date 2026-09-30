@echo off
rem QuizForge MCP server over stdio (for pi and other MCP clients).
setlocal
"%~dp0.venv\Scripts\python.exe" "%~dp0tools\qf_mcp_server.py" %*
exit /b %ERRORLEVEL%
