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
if [ "$START_CHECK_ONLY" = "1" ]; then echo "python: $PY"; exit 0; fi   # used by the automated test

# HTTPS certificates. python.org's Python has none until "Install Certificates.command" is run, and then every
# connection to OpenAI fails with CERTIFICATE_VERIFY_FAILED. The check is a handshake only: nothing is sent.
"$PY" -m prototype.admin --check-https
https=$?
if [ "$https" -eq 2 ] && command -v security >/dev/null 2>&1; then
  # use the Mac's own root certificates (the same ones Safari trusts) for this start
  roots="${TMPDIR:-/tmp}/ai-concierge-root-certificates.pem"
  security find-certificate -a -p /System/Library/Keychains/SystemRootCertificates.keychain > "$roots" 2>/dev/null
  security find-certificate -a -p /Library/Keychains/System.keychain >> "$roots" 2>/dev/null
  if [ -s "$roots" ] && SSL_CERT_FILE="$roots" "$PY" -m prototype.admin --check-https; then
    export SSL_CERT_FILE="$roots"
    echo "  → Mac 本体の証明書を使います（この起動のあいだだけ）。"
    https=0
  fi
fi
if [ "$https" -eq 2 ]; then
  echo
  echo "------------------------------------------------------------"
  echo "このMacの Python は、まだ HTTPS の証明書を持っていません。このままでは AI に接続できません"
  echo "（オフラインの模擬だけは使えます）。次のファイルをダブルクリックして、終わったらこの画面を閉じ、"
  echo "start_mac.command を開き直してください。"
  found=""
  for f in /Applications/Python\ 3.*/"Install Certificates.command"; do
    [ -e "$f" ] && { echo "  $f"; found=1; }
  done
  [ -n "$found" ] || echo "  Finder →「アプリケーション」→「Python 3.x」フォルダー →「Install Certificates.command」"
  echo "手順書 docs/start-mac.md の「手順1」の4です。"
  echo "------------------------------------------------------------"
  echo
  read -r -p "このまま起動するときは Enter を押します（模擬だけ使えます）。" _
elif [ "$https" -ne 0 ]; then
  echo "  → インターネットにつながっていないようです。AI には接続できませんが、オフラインの模擬は使えます。"
fi
if [ "$START_CHECK_ONLY" = "https" ]; then echo "https: $https ${SSL_CERT_FILE:+cert-file}"; exit 0; fi

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
