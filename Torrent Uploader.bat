@echo off
rem Lanceur de Torrent Uploader.
rem Double-cliquez, ou deposez des fichiers .torrent (ou un dossier) sur ce fichier.
setlocal
set "SCRIPT=%~dp0torrent_uploader.py"

where pythonw >nul 2>&1
if %errorlevel%==0 (
    start "" pythonw "%SCRIPT%" %*
    goto :eof
)
where pyw >nul 2>&1
if %errorlevel%==0 (
    start "" pyw -3 "%SCRIPT%" %*
    goto :eof
)
echo.
echo   Python est introuvable.
echo   Installez Python 3 depuis https://www.python.org/downloads/
echo   (la version du Microsoft Store ne convient pas).
echo.
pause
