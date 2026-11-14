@echo off
cd /d "%~dp0"
if exist annunci.html (start "" annunci.html) else (python cerca_lavoro.py --una-volta --apri)
