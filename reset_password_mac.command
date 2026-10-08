#!/bin/bash
# Set a new admin password (macOS). Close the admin app's window first.
cd "$(dirname "$0")" || exit 1
export PYTHONUTF8=1
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
  echo "Python 3.11 以上が見つかりません。手順書 docs/start-mac.md の手順1を見てください。"
else
  echo "管理画面のパスワードを決め直します。"
  "$PY" -m prototype.admin --reset-password
fi
echo
read -r -p "Enter キーを押すと終わります（この画面は閉じてかまいません）。" _
