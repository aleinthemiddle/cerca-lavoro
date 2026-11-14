@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === Cerca Lavoro - installazione ===
echo.
echo 1) Installo le librerie Python...
python -m pip install -q -r requirements.txt
if errorlevel 1 (
  echo ERRORE: Python non trovato o installazione fallita.
  pause
  exit /b 1
)

echo 2) Avvio automatico all'accensione del PC...
for /f "delims=" %%P in ('python -c "import sys,os;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))"') do set PYW=%%P
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup')+'\Cerca Lavoro.lnk'); $s.TargetPath='%PYW%'; $s.Arguments='\"%~dp0cerca_lavoro.py\"'; $s.WorkingDirectory='%~dp0'; $s.Description='Avvisi annunci di lavoro'; $s.Save()"

echo 3) Notifica di prova...
python cerca_lavoro.py --test

echo 4) Avvio in background...
start "" "%PYW%" "%~dp0cerca_lavoro.py"

echo.
echo Fatto! Cerca Lavoro ora gira in background e partira' da solo a ogni accensione.
echo Il primo controllo dura qualche minuto: poi riceverai la notifica "Cerca Lavoro e' attivo".
echo Per il telefono leggi LEGGIMI.txt (serve l'app gratuita "ntfy").
echo.
pause
