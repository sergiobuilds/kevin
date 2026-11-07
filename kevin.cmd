@echo off
cd /d "%~dp0"
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe src\companion_cli.py %*
) else (
  python src\companion_cli.py %*
)
