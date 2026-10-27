#!/bin/bash
# Mac: start the AI reception prototype from one fixed folder, ~/AI-Concierge.
#
# macOS asks to approve every file downloaded with a browser, so each new ZIP used to bring the
# 「"start_mac.command"は開いていません」 dialog back. This file is approved once. Each time it is opened it looks
# in Downloads for a newer ZIP (or the folder Safari unpacked), copies the program into ~/AI-Concierge and starts it.
# The records and the admin password (prototype/data) and the test results (prototype/results) are kept.
# Nothing is sent anywhere by this file.

APP="$HOME/AI-Concierge"
DL="${AI_CONCIERGE_DOWNLOADS:-$HOME/Downloads}"
NAME="AI-Concierge-claude-new-session-w4sjel"
STAMP="$APP/.installed-from"

finish() { echo; read -r -p "Enter キーを押すと終わります（この画面は閉じてかまいません）。" _; exit "$1"; }
mtime() { if [ "$(uname)" = "Darwin" ]; then stat -f %m "$1"; else stat -c %Y "$1"; fi; }

# the newest download: a ZIP, or a folder (Safari unpacks ZIPs itself) with the program inside
newest="" newest_t=0
for f in "$DL/$NAME"*; do
  [ -e "$f" ] || continue
  if [ -d "$f" ]; then
    [ -f "$f/start_mac.command" ] || [ -n "$(find "$f" -maxdepth 2 -name start_mac.command -print -quit 2>/dev/null)" ] || continue
  else
    case "$f" in *.zip) ;; *) continue ;; esac
  fi
  t="$(mtime "$f")"
  if [ "$t" -gt "$newest_t" ]; then newest="$f" newest_t="$t"; fi
done

installed_t=0
[ -f "$STAMP" ] && installed_t="$(head -n 1 "$STAMP" 2>/dev/null)"
case "$installed_t" in ''|*[!0-9]*) installed_t=0 ;; esac

if [ -n "$newest" ] && [ "$newest_t" -gt "$installed_t" ]; then
  echo "新しいダウンロードを入れます：$(basename "$newest")"
  work="$(mktemp -d "${TMPDIR:-/tmp}/ai-concierge.XXXXXX")" || finish 1
  if [ -d "$newest" ]; then
    cp -R "$newest/." "$work/" || finish 1
  else
    # 2026-10-27 (user's Mac): the built-in unzip broke on the Japanese file name AI受付を起動_windows.bat
    # (「write error (disk full?)」, the name shown as ????). ditto is the Mac's own way and keeps the names; a file
    # unzip cannot write does not stop the install when the program itself is there.
    if command -v ditto >/dev/null 2>&1 && ditto -x -k "$newest" "$work" 2>/dev/null; then
      :
    elif unzip -qo "$newest" -d "$work" </dev/null; then
      :
    elif [ -n "$(find "$work" -maxdepth 4 -path '*/prototype/admin/__main__.py' -print -quit 2>/dev/null)" ]; then
      echo "（ZIP の中の一部のファイルを開けませんでしたが、Mac で使うものは入っています。続けます）"
    else
      echo "ZIP を開けませんでした。ダウンロードし直してください。"
      echo "直らないときは、ZIP をダブルクリックして展開してから、もう一度これを開いてください。"
      rm -rf "$work"
      finish 1
    fi
  fi
  src="$(dirname "$(find "$work" -maxdepth 3 -name start_mac.command -print -quit)")"
  if [ ! -f "$src/prototype/admin/__main__.py" ]; then
    echo "ダウンロードの中にプログラムが見つかりませんでした。手順書 docs/start-mac.md の手順2のとおり、ダウンロードし直してください。"
    rm -rf "$work"
    finish 1
  fi
  # keep the records, the password and the test results of the folder in use
  for keep in prototype/data prototype/results; do
    if [ -d "$APP/$keep" ]; then
      rm -rf "${src:?}/$keep"
      mkdir -p "$(dirname "$src/$keep")"
      cp -R "$APP/$keep" "$src/$keep" || { echo "記録の引き継ぎに失敗しました。何も変えずに止めます。"; rm -rf "$work"; finish 1; }
    fi
  done
  echo "$newest_t" > "$src/.installed-from"
  echo "$(basename "$newest")" >> "$src/.installed-from"
  if [ -d "$APP" ]; then mv "$APP" "$APP.old.$$" || { echo "$APP を入れ替えられませんでした。開いているサーバーの画面を閉じてから、もう一度開いてください。"; rm -rf "$work"; finish 1; }; fi
  mv "$src" "$APP" || { [ -d "$APP.old.$$" ] && mv "$APP.old.$$" "$APP"; echo "入れ替えに失敗しました。"; rm -rf "$work"; finish 1; }
  rm -rf "$APP.old.$$" "$work"
  # copied by this approved file, so macOS does not ask again; the browser's download mark is removed as well
  command -v xattr >/dev/null 2>&1 && xattr -dr com.apple.quarantine "$APP" 2>/dev/null
  chmod +x "$APP"/*.command 2>/dev/null
  echo "入れました：$APP（記録・パスワード・試験の結果は引き継ぎました）"
  # this file's own copy in the home folder, for the Dock: made here, so macOS never asks about it
  if [ -f "$APP/AI受付を起動_mac.command" ] && ! cmp -s "$APP/AI受付を起動_mac.command" "$HOME/AI受付を起動_mac.command"; then
    cp "$APP/AI受付を起動_mac.command" "$HOME/.ai-concierge-launcher.tmp" && chmod +x "$HOME/.ai-concierge-launcher.tmp" &&
      mv -f "$HOME/.ai-concierge-launcher.tmp" "$HOME/AI受付を起動_mac.command" &&   # a new file: safe while this one runs
      echo "次からは、ホームフォルダーの「AI受付を起動_mac.command」から起動できます（Dock に入れておくと便利です）。"
  fi
  echo
elif [ ! -f "$APP/start_mac.command" ]; then
  echo "「ダウンロード」フォルダーに $NAME の ZIP が見つかりません。"
  echo "手順書 docs/start-mac.md の手順2のとおりダウンロードしてから、もう一度このファイルを開いてください（ZIP は展開しなくてかまいません）。"
  finish 1
else
  echo "新しいダウンロードはありません。いまの版で起動します。"
  echo
fi

if [ "$LAUNCHER_INSTALL_ONLY" = "1" ]; then exit 0; fi   # used by the automated test
exec /bin/bash "$APP/start_mac.command"
