@echo off
REM setup.bat - Windows wrapper for ads-remover setup.
REM Usage: setup.bat [-y]
python "%~dp0remove_ads.py" --setup %*
