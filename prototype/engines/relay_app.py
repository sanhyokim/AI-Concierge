"""Plan B skeleton: WebSocket handler for Twilio ConversationRelay.

NOT EXECUTED in this round: it needs a Twilio account, a phone number and a public
wss:// URL, which require a contract decision. The pure functions (TwiML, token
building, history truncation, DTMF hand-off) are unit-tested offline.

Message types follow the ConversationRelay WebSocket documentation:
  in : setup, prompt, interrupt (utteranceUntilInterrupt), dtmf, error
  out: text (token, last), play, end (handoffData)
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol
from xml.sax.saxutils import escape, quoteattr

from ..concierge.call_flow import CallFlow
from ..concierge.ssml import relay_slow_token
from .tools import ToolHandler


@dataclass
class Segment:
    text: str
    slow: bool = False


class Brain(Protocol):
    async def reply(self, history: list[dict], tools: ToolHandler) -> list[Segment]: ...


class ScriptedBrain:
    """Deterministic replies for dry runs; the real dialogue model is not chosen yet."""

    def __init__(self, replies: list[list[Segment]]) -> None:
        self.replies = list(replies)

    async def reply(self, history, tools) -> list[Segment]:
        return self.replies.pop(0) if self.replies else [Segment("承知しました。")]


def twiml_connect(ws_url: str, voice: str, action_url: str, greeting: str) -> str:
    attrs = {"url": ws_url, "ttsProvider": "Amazon", "voice": voice, "language": "ja-JP",
             "transcriptionProvider": "Google", "dtmfDetection": "true", "interruptible": "any",
             "welcomeGreeting": greeting}
    attr_text = " ".join(f"{k}={quoteattr(v)}" for k, v in attrs.items())
    return (f'<?xml version="1.0" encoding="UTF-8"?><Response><Connect action={quoteattr(action_url)}>'
            f"<ConversationRelay {attr_text}/></Connect></Response>")


def twiml_dtmf_entry(action_url: str, clip_base_url: str) -> str:
    """After `end` hand-off: pre-recorded prompt and keypad entry only (no AI)."""
    return (f'<?xml version="1.0" encoding="UTF-8"?><Response>'
            f'<Gather input="dtmf" finishOnKey="#" timeout="8" action={quoteattr(action_url)}>'
            f"<Play>{escape(clip_base_url)}/clip_dtmf_enter_number.wav</Play></Gather></Response>")


def build_tokens(segments: list[Segment], slow_rate: str) -> list[dict]:
    """One text token per segment; a slowed segment is one self-contained SSML token."""
    msgs = []
    for i, seg in enumerate(segments):
        token = relay_slow_token(seg.text, slow_rate) if seg.slow else escape(seg.text)
        msgs.append({"type": "text", "token": token, "last": i == len(segments) - 1})
    return msgs


def truncate_history(history: list[dict], utterance_until_interrupt: str) -> list[dict]:
    """Keep only what the caller heard of the last assistant turn."""
    for entry in reversed(history):
        if entry["role"] == "assistant":
            entry["content"] = utterance_until_interrupt
            entry["interrupted"] = True
            break
    return history


class RelaySession:
    def __init__(self, brain: Brain, slow_rate: str = "80%", caller_id: str | None = None) -> None:
        self.brain, self.slow_rate = brain, slow_rate
        self.tools = ToolHandler()
        self.flow = CallFlow(caller_id=caller_id)
        self.history: list[dict] = []

    async def on_message(self, msg: dict) -> list[dict]:
        kind = msg.get("type")
        if kind == "setup":
            self.flow.caller_id = msg.get("from") or self.flow.caller_id
            return []
        if kind == "prompt":
            text = msg.get("voicePrompt", "")
            self.tools.last_caller_utterance = text
            self.history.append({"role": "user", "content": text})
            segments = await self.brain.reply(self.history, self.tools)
            self.history.append({"role": "assistant", "content": "".join(s.text for s in segments)})
            return build_tokens(segments, self.slow_rate)
        if kind == "interrupt":
            truncate_history(self.history, msg.get("utteranceUntilInterrupt", ""))
            return []
        if kind == "dtmf":
            return []  # keypad input is handled by the TwiML fallback after `end`
        return []

    def refuse_ai(self, via_human_request: bool) -> list[dict]:
        actions = (self.flow.on_human_request_answer(False) if via_human_request else self.flow.on_ai_refused())
        if ("stop_ai_stream",) in actions:
            return [{"type": "end", "handoffData": json.dumps({"reason": "ai_refused"})}]
        return []


async def serve(host: str = "0.0.0.0", port: int = 8080) -> None:  # pragma: no cover - needs Twilio
    import websockets

    async def handler(ws):
        session = RelaySession(ScriptedBrain([]))
        async for raw in ws:
            for out in await session.on_message(json.loads(raw)):
                await ws.send(json.dumps(out, ensure_ascii=False))

    async with websockets.serve(handler, host, port):
        await __import__("asyncio").Future()
