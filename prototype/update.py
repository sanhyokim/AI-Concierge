"""Windows: keep the program in one folder and bring it up to date by itself (2026-10-20).

The user asked for a smoother test cycle: each update meant downloading the ZIP, extracting it, moving the
results and setting the password again. This file does that work:

- ``python prototype/update.py --install`` (from 「AI受付を起動_windows.bat」 in an extracted ZIP) puts the program
  in %USERPROFILE%\\AI-Concierge, keeps the records, password and test results of the folder it was run from,
  and makes 「AI受付を起動.bat」 on the desktop.
- ``python update.py`` (from that desktop file) asks GitHub for the newest commit of the branch. When it is new,
  it downloads that commit's ZIP, puts it in place and keeps prototype\\data and prototype\\results. Without a
  network it starts the version already there.
- ``python update.py --remove-from-pc`` deletes all of it from the PC (for the move to a server).

It reads only this public repository and sends nothing else. Standard library only, and no imports from the
program itself: the desktop file runs a copy of this file from outside the program folder, so that folder can
be replaced while this runs.
"""
from __future__ import annotations

import io
import json
import os
import pathlib
import shutil
import sys
import tempfile
import urllib.request
import zipfile

REPO = "sanhyokim/AI-Concierge"
BRANCH = "claude/new-session-w4sjel"
SHA_URL = f"https://api.github.com/repos/{REPO}/commits/{BRANCH}"
ZIP_URL = "https://github.com/" + REPO + "/archive/{sha}.zip"
KEEP = ("prototype/data", "prototype/results")   # the records, the password and the test results
STAMP = ".installed-from"
LAUNCHER = "AI受付を起動.bat"
REKEY = "APIキーを入れ直す.bat"   # starts the same way, after deleting the saved API keys (prototype/admin/keystore.py)


def app_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("AI_CONCIERGE_HOME") or pathlib.Path.home() / "AI-Concierge")


def tool_dir() -> pathlib.Path:
    """Where the desktop file finds its copy of this file (outside the program folder)."""
    return pathlib.Path(os.environ.get("AI_CONCIERGE_TOOLS") or pathlib.Path.home() / ".ai-concierge")


def desktop_dir() -> pathlib.Path:
    """The desktop as Windows knows it (it may live in OneDrive)."""
    if os.environ.get("AI_CONCIERGE_DESKTOP"):
        return pathlib.Path(os.environ["AI_CONCIERGE_DESKTOP"])
    try:
        import winreg
        key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            path = pathlib.Path(os.path.expandvars(winreg.QueryValueEx(k, "Desktop")[0]))
        if path.is_dir():
            return path
    except (ImportError, OSError):
        pass
    return pathlib.Path.home() / "Desktop"


def fetch(url: str, accept: str | None = None, timeout: float = 30.0) -> bytes:
    headers = {"User-Agent": "ai-concierge-updater", **({"Accept": accept} if accept else {})}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def latest_sha(get=fetch) -> str | None:
    """The newest commit of the branch, or None when GitHub cannot be reached."""
    try:
        sha = get(SHA_URL, "application/vnd.github.sha", 15.0).decode("ascii", "replace").strip()
    except OSError as exc:
        print(f"最新版を確かめられませんでした（{type(exc).__name__}）。")
        return None
    return sha if len(sha) == 40 and all(c in "0123456789abcdef" for c in sha) else None


def installed_sha(app: pathlib.Path) -> str:
    try:
        return (app / STAMP).read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, IndexError):
        return ""


def program_root(folder: pathlib.Path) -> pathlib.Path | None:
    """The folder that holds start_windows.bat and the program (a ZIP has one more folder level)."""
    for cand in [folder, *sorted(p for p in folder.iterdir() if p.is_dir())]:
        if (cand / "start_windows.bat").is_file() and (cand / "prototype" / "admin" / "__main__.py").is_file():
            return cand
    return None


def download(sha: str, into: pathlib.Path, get=fetch) -> pathlib.Path:
    print("最新版をダウンロードしています…")
    data = get(ZIP_URL.format(sha=sha), None, 120.0)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        z.extractall(into)
    root = program_root(into)
    if root is None:
        raise RuntimeError("ダウンロードの中にプログラムが見つかりませんでした")
    return root


def put_in_place(src: pathlib.Path, app: pathlib.Path, sha: str, carry_from: pathlib.Path | None = None) -> None:
    """Copy src into app. The records come from the folder in use (app), or, the first time, from carry_from."""
    staging = app.with_name(app.name + ".new")
    shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(src, staging, ignore=shutil.ignore_patterns("__pycache__", STAMP))
    for keep in KEEP:   # copied over the download's own files there
        origin = next((base / keep for base in (app, carry_from) if base and (base / keep).is_dir()), None)
        if origin is not None:
            shutil.copytree(origin, staging / keep, dirs_exist_ok=True)
    (staging / STAMP).write_text(f"{sha}\n{BRANCH}\n", encoding="utf-8")
    old = app.with_name(app.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    if app.exists():
        try:
            app.rename(old)
        except OSError:
            shutil.rmtree(staging, ignore_errors=True)
            raise RuntimeError(f"{app} を入れ替えられませんでした。開いている黒い画面（サーバー）と、"
                               "このフォルダーを開いているエクスプローラーを閉じてから、もう一度開いてください") from None
    try:
        staging.rename(app)
    except OSError:
        if old.exists():
            old.rename(app)
        raise
    shutil.rmtree(old, ignore_errors=True)


LAUNCHER_TEXT = r"""@echo off
chcp 65001 >nul
set "PYTHONUTF8=1"
title {title}
cd /d "%USERPROFILE%"
rem Brings the program in %USERPROFILE%\AI-Concierge up to date, then starts it. Made by prototype\update.py.
{extra}set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1 && set "PY=python"
if not defined PY goto nopython
rem One line: this file may be rewritten by the update, so nothing after it is read.
%PY% "{tool}" && call "{app}\start_windows.bat" & exit /b
:nopython
echo Python 3.11 以上が見つかりません。手順書 docs\start-windows.md の「手順1」を見てください。
pause
"""


def write_launcher(app: pathlib.Path, tools: pathlib.Path, desktop: pathlib.Path) -> pathlib.Path | None:
    """This file's copy outside the program folder, and the desktop file that runs it (written only if changed)."""
    tools.mkdir(parents=True, exist_ok=True)
    me = app / "prototype" / "update.py"
    tool = tools / "update.py"
    if me.is_file() and (not tool.is_file() or tool.read_bytes() != me.read_bytes()):
        tmp = tools / "update.py.tmp"
        tmp.write_bytes(me.read_bytes())
        os.replace(tmp, tool)
    if not desktop.is_dir():
        return None
    for name, title, extra in ((LAUNCHER, "AI受電（試作）の起動", ""),
                               (REKEY, "APIキーを入れ直して起動", 'set "AI_CONCIERGE_FORGET_KEYS=1"\n')):
        text = (LAUNCHER_TEXT.replace("{title}", title).replace("{extra}", extra).replace("{tool}", str(tool))
                .replace("{app}", str(app)).replace("\n", "\r\n").encode("utf-8"))
        target = desktop / name
        if not target.is_file() or target.read_bytes() != text:
            tmp = desktop / (name + ".tmp")
            tmp.write_bytes(text)
            os.replace(tmp, target)
    return desktop / LAUNCHER


def remove_from_pc(app: pathlib.Path, tools: pathlib.Path, desktop: pathlib.Path) -> list[str]:
    """When the program moves to a server: delete what the test phase left on this PC (user decision 2026-10-20,
    「サーバーに移すときは必ずPCに残った残骸は消してほしい」). The saved API keys go first; then the program with its
    records and test results, this file's copy and the desktop files. Returns what was deleted."""
    gone = []
    keys = app / "prototype" / "data" / "api-keys.dpapi.json"
    if keys.is_file():
        keys.unlink()
        gone.append(str(keys))
    for folder in (app, app.with_name(app.name + ".new"), app.with_name(app.name + ".old"), tools):
        if folder.exists():
            shutil.rmtree(folder)
            gone.append(str(folder))
    for name in (LAUNCHER, REKEY):
        if (desktop / name).is_file():
            (desktop / name).unlink()
            gone.append(str(desktop / name))
    return gone


def run(argv: list[str], get=fetch, here: pathlib.Path | None = None) -> int:
    if "--remove-from-pc" in argv:
        gone = remove_from_pc(app_dir(), tool_dir(), desktop_dir())
        print("このPCから消しました：" if gone else "このPCに、消すものはありませんでした。")
        for path in gone:
            print(f"  {path}")
        return 0
    install = "--install" in argv
    here = here or pathlib.Path(__file__).resolve().parents[1]    # the extracted folder, for --install
    app, tools, desktop = app_dir(), tool_dir(), desktop_dir()
    sha = latest_sha(get)
    try:
        if sha and sha != installed_sha(app):
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    src = download(sha, pathlib.Path(tmp), get)
                except (OSError, zipfile.BadZipFile) as exc:
                    print(f"ダウンロードできませんでした（{type(exc).__name__}）。")
                    src = None
                if src is not None:
                    put_in_place(src, app, sha, carry_from=here if install else None)
                    print(f"最新版を入れました（{sha[:7]}）。記録・パスワード・試験の結果は引き継ぎました。")
                elif install and program_root(here) and not app.exists():
                    put_in_place(here, app, "", carry_from=here)
        elif install and not app.exists() and program_root(here):   # no network: install this folder as it is
            put_in_place(here, app, "", carry_from=here)
        elif sha:
            print(f"最新版です（{sha[:7]}）。")
    except RuntimeError as exc:
        print(str(exc))
        if not (app / "start_windows.bat").is_file():
            return 1
    if not (app / "start_windows.bat").is_file():
        print(f"{app} にプログラムがありません。ZIP を展開して「AI受付を起動_windows.bat」を開いてください。")
        return 1
    made = write_launcher(app, tools, desktop)
    if install and made:
        print(f"デスクトップに「{LAUNCHER}」を作りました。次からは、これをダブルクリックするだけです（ZIP は要りません）。")
        print(f"APIキーを変えるときは、デスクトップの「{REKEY}」から起動します。")
    print()
    return 0


def main() -> None:
    code = run(sys.argv[1:])
    if code:
        input("Enter キーを押すと終わります。")
    sys.exit(code)


if __name__ == "__main__":
    main()
