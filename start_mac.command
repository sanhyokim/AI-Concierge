#!/bin/bash
# Double-click start for macOS (same as start_windows.bat). Opens the admin app and the conversation lab.
cd "$(dirname "$0")" || exit 1
export PYTHONUTF8=1   # read and write files as UTF-8 whatever the system setting is

finish() { echo; read -r -p "Enter キーを押すと終わります（この画面は閉じてかまいません）。" _; exit "$1"; }

if [ ! -f "prototype/admin/__main__.py" ]; then
  echo
  echo "このファイルは、ZIP を展開したフォルダーの中から開いてください。"
  echo "手順書 docs/start-mac.md の「手順2」を見てください。"
  finish 1
fi

# Python 3.11 or later. /usr/bin/python3 without the developer tools only opens an install dialog, so skip it then.
PY=""
for c in python3.14 python3.13 python3.12 python3.11 python3; do
  path="$(command -v "$c" 2>/dev/null)" || continue
  if [ "$(uname)" = "Darwin" ] && [ "$path" = "/usr/bin/python3" ] && ! xcode-select -p >/dev/null 2>&1; then
    continue
  fi
  if "$path" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
    PY="$path"
    break
  fi
done
if [ -z "$PY" ]; then
  echo
  echo "Python 3.11 以上が見つかりません。"
  echo "手順書 docs/start-mac.md の「手順1 Python を入れる」を行ってから、もう一度このファイルを開いてください。"
  finish 1
fi
if [ -n "$START_CHECK_ONLY" ]; then echo "python: $PY"; exit 0; fi   # used by the automated test

"$PY" -m prototype.admin --setup
status=$?
if [ "$status" -ne 0 ]; then
  echo
  echo "------------------------------------------------------------"
  echo "止まりました。上に表示されたメッセージを確かめてください。"
  echo "英語のエラー（Traceback から始まる文字）が出ている場合は、"
  echo "Traceback から最後の行までをマウスでなぞって選び、⌘+C でコピーして、開発の担当に送ってください。"
  echo "キーやパスワードは画面に表示されないので、そのまま送ってかまいません。"
  echo "------------------------------------------------------------"
  finish "$status"
fi
echo
echo "サーバーが止まりました。この画面は閉じてかまいません。"
