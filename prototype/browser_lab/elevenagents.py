"""ElevenAgents (ElevenLabs): the three-stage path, speech recognition → a text model → speech synthesis.

2026-11-03, user: 「「聞き取り → 考えるAI → 読み上げ」の3段の作りで一本化がもっとも安定しそうですね。あなたの提案で
進めましょう」. With the speech-to-speech models the server's fixed words and the model's own words were both
spoken (「こちらの指示とモデルのセリフが両方動いてる」, 2026-11-01). Here ElevenLabs hears the caller, a text model
chosen on the test page writes the answer, and ElevenLabs reads exactly that text in the chosen voice: what the
model writes is what the caller hears, so the read-backs and the summary the server returns are read as they are.

The local server keeps one agent for the lab on the tester's ElevenLabs account, with the reception tools as
client tools. Both are made with the API the first time (no dashboard steps); their ids are kept in
prototype/data/elevenagents.json, which the Windows updater keeps. Before each call the agent is updated with the
call's prompt, model and voice (PATCH), then the page gets a signed URL for one conversation and talks to the
agent over the raw WebSocket (static/adapters/elevenagents.js). The tool calls come back to the page and go to
the reception service, as for every other candidate.

Written from the public API reference (2026-11-03): agents create/update, tools create/update, signed URL,
conversation details. Not yet confirmed with a real account.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import urllib.request

from ..concierge.prompts import OPENING_ANNOUNCEMENT, OPENING_QUESTION, for_speech
from ..reception.tooldefs import RECEPTION_TOOLS
from ..tenants import current as current_tenant
from .config import MAX_SESSION_MIN

API = "https://api.elevenlabs.io/v1/convai"
STATE_FILE = pathlib.Path(__file__).resolve().parents[1] / "data" / "elevenagents.json"
AGENT_NAME = f"{current_tenant().company_name} 受付AI（会話試験）"   # the company settings
# 2026-11-05: the caller-role test talks to its own agent in text only (chat mode), so it never changes the
# voice agent; same prompt, tools and thinking AI
TEXT_AGENT_NAME = f"{current_tenant().company_name} 受付AI（客役の自動試験・文字だけ）"

# The text models offered on the test page (the agent's prompt.llm; ElevenLabs' list, read 2026-11-03).
# USD per 1M tokens (input, output) at the makers' list prices; ElevenLabs bills the model on top of $0.08/min.
# Left out: gpt-6.1-terra (not in ElevenLabs' list); gemini-3.8-flash (13.3 s to the first word in a third-party
# measurement, too slow for a phone call).
LLM_CHOICES = [
    {"id": "gpt-5.6-terra", "label": "GPT-5.6 Terra", "maker": "OpenAI", "usd_per_m": (2.00, 12.00),
     "note": "中くらいの速さと賢さ（OpenAI の Terra は ElevenAgents では 5.6 が最新）"},
    {"id": "gpt-6-luna", "label": "GPT-6 Luna", "maker": "OpenAI", "usd_per_m": (0.10, 0.50),
     "note": "いちばん安く軽い（GPT-Live の裏方で 11/2 まで使っていたもの）"},
    {"id": "gpt-6.1-sol", "label": "GPT-6.1 Sol", "maker": "OpenAI", "usd_per_m": (2.00, 10.00),
     "note": "上位のモデル。返事までの間は長くなるかもしれない"},
    {"id": "claude-sonnet-5-5", "label": "Claude Sonnet 5.5", "maker": "Anthropic", "usd_per_m": (2.00, 10.00),
     "note": "ElevenLabs の料金に含めて払う（Anthropic のアカウントは要らない）"},
]
LLM_DEFAULT = "gpt-5.6-terra"
LLM_IDS = {c["id"] for c in LLM_CHOICES}
# The OpenAI reasoning models think a little before each answer: the least that still follows the steps
REASONING_EFFORT = {"gpt-5.6-terra": "low", "gpt-6-luna": "low", "gpt-6.1-sol": "low"}
# Tokens per call minute behind ElevenLabs' own price calculator (2026-09-19): about 7,070 in and 307 out
# (solved from its GPT-5, Haiku 4.5 and GPT-5.5 figures, which it matches within $0.0001)
TOKENS_PER_MIN = (7070, 307)
PLATFORM_USD_PER_MIN = 0.08

TTS_MODEL = "eleven_v4_turbo"      # ElevenLabs: about 100 ms, over 90 languages, Japanese much improved (2026-09-28)
OUTPUT_FORMAT, INPUT_FORMAT = "pcm_24000", "pcm_16000"
KEYWORDS = list(current_tenant().keywords)   # the company settings (speech.keywords)
CLIENT_EVENTS = ["conversation_initiation_metadata", "ping", "audio", "interruption", "user_transcript",
                 "agent_response", "agent_response_correction", "client_tool_call", "client_error"]
TOOL_TIMEOUT_S = 20   # the page may wait for the caller's words, and C5 slows the tools down on purpose
# ElevenLabs' own tools would clash with ours (end_call) or act on the call itself: all off
BUILT_IN_OFF = {k: None for k in ("end_call", "language_detection", "transfer_to_agent", "transfer_to_number",
                                  "skip_turn", "play_keypad_touch_tone", "voicemail_detection")}


def llm_usd_per_min(llm: str) -> float:
    pin, pout = next(c["usd_per_m"] for c in LLM_CHOICES if c["id"] == llm)
    return (TOKENS_PER_MIN[0] * pin + TOKENS_PER_MIN[1] * pout) / 1e6


def greeting() -> str:
    """The opening (A-01), the agent's first message: read as it is by ElevenLabs, not written by the model."""
    return for_speech(OPENING_ANNOUNCEMENT + OPENING_QUESTION)


def _param(name: str, schema: dict) -> dict:
    """ElevenLabs asks each parameter for a description (the model fills it in). An enum is written into it."""
    out = {"type": schema.get("type", "string"),
           "description": schema.get("description") or {"field": "保存する項目", "value": "保存する値",
                                                          "question": "お客様のご質問", "level": "危険の程度",
                                                          "reason": "理由"}.get(name, name)}
    if schema.get("enum"):
        out["description"] += "（" + "・".join(schema["enum"]) + " のどれか）"
    return out


def tool_config(t: dict) -> dict:
    """A reception tool as an ElevenLabs client tool. pre_tool_speech off: the model says nothing before the call
    (2026-10-30, the speech-to-speech models said a line of their own and then the server's)."""
    props = t["parameters"].get("properties", {})
    return {"type": "client", "name": t["name"], "description": t["description"],
            "parameters": {"type": "object", "properties": {k: _param(k, v) for k, v in props.items()},
                           "required": list(t["parameters"].get("required", []))},
            "expects_response": True, "response_timeout_secs": TOOL_TIMEOUT_S, "pre_tool_speech": "off"}


def _digest(obj) -> str:
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def conversation_config(instructions: str, llm: str, voice: str | None, tool_ids: list[str],
                        minimal: bool = False, text_only: bool = False) -> dict:
    """The agent's settings for one call. minimal=True keeps only what the reception needs, for when ElevenLabs
    refuses one of the other fields. text_only: chat mode (no speech; each message is billed)."""
    prompt = {"prompt": instructions, "llm": llm, "tool_ids": tool_ids, "built_in_tools": dict(BUILT_IN_OFF)}
    agent = {"first_message": greeting(), "language": "ja", "prompt": prompt}
    conf = {"agent": agent, "conversation": {"max_duration_seconds": int(MAX_SESSION_MIN * 60 + 30),
                                             "text_only": bool(text_only)}}
    tts = {"voice_id": voice} if voice else {}
    if minimal:
        if tts:
            conf["tts"] = tts
        return conf
    if llm in REASONING_EFFORT:
        prompt["reasoning_effort"] = REASONING_EFFORT[llm]
    prompt["timezone"] = "Asia/Tokyo"
    agent["disable_first_message_interruptions"] = True   # the opening is heard to the end, as with Phonic
    conf["tts"] = {**tts, "model_id": TTS_MODEL, "agent_output_audio_format": OUTPUT_FORMAT}
    conf["asr"] = {"user_input_audio_format": INPUT_FORMAT, "keywords": KEYWORDS}
    # a number given in parts: ElevenLabs waits longer while the caller spells (spelling_patience auto)
    conf["turn"] = {"turn_eagerness": "normal", "spelling_patience": "auto"}
    conf["conversation"]["client_events"] = list(CLIENT_EVENTS)
    return conf


def _headers(env: dict) -> dict:
    return {"xi-api-key": env["ELEVENLABS_API_KEY"], "Content-Type": "application/json"}


PERMISSION_HINT = ("。ElevenLabs のAPIキーに「ElevenAgents」の「書き込み」の権限が要ります（ElevenLabs の画面の"
                   "「デベロッパー」→「APIキー」でキーを開き、権限を直す。docs/browser-lab-setup.md）")


def load_state(path: pathlib.Path) -> dict:
    try:
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path: pathlib.Path, state: dict) -> None:
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def prepare(env: dict, http, error_cls, state_path: pathlib.Path, instructions: str, voice: str | None,
            llm: str, text_only: bool = False) -> dict:
    """Make or update the lab's tools and agent for this call and mint the signed URL. error_cls is
    vendors.VendorError (passed in to keep this module free of an import cycle). text_only: the caller-role
    test's own agent, in chat mode."""
    def call(method: str, url: str, body: dict | None = None):
        try:
            return http(method, url, _headers(env), body)[1]
        except error_cls as exc:
            if exc.status in (401, 403):
                raise error_cls(f"{exc}{PERMISSION_HINT}"[:1400], exc.status, exc.unreached, exc.certificate) from None
            raise

    state = load_state(state_path)
    tools = state.get("tools") or {}
    changed = False
    for t in RECEPTION_TOOLS:
        conf = tool_config(t)
        digest, have = _digest(conf), tools.get(t["name"]) or {}
        if have.get("id") and have.get("digest") == digest:
            continue
        data = None
        if have.get("id"):
            try:
                data = call("PATCH", f"{API}/tools/{urllib.request.quote(have['id'])}", {"tool_config": conf})
                data = data if isinstance(data, dict) else {"id": have["id"]}
                data.setdefault("id", have["id"])
            except error_cls as exc:
                if exc.status != 404:   # deleted on ElevenLabs' screen: made again below
                    raise
        if data is None:
            data = call("POST", f"{API}/tools", {"tool_config": conf})
        if not isinstance(data, dict) or not data.get("id"):
            raise error_cls(f"tools: no 'id' in the response for {t['name']}")
        tools[t["name"]] = {"id": data["id"], "digest": digest}
        changed = True
    state["tools"] = tools
    if changed:
        save_state(state_path, state)
    tool_ids = [tools[t["name"]]["id"] for t in RECEPTION_TOOLS]

    def body(minimal: bool) -> dict:
        b = {"conversation_config": conversation_config(instructions, llm, voice, tool_ids, minimal, text_only)}
        if not minimal:   # a signed URL is then the only way in
            b["platform_settings"] = {"auth": {"enable_auth": True}}
        return b

    state_key = "text_agent_id" if text_only else "agent_id"
    fixed_id = None if text_only else env.get("ELEVENLABS_AGENT_ID")   # an agent named by the tester: voice only
    agent_id = fixed_id or state.get(state_key)
    fallback = None

    def write(minimal: bool) -> str:
        nonlocal agent_id
        if agent_id:
            try:
                call("PATCH", f"{API}/agents/{urllib.request.quote(agent_id)}", body(minimal))
                return agent_id
            except error_cls as exc:
                if exc.status != 404 or fixed_id:
                    raise
                agent_id = None   # the lab's agent was deleted on ElevenLabs' screen: make a new one
        data = call("POST", f"{API}/agents/create", {**body(minimal), "name": TEXT_AGENT_NAME if text_only else AGENT_NAME})
        if not isinstance(data, dict) or not data.get("agent_id"):
            raise error_cls("agents/create: no 'agent_id' in the response")
        agent_id = data["agent_id"]
        if not fixed_id:
            state[state_key] = agent_id
            save_state(state_path, state)
        return agent_id

    try:
        write(False)
    except error_cls as exc:
        if not exc.rejected or exc.status in (401, 403):
            raise
        try:
            write(True)
        except error_cls as again:
            raise error_cls(f"{again}（最小の設定でも試しました。最初の返事：{exc}）"[:1400], again.status,
                            again.unreached, again.certificate) from None
        fallback = {"dropped": "考える量・時刻の地域・冒頭の案内の割り込み禁止・読み上げのモデルと音質・"
                               "聞き取りの語・話の区切り・受け取る出来事・鍵の必須",
                    "reason": str(exc)[:600]}
    data = call("GET", f"{API}/conversation/get-signed-url?agent_id={urllib.request.quote(agent_id)}")
    if not isinstance(data, dict) or "signed_url" not in data:
        raise error_cls("get-signed-url: no 'signed_url' in the response")
    out = {"signed_url": data["signed_url"], "agent_id": agent_id, "llm": llm, "text_only": bool(text_only),
           "output_rate": int(OUTPUT_FORMAT.split("_")[1]), "input_rate": int(INPUT_FORMAT.split("_")[1])}
    if fallback:
        out["agent_fallback"] = fallback
    return out


def conversation_usage(env: dict, http, conversation_id: str) -> dict | None:
    """ElevenLabs' own record of one conversation (GET conversations/{id}): its length and what it was charged.
    None while ElevenLabs is still processing it."""
    _, data = http("GET", f"{API}/conversations/{urllib.request.quote(conversation_id)}",
                   {"xi-api-key": env["ELEVENLABS_API_KEY"]}, None)
    if not isinstance(data, dict) or data.get("status") not in ("done", "failed"):
        return None
    meta = data.get("metadata") or {}
    charging = meta.get("charging") or {}
    return {"source": "elevenlabs", "conversation_id": conversation_id, "status": data.get("status"),
            "call_seconds": meta.get("call_duration_secs"), "credits": meta.get("cost"),
            "call_credits": charging.get("call_charge"), "llm_credits": charging.get("llm_charge"),
            "llm_usd": charging.get("llm_price"), "tier": charging.get("tier")}
