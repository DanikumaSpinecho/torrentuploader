@echo off
rem Retire l'association des .torrent et des liens magnet.
setlocal
echo Retrait de l'enregistrement...
echo.
python "%~dp0torrent_uploader.py" --unregister
echo.
pause
