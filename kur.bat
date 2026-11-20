@echo off
rem Oyun Izleyici - Windows kurulumu: bu dosyaya cift tikla (ya da: kur.bat "PC Adi")
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0kur.ps1" %*
echo.
pause
