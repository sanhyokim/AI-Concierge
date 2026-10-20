@echo off
chcp 65001 >nul
set "PYTHONUTF8=1"
title AI受電（試作）を入れる
cd /d "%~dp0"
rem Windows (2026-10-20): installs the program in %USERPROFILE%\AI-Concierge and makes 「AI受付を起動.bat」 on
rem the desktop. From then on, that desktop file brings the program up to date by itself; no more ZIPs.
if not exist "prototype\update.py" goto not_extracted
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1 && set "PY=python"
if not defined PY goto nopython
%PY% prototype\update.py --install && call "%USERPROFILE%\AI-Concierge\start_windows.bat" & exit /b

:nopython
echo.
echo Python 3.11 以上が見つかりません。
echo 手順書 docs\start-windows.md の「手順1 Python を入れる」を行ってから、
echo もう一度このファイルをダブルクリックしてください。
echo.
pause
exit /b 1

:not_extracted
echo.
echo このファイルは、ZIP を「すべて展開」したフォルダーの中から開いてください。
echo 手順書 docs\start-windows.md の「手順2」を見てください。
echo.
pause
exit /b 1
