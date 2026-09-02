@echo off
rem Associe les fichiers .torrent et les liens magnet a Torrent Uploader.
rem N'ecrit que dans HKEY_CURRENT_USER : aucun droit administrateur necessaire,
rem et l'operation est entierement reversible avec l'autre fichier .bat.
setlocal
echo Enregistrement de Torrent Uploader...
echo.
python "%~dp0torrent_uploader.py" --register
echo.
python "%~dp0torrent_uploader.py" --status
echo.
pause
