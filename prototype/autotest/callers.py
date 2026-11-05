"""The caller patterns of the company (prototype/tenants/<company>.callers.json) and the caller AI that plays them.

The caller AI is an OpenAI text model called with the Responses API (POST /v1/responses). It is told who it is,
what it knows and how it talks, and it answers the reception AI one turn at a time, as a person on the phone would
type what they say. It writes 「（切る）」 when it would hang up.
"""
from __future__ import annotations

import json
import pathlib

from ..tenants import HERE as TENANTS_DIR
from ..tenants import current as current_tenant

RESPONSES_URL = "https://api.openai.com/v1/responses"
CALLER_MODEL = "gpt-6.1-terra"     # natural enough as a person on the phone; about 1-3 yen per conversation
HANG_UP = "（切る）"
EXPECT_KEYS = {"confirmed", "number", "request_has", "datetime", "asked", "tools", "no_tools", "emergency",
               "ai_stopped", "ended", "ai_says_any", "ai_not_repeat_question"}


class CallerError(ValueError):
    """The patterns cannot be used; the message says what to fix."""


def callers_path(tenant_id: str | None = None) -> pathlib.Path:
    return TENANTS_DIR / f"{tenant_id or current_tenant().id}.callers.json"


def load(path: pathlib.Path | None = None) -> list[dict]:
    p = pathlib.Path(path) if path else callers_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CallerError(f"客のパターンのファイルがありません：{p}") from None
    except json.JSONDecodeError as e:
        raise CallerError(f"客のパターンのファイルが読めません（{p} の {e.lineno} 行目）") from None
    out, seen = [], set()
    for c in data.get("callers", []):
        for k in ("id", "title", "persona", "facts"):
            if not c.get(k):
                raise CallerError(f"客のパターンに「{k}」がありません：{c.get('id', '?')}")
        if c["id"] in seen:
            raise CallerError(f"客のパターンの id が重なっています：{c['id']}")
        seen.add(c["id"])
        unknown = set(c.get("expect", {})) - EXPECT_KEYS
        if unknown:
            raise CallerError(f"{c['id']} の expect に知らない項目があります：{', '.join(sorted(unknown))}")
        out.append({**c, "behavior": list(c.get("behavior", [])), "expect": dict(c.get("expect", {}))})
    if not out:
        raise CallerError("客のパターンが1つもありません")
    return out


def caller_instructions(c: dict) -> str:
    """What the caller AI is told: who it is, what it knows, how it talks."""
    company = current_tenant().company_name
    facts = "\n".join(f"- {k}：{v}" for k, v in c["facts"].items())
    behavior = "\n".join(f"- {b}" for b in c["behavior"]) or "- 特になし"
    return (f"あなたは、{company}に電話をかけてきたお客様の役です。相手は{company}の電話受付のAIです。\n"
            f"【あなた】{c['persona']}\n【あなたが知っていること】\n{facts}\n【話し方・ふるまい】\n{behavior}\n"
            "【守ること】\n"
            "- 電話で話すように、話し言葉の日本語で、1回に1〜2文だけ話します。聞かれていないことは、ふるまいにない限り先に言いません。\n"
            "- 書くのは、あなたが口で言う言葉だけです。説明・かっこ書き・ト書きは書きません。\n"
            "- 知っていることにないことを聞かれたら、それらしく自然に答えるか「分からないです」と言います。\n"
            "- 電話番号は数字で書くか、「ゼロキューゼロ」のように読みで書きます。\n"
            f"- 相手が最後のあいさつ（「失礼いたします」など）を言ったら、「はい、失礼します。」と言うか、{HANG_UP}とだけ書きます。\n"
            f"- 用が済んだのに相手が話し続けるときや、話が進まないときも、{HANG_UP}とだけ書きます。")


def caller_turn(env: dict, http, c: dict, transcript: list[tuple[str, str]], model: str = CALLER_MODEL) -> tuple[str, dict]:
    """The caller's next words, and the token usage. transcript: (role, text) with role 'ai' or 'caller'."""
    items = [{"role": "user" if role == "ai" else "assistant", "content": text} for role, text in transcript]
    if not items or items[0]["role"] != "user":
        items.insert(0, {"role": "user", "content": "（電話がつながりました）"})
    body = {"model": model, "instructions": caller_instructions(c), "input": items, "max_output_tokens": 600,
            "reasoning": {"effort": "low"}, "store": False}
    _, data = http("POST", RESPONSES_URL, {"Authorization": f"Bearer {env['OPENAI_API_KEY']}",
                                           "Content-Type": "application/json"}, body)
    text = ""
    if isinstance(data, dict):
        for item in data.get("output", []) or []:
            if item.get("type") == "message":
                text += "".join(p.get("text", "") for p in item.get("content", []) if p.get("type") == "output_text")
    usage = (data.get("usage") or {}) if isinstance(data, dict) else {}
    return text.strip(), {"input_tokens": int(usage.get("input_tokens") or 0),
                          "cached_tokens": int(((usage.get("input_tokens_details") or {}).get("cached_tokens")) or 0),
                          "output_tokens": int(usage.get("output_tokens") or 0)}
