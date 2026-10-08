@echo off
chcp 65001 >nul
cd /d "%~dp0"
title AI受電 管理画面（試作）

if not exist "prototype\admin\__main__.py" goto not_extracted

set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if defined PY goto run
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=python"
if defined PY goto run

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

:run
%PY% -m prototype.admin --setup
echo.
echo サーバーが止まりました。この画面は閉じてかまいません。
pause
