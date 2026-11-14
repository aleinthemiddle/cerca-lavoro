@echo off
chcp 65001 >nul
echo Rimuovo l'avvio automatico e fermo Cerca Lavoro...
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Cerca Lavoro.lnk" 2>nul
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | Where-Object { $_.CommandLine -like '*cerca_lavoro.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo Fatto. (I file nella cartella non vengono cancellati.)
pause
