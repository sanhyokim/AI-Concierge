"""Short-lived browser credentials per vendor, minted on the local machine with keys from the environment.

The browser never receives an API key. Request and response shapes follow the vendors' public docs
(checked 2026-10-05) but have not been exercised against a real account yet: every call here is
"written from the docs, connection not yet confirmed". The HTTP function is injectable for tests.
"""
from __future__ import annotations

import datetime as dt
import json
import urllib.error
import urllib.request
from typing import Callable

from ..engines.budget import MAX_OUTPUT_TOKENS
from .config import GREETING, LAB_INSTRUCTIONS, gemini_function_declarations, openai_tools

Http = Callable[[str, str, dict, dict | None], tuple[int, dict | str]]

ELEVENLABS_CLIENT_ESM = "https://cdn.jsdelivr.net/npm/@elevenlabs/client@1.26.0/+esm"


class VendorError(RuntimeError):
    pass


def http_json(method: str, url: str, headers: dict, body: dict | None = None, timeout: float = 20.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            status = resp.status
    except urllib.error.HTTPError as exc:
        raise VendorError(f"{url}: HTTP {exc.code} {exc.read().decode()[:300]}") from None
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


def _iso(delta: dt.timedelta) -> str:
    return (dt.datetime.now(dt.timezone.utc) + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def mint(cfg: dict, env: dict, http: Http = http_json) -> dict:
    """Credentials and session settings for the page. Never returns a key from env."""
    adapter = cfg["adapter"]
    if adapter == "fake":
        return {"greeting": GREETING}
    if adapter == "openai-realtime":
        body = {"session": {
            "type": "realtime", "model": cfg["model"], "instructions": LAB_INSTRUCTIONS,
            "audio": {"input": {"transcription": {"model": "gpt-4o-transcribe", "language": "ja"},
                                "turn_detection": {"type": "semantic_vad"}},
                      "output": {"voice": cfg.get("voice", "marin")}},
            "tools": openai_tools(), "tool_choice": "auto", "max_output_tokens": MAX_OUTPUT_TOKENS}}
        _, data = http("POST", "https://api.openai.com/v1/realtime/client_secrets",
                       {"Authorization": f"Bearer {env['OPENAI_API_KEY']}", "Content-Type": "application/json"}, body)
        if not isinstance(data, dict) or "value" not in data:
            raise VendorError("client_secrets: no 'value' in the response")
        return {"ephemeral_key": data["value"], "calls_url": "https://api.openai.com/v1/realtime/calls",
                "greeting": GREETING}
    if adapter == "gpt-live":
        return {"sdp_exchange": "/api/session/sdp", "greeting": GREETING}  # the offer is exchanged server-side
    if adapter == "gemini-live":
        body = {"uses": 1, "expireTime": _iso(dt.timedelta(minutes=30)),
                "newSessionExpireTime": _iso(dt.timedelta(minutes=1))}
        _, data = http("POST", "https://generativelanguage.googleapis.com/v1beta/auth_tokens",
                       {"x-goog-api-key": env["GEMINI_API_KEY"], "Content-Type": "application/json"}, body)
        token = data.get("name") if isinstance(data, dict) else None
        if not token:
            raise VendorError("auth_tokens: no 'name' in the response")
        return {"token": token,
                "ws_url": "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta."
                          "GenerativeService.BidiGenerateContentConstrained",
                "setup": {"setup": {
                    "model": cfg["model"], "responseModalities": ["AUDIO"],
                    "systemInstruction": {"parts": [{"text": LAB_INSTRUCTIONS}]},
                    "tools": [{"functionDeclarations": gemini_function_declarations()}],
                    "inputAudioTranscription": {}, "outputAudioTranscription": {}}},
                "greeting": GREETING}
    if adapter == "elevenlabs":
        url = ("https://api.elevenlabs.io/v1/convai/conversation/get-signed-url?agent_id="
               + urllib.request.quote(env["ELEVENLABS_AGENT_ID"]))
        _, data = http("GET", url, {"xi-api-key": env["ELEVENLABS_API_KEY"]}, None)
        if not isinstance(data, dict) or "signed_url" not in data:
            raise VendorError("get-signed-url: no 'signed_url' in the response")
        return {"signed_url": data["signed_url"], "client_esm": ELEVENLABS_CLIENT_ESM}
    if adapter == "cartesia":
        _, data = http("POST", "https://api.cartesia.ai/access-token",
                       {"Authorization": f"Bearer {env['CARTESIA_API_KEY']}",
                        "Cartesia-Version": cfg["cartesia_version"], "Content-Type": "application/json"},
                       {"grants": {"agent": True}, "expires_in": 600})
        token = data.get("token") if isinstance(data, dict) else None
        if not token:
            raise VendorError("access-token: no 'token' in the response")
        return {"token": token, "cartesia_version": cfg["cartesia_version"],
                "ws_url": f"wss://api.cartesia.ai/agents/stream/{urllib.request.quote(env['CARTESIA_AGENT_ID'])}",
                "start": {"event": "start", "config": {"input_format": "pcm_16000"}}}
    raise VendorError(f"unknown adapter {adapter}")


def exchange_live_sdp(cfg: dict, env: dict, offer_sdp: str, http: Http = http_json) -> dict:
    """GPT-Live: the server posts the browser's offer to /v1/live/sessions with the key and returns the answer."""
    body = {"session": {"model": cfg["model"], "instructions": LAB_INSTRUCTIONS, "delegation": {"type": "client"}},
            "transport": {"type": "webrtc", "sdp": offer_sdp}}
    _, data = http("POST", "https://api.openai.com/v1/live/sessions",
                   {"Authorization": f"Bearer {env['OPENAI_API_KEY']}", "Content-Type": "application/json"}, body)
    try:
        return {"sdp": data["transport"]["sdp"], "vendor_session_id": data["session"]["id"]}
    except (KeyError, TypeError):
        raise VendorError("live/sessions: no transport.sdp in the response") from None
