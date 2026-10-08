"""Short-lived browser credentials per vendor, minted on the local machine with keys from the environment.

The browser never receives an API key. Request and response shapes follow the vendors' public docs
(checked 2026-10-05; GPT-Live delegation and the Cartesia agent WebSocket re-checked 2026-10-07) but have not
been exercised against a real account yet: every call here is "written from the docs, connection not yet
confirmed". The HTTP function is injectable for tests. Long-lived keys only appear in request headers built
here; error messages carry the URL and the vendor's error text, never the headers.
"""
from __future__ import annotations

import datetime as dt
import json
import socket
import ssl
import urllib.error
import urllib.request
from typing import Callable

from ..engines.budget import MAX_OUTPUT_TOKENS
from .config import (GREETING, LAB_INSTRUCTIONS, MAX_SESSION_MIN, delegation_model, gemini_function_declarations,
                     openai_tools)

Http = Callable[[str, str, dict, dict | None], tuple[int, dict | str]]

ELEVENLABS_CLIENT_ESM = "https://cdn.jsdelivr.net/npm/@elevenlabs/client@1.26.0/+esm"


class VendorError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, unreached: bool = False,
                 certificate: bool = False) -> None:
        super().__init__(message)
        self.status = status   # HTTP status when the vendor answered; None for network failures and bad replies
        self.unreached = unreached        # failed before the request left this PC: nothing reached the vendor
        self.certificate = certificate    # this PC could not verify the vendor's HTTPS certificate

    @property
    def rejected(self) -> bool:
        """The vendor answered 4xx: the request was refused and no session was created."""
        return self.status is not None and 400 <= self.status < 500


def network_error(url: str, exc: BaseException) -> VendorError:
    """A failure without an HTTP answer. The reason is kept (it never contains headers). Certificate checks, name
    lookups and refused connections fail before the request is sent; anything else (time-outs, resets) may have
    reached the vendor."""
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    certificate = isinstance(reason, ssl.SSLCertVerificationError)
    unreached = certificate or isinstance(reason, (socket.gaierror, ConnectionRefusedError))
    detail = f"{type(exc).__name__}: {reason}" if str(reason) else type(exc).__name__
    return VendorError(f"{url}: {detail}"[:900], unreached=unreached, certificate=certificate)


def http_json(method: str, url: str, headers: dict, body: dict | None = None, timeout: float = 20.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            status = resp.status
    except urllib.error.HTTPError as exc:
        raise VendorError(f"{url}: HTTP {exc.code} {exc.read().decode('utf-8', 'replace')[:600]}", exc.code) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:   # network failure: no headers in the message
        raise network_error(url, exc) from None
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


def _iso(delta: dt.timedelta) -> str:
    return (dt.datetime.now(dt.timezone.utc) + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def _strict(schema: dict) -> dict:
    """Responses function tools are strict by default: every property required, no extra properties."""
    return {**schema, "required": list(schema.get("properties", {})), "additionalProperties": False}


def _function_tools() -> list:
    """Function tools in the Responses format (flat), with strict schemas (GPT-Live delegation)."""
    return [{"type": "function", "name": t["name"], "description": t["description"],
             "parameters": _strict(t["parameters"]), "strict": True} for t in openai_tools()]


def mint(cfg: dict, env: dict, http: Http = http_json, instructions: str = LAB_INSTRUCTIONS,
         voice: str | None = None, overrides: dict | None = None, script: list | None = None) -> dict:
    """Credentials and session settings for the page. Never returns a key from env.

    instructions and voice come from the call's settings snapshot when the reception service is attached.
    Short-lived credentials are kept as short as each vendor allows, for starting this one session only."""
    adapter = cfg["adapter"]
    if adapter == "fake":
        return {"greeting": GREETING, "script": list(script or [])}
    if adapter == "openai-realtime":
        body = {"expires_after": {"anchor": "created_at", "seconds": 60},   # one key could open several sessions
                "session": {
            "type": "realtime", "model": cfg["model"], "instructions": instructions,
            "audio": {"input": {"transcription": {"model": "gpt-4o-transcribe", "language": "ja"},
                                "turn_detection": {"type": "semantic_vad"}},
                      "output": {"voice": voice or cfg.get("voice", "marin")}},
            "tools": openai_tools(), "tool_choice": "auto", "max_output_tokens": MAX_OUTPUT_TOKENS}}
        _, data = http("POST", "https://api.openai.com/v1/realtime/client_secrets",
                       {"Authorization": f"Bearer {env['OPENAI_API_KEY']}", "Content-Type": "application/json"}, body)
        if not isinstance(data, dict) or "value" not in data:
            raise VendorError("client_secrets: no 'value' in the response")
        return {"ephemeral_key": data["value"], "calls_url": "https://api.openai.com/v1/realtime/calls",
                "greeting": GREETING}
    if adapter == "gpt-live":
        return {"sdp_exchange": "api/session/sdp", "greeting": GREETING}  # the offer is exchanged server-side
    if adapter == "gemini-live":
        # one use, started within a minute, and no messages after the maximum session length (+1 minute)
        body = {"uses": 1, "expireTime": _iso(dt.timedelta(minutes=MAX_SESSION_MIN + 1)),
                "newSessionExpireTime": _iso(dt.timedelta(minutes=1))}
        _, data = http("POST", "https://generativelanguage.googleapis.com/v1beta/auth_tokens",
                       {"x-goog-api-key": env["GEMINI_API_KEY"], "Content-Type": "application/json"}, body)
        token = data.get("name") if isinstance(data, dict) else None
        if not token:
            raise VendorError("auth_tokens: no 'name' in the response")
        setup = {"model": cfg["model"], "responseModalities": ["AUDIO"],
                 "systemInstruction": {"parts": [{"text": instructions}]},
                 "tools": [{"functionDeclarations": gemini_function_declarations()}],
                 "inputAudioTranscription": {}, "outputAudioTranscription": {},
                 # the history is re-billed every turn (official best practices): cap it with a sliding window
                 "contextWindowCompression": {"slidingWindow": {}}}
        if voice:
            setup["speechConfig"] = {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}
        return {"token": token,
                "ws_url": "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta."
                          "GenerativeService.BidiGenerateContentConstrained",
                "setup": {"setup": setup}, "greeting": GREETING}
    if adapter == "elevenlabs":
        url = ("https://api.elevenlabs.io/v1/convai/conversation/get-signed-url?agent_id="
               + urllib.request.quote(env["ELEVENLABS_AGENT_ID"]))
        _, data = http("GET", url, {"xi-api-key": env["ELEVENLABS_API_KEY"]}, None)
        if not isinstance(data, dict) or "signed_url" not in data:
            raise VendorError("get-signed-url: no 'signed_url' in the response")
        out = {"signed_url": data["signed_url"], "client_esm": ELEVENLABS_CLIENT_ESM}
        if overrides:   # only fields the agent allows in its Security tab (ELEVENLABS_OVERRIDES on this PC)
            out["overrides"] = overrides
        return out
    if adapter == "cartesia":
        _, data = http("POST", "https://api.cartesia.ai/access-token",
                       {"Authorization": f"Bearer {env['CARTESIA_API_KEY']}",
                        "Cartesia-Version": cfg["cartesia_version"], "Content-Type": "application/json"},
                       {"grants": {"agent": True}, "expires_in": 60})
        token = data.get("token") if isinstance(data, dict) else None
        if not token:
            raise VendorError("access-token: no 'token' in the response")
        # Managed Agents WebSocket (the legacy /agents/stream endpoint has no tool events). Client tools arrive
        # as client_tool_call and are answered with client_tool_result. Output audio uses the input format.
        return {"token": token, "cartesia_version": cfg["cartesia_version"],
                "ws_url": f"wss://api.cartesia.ai/v1/agents/websocket/{urllib.request.quote(env['CARTESIA_AGENT_ID'])}",
                "start": {"type": "session_create", "audio": {"input_format": "pcm_16000"}}}
    raise VendorError(f"unknown adapter {adapter}")


def exchange_live_sdp(cfg: dict, env: dict, offer_sdp: str, http: Http = http_json,
                      instructions: str = LAB_INSTRUCTIONS, voice: str | None = None) -> dict:
    """GPT-Live: the server posts the browser's offer to /v1/live/sessions with the key and returns the answer.

    Business logic uses Responses delegation: the backend model gets the reception tools as functions and its
    function calls reach the page as response.event / response.output_item.done, which the page sends to the
    common reception service and answers with response.item.create + response.create."""
    model = delegation_model(cfg, env)
    body = {"session": {
        "model": cfg["model"], "instructions": instructions, "audio": {"output": {"voice": voice or "marin"}},
        "delegation": {"type": "responses", "responses": {
            "model": model, "instructions": instructions, "tools": _function_tools(), "tool_choice": "auto",
            "parallel_tool_calls": False, "max_output_tokens": MAX_OUTPUT_TOKENS}},
        "store": False},
        "transport": {"type": "webrtc", "sdp": offer_sdp}}
    _, data = http("POST", "https://api.openai.com/v1/live/sessions",
                   {"Authorization": f"Bearer {env['OPENAI_API_KEY']}", "Content-Type": "application/json"}, body)
    sdp = ((data.get("transport") or {}).get("sdp")) if isinstance(data, dict) else None
    if not sdp:
        raise VendorError("live/sessions: no transport.sdp in the response")
    sid = (data.get("session") or {}).get("id") or data.get("id")
    return {"sdp": sdp, "vendor_session_id": sid or "unknown"}   # a session exists even if its id is missing
