"""Interactive start for people who do not use a terminal (start_windows.bat / --setup).

- The admin password is chosen on the first start (typed twice, hidden). Forgotten: --reset-password.
- API keys are typed (or pasted) into this window only. Nothing is shown on screen; the keys live in this
  process's memory and are never written to a file. They are asked again at every start.
Input functions are injectable so the prompts can be tested without a console.
"""
from __future__ import annotations

import getpass
import os
import socket
import ssl
from typing import Callable

from .auth import hash_password
from ..reception.store import Store

MIN_PASSWORD = 8
KEYS = (   # only the two candidates of the first connection check (user decision 2026-10-07)
    ("OPENAI_API_KEY", "OpenAI（GPT-Live 1）のAPIキー", "sk-"),
    ("GEMINI_API_KEY", "Google AI Studio（Gemini 3.8 Live）のAPIキー", "AIza"),
)

Ask = Callable[[str], str]
Say = Callable[[str], None]


def _clean(value: str) -> str:
    return value.strip().strip('"').strip("'").strip()


def choose_password(ask: Ask = getpass.getpass, say: Say = print, tries: int = 3) -> str | None:
    """A new password typed twice. None when the person gives up (a random one is then printed instead)."""
    for _ in range(tries):
        first = ask(f"管理画面のパスワードを決めて入力し、Enter（{MIN_PASSWORD}文字以上。画面には表示されません）：")
        if len(first) < MIN_PASSWORD:
            say(f"  → {MIN_PASSWORD}文字以上にしてください。")
            continue
        if ask("確認のため、同じパスワードをもう一度入力し、Enter：") != first:
            say("  → 1回目と違います。もう一度お願いします。")
            continue
        return first
    return None


def ensure_password(store: Store, auth, ask: Ask = getpass.getpass, say: Say = print) -> None:
    if store.q1("SELECT id FROM users LIMIT 1"):
        say("ログイン：ユーザー名 admin ／ パスワードは最初の起動で決めたもの"
            "（忘れたときは reset_password_windows.bat）")
        return
    say("【最初の起動】管理画面にログインするためのパスワードを決めます。")
    chosen = choose_password(ask, say)
    generated = auth.ensure_admin(chosen)
    if generated:
        say(f"パスワードを自動で作りました：{generated}  （控えてください。表示はこの1回だけです）")
    else:
        say("パスワードを保存しました。ログインのユーザー名は admin です。")


def reset_password(store: Store, ask: Ask = getpass.getpass, say: Say = print) -> bool:
    if not store.q1("SELECT id FROM users WHERE username = 'admin'"):
        say("まだパスワードが作られていません。start_windows.bat で起動すると、最初に決められます。")
        return False
    chosen = choose_password(ask, say)
    if not chosen:
        say("変更しませんでした。")
        return False
    h, salt, it = hash_password(chosen)
    store.x("UPDATE users SET pw_hash = ?, salt = ?, iterations = ? WHERE username = 'admin'", (h, salt, it))
    store.x("DELETE FROM sessions")   # log out every browser that used the old password
    store.audit("system", "password_reset", "admin")
    say("パスワードを変更しました。ログインのユーザー名は admin です。")
    return True


def ask_keys(env: dict | None = None, ask: Ask = getpass.getpass, say: Say = print, tries: int = 3) -> dict:
    """Ask for each key not already set. Empty input skips it (that candidate cannot be chosen)."""
    env = os.environ if env is None else env
    got = {}
    say("")
    say("【APIキー】キーを貼り付けて Enter を押します。貼り付けても画面には何も表示されませんが、入っています。")
    say("  キーはこのPCのメモリーにだけ置き、ファイルには書きません（次に起動したときは、もう一度入力します）。")
    say("  まだキーがない・使わない場合は、何も入力せずに Enter を押してください（オフラインの模擬だけで動かせます）。")
    for name, label, prefix in KEYS:
        if env.get(name):
            say(f"- {label}：設定済み（このPCの環境変数）")
            got[name] = "env"
            continue
        for attempt in range(tries):
            value = _clean(ask(f"- {label}（{prefix}… で始まる）："))
            if not value:
                say("  → 入力なし。この候補は選べません。")
                break
            if not value.startswith(prefix) or len(value) < 20 or any(c.isspace() for c in value):
                say(f"  → 形が違うようです（{prefix} で始まり、空白のない長い文字列のはずです）。もう一度貼り付けてください。")
                continue
            env[name] = value
            got[name] = "typed"
            say(f"  → 受け付けました（{prefix} で始まる {len(value)} 文字）。")
            break
        else:
            say(f"  → {tries}回とも形が違ったため、この候補は選べません。キーを確かめて、起動し直してください。")
    return got


HTTPS_OK, HTTPS_CERTIFICATE, HTTPS_NETWORK = 0, 2, 3


def _handshake(host: str, timeout: float) -> None:
    with socket.create_connection((host, 443), timeout=timeout) as raw:
        with ssl.create_default_context().wrap_socket(raw, server_hostname=host):
            pass


def check_https(host: str = "api.openai.com", say: Say = print, connect=_handshake, timeout: float = 8.0) -> int:
    """TLS handshake only: no HTTP request and no key, so nothing is sent to the vendor and nothing is billed.
    0 = this PC can verify the vendor's certificate, 2 = it cannot (Mac python.org Python before
    "Install Certificates.command"), 3 = no connection (offline, blocked)."""
    try:
        connect(host, timeout)
    except ssl.SSLCertVerificationError as exc:
        say(f"HTTPS の確認：{host} の証明書を、このPCのPythonで確かめられません（{exc.verify_message or exc}）。")
        return HTTPS_CERTIFICATE
    except OSError as exc:
        say(f"HTTPS の確認：{host} につながりません（{type(exc).__name__}: {exc}）。")
        return HTTPS_NETWORK
    say(f"HTTPS の確認：{host} の証明書を確かめられました。")
    return HTTPS_OK
