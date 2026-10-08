@echo off
chcp 65001 >nul
cd /d "%~dp0"
title AI受電 管理画面のパスワードの再設定

set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if defined PY goto run
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=python"
if defined PY goto run
echo Python 3.11 以上が見つかりません。手順書 docs\start-windows.md の手順1を見てください。
pause
exit /b 1

:run
echo 管理画面のパスワードを決め直します（管理画面のサーバーを閉じてから行ってください）。
%PY% -m prototype.admin --reset-password
pause
