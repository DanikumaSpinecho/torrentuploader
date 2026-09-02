@echo off
rem Glissez-deposez un ou plusieurs fichiers .torrent sur ce fichier (ou sur un
rem raccourci vers ce fichier place sur le Bureau) pour les envoyer a la seedbox.
setlocal
set "SCRIPT=%~dp0torrent_uploader.py"

where pythonw >nul 2>&1
if %errorlevel%==0 (
    start "" pythonw "%SCRIPT%" %*
    goto :eof
)
where python >nul 2>&1
if %errorlevel%==0 (
    start "" python "%SCRIPT%" %*
    goto :eof
)
echo Python est introuvable dans le PATH. Installez Python 3 depuis https://www.python.org/
pause
