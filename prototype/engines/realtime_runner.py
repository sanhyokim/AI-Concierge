"""Plan A dialogue runner (OpenAI Realtime over WebSocket, no telephone network).

- Streams scripted caller audio (8 kHz mu-law) in real time, following each turn's timing rule.
- Simulates phone-side playback of the AI audio. When the server reports that the
  caller started speaking while AI audio is still playing, playback stops at once and
  ``conversation.item.truncate`` is sent with the played length, so unplayed audio is
  removed from the conversation.
- Logs event times, AI audio as heard, transcripts and tool calls.

Measurement point: the API side of this client. Telephone-network delay is not
included; it is not the same as point S (cloud recording) or T (caller handset).

The transport is injectable; tests use a fake server, real runs use WebSocketTransport.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from ..concierge.prompts import BASE_INSTRUCTIONS, TOOLS
from .budget import (UsageLedger, openai_cost, openai_upper_bound, openai_usage_breakdown, transcription_cost,
                     transcription_upper_bound)
from .tools import ToolHandler

BYTES_PER_MS = 8  # 8 kHz mu-law: one byte per sample
CHUNK_MS = 20
TURN_RESPONSES = 3   # budget reservation per caller turn: the reply plus replies after tool calls
TURN_AUDIO_S = 30.0
PATH = "openai_api_direct"
TRANSCRIPTION_WAIT_S = 2.0
AUDIO_DELTA_TYPES = {"response.output_audio.delta", "response.audio.delta"}
TRANSCRIPT_DELTA_TYPES = {"response.output_audio_transcript.delta", "response.audio_transcript.delta"}


class Transport(Protocol):
    async def send(self, event: dict) -> None: ...
    async def recv(self) -> dict: ...
    async def close(self) -> None: ...


class WebSocketTransport:
    def __init__(self, url: str, api_key: str) -> None:
        self.url, self.api_key, self.ws = url, api_key, None

    async def connect(self) -> "WebSocketTransport":
        import websockets  # optional dependency (requirements-vendor.txt)
        self.ws = await websockets.connect(self.url, max_size=None,
                                           additional_headers={"Authorization": f"Bearer {self.api_key}"})
        return self

    async def send(self, event: dict) -> None:
        await self.ws.send(json.dumps(event))

    async def recv(self) -> dict:
        return json.loads(await self.ws.recv())

    async def close(self) -> None:
        if self.ws is not None:
            await self.ws.close()


def session_update(voice: str, instructions: str = BASE_INSTRUCTIONS, speed: float = 1.0,
                   tools: list | None = None, transcription_model: str | None = "gpt-4o-transcribe",
                   turn_detection: dict | None = None) -> dict:
    """GA session shape: audio formats, voice and speed under session.audio."""
    audio_input: dict = {"format": {"type": "audio/pcmu"},
                         "turn_detection": turn_detection or {"type": "semantic_vad"}}
    if transcription_model:
        audio_input["transcription"] = {"model": transcription_model, "language": "ja"}
    return {"type": "session.update", "session": {
        "type": "realtime",
        "instructions": instructions,
        "output_modalities": ["audio"],
        "audio": {
            "input": audio_input,
            "output": {"format": {"type": "audio/pcmu"}, "voice": voice, "speed": speed},
        },
        "tools": TOOLS if tools is None else tools,
        "tool_choice": "auto",
    }}


@dataclass
class Playback:
    item_id: str
    start_t: float
    received_ms: float = 0.0
    stopped: bool = False
    audio: bytearray = field(default_factory=bytearray)

    def played_ms(self, now: float) -> float:
        return min(self.received_ms, (now - self.start_t) * 1000)

    def playing(self, now: float) -> bool:
        return not self.stopped and self.played_ms(now) < self.received_ms


class RealtimeRunner:
    def __init__(self, transport: Transport, voice: str, *, instructions: str = BASE_INSTRUCTIONS,
                 clock: Callable[[], float] = time.monotonic, idle_timeout_s: float = 20.0,
                 ledger: UsageLedger | None = None, run_id: str = "", model_key: str = "openai_rt21",
                 transcription_model: str | None = "gpt-4o-transcribe") -> None:
        self.t, self.voice, self.instructions = transport, voice, instructions
        self.ledger, self.run_id, self.model_key = ledger, run_id, model_key
        self.transcription_model = transcription_model
        self.usage: list[dict] = []
        self.transcription_usage: list[dict] = []
        self.turn_rids: list[str] = []
        self.current_rid: str | None = None
        self.rids_missing_usage: set[str] = set()
        self.committed = self.transcribed = 0
        self.clock, self.idle_timeout_s = clock, idle_timeout_s
        self.t0 = clock()
        self.tools = ToolHandler()
        self.events: list[dict] = []
        self.playbacks: list[Playback] = []
        self.responses_in_progress = 0
        self.caller_turn_start: float | None = None
        self.caller_onset_ms = 0.0
        self.caller_turn_end: float | None = None
        self.last_speech_stopped: float | None = None
        self.transcripts: dict[str, str] = {}
        self._new_audio = asyncio.Event()

    # --- helpers -------------------------------------------------------------

    def _now(self) -> float:
        return self.clock()

    def _log(self, kind: str, **data) -> None:
        self.events.append({"t_ms": round((self._now() - self.t0) * 1000), "kind": kind, **data})

    @property
    def current(self) -> Playback | None:
        return self.playbacks[-1] if self.playbacks else None

    # --- receive loop ----------------------------------------------------------

    async def _recv_loop(self) -> None:
        while True:
            ev = await self.t.recv()
            et = ev.get("type", "")
            now = self._now()
            if et in AUDIO_DELTA_TYPES:
                item_id = ev.get("item_id", "")
                cur = self.current
                if cur is None or cur.item_id != item_id:
                    cur = Playback(item_id, now)
                    self.playbacks.append(cur)
                    lat_vad = (now - self.last_speech_stopped) * 1000 if self.last_speech_stopped else None
                    lat_send = (now - self.caller_turn_end) * 1000 if self.caller_turn_end else None
                    self._log("ai_audio_start", item_id=item_id,
                              latency_from_vad_end_ms=None if lat_vad is None else round(lat_vad),
                              latency_from_caller_audio_end_ms=None if lat_send is None else round(lat_send))
                    self._new_audio.set()
                if not cur.stopped:
                    chunk = base64.b64decode(ev.get("delta", ""))
                    cur.audio += chunk
                    cur.received_ms += len(chunk) / BYTES_PER_MS
            elif et == "input_audio_buffer.speech_started":
                cur = self.current
                if cur is not None and cur.playing(now):
                    played = cur.played_ms(now)
                    cur.stopped = True
                    del cur.audio[int(played * BYTES_PER_MS):]
                    await self.t.send({"type": "conversation.item.truncate", "item_id": cur.item_id,
                                       "content_index": 0, "audio_end_ms": int(played)})
                    onset = (self.caller_turn_start or now) + self.caller_onset_ms / 1000
                    self._log("barge_in", item_id=cur.item_id, played_ms=round(played),
                              received_ms=round(cur.received_ms),
                              stop_after_caller_onset_ms=round((now - onset) * 1000))
                else:
                    self._log("speech_started")
            elif et == "input_audio_buffer.speech_stopped":
                self.last_speech_stopped = now
                self._log("speech_stopped")
            elif et in TRANSCRIPT_DELTA_TYPES:
                item_id = ev.get("item_id", "")
                self.transcripts[item_id] = self.transcripts.get(item_id, "") + ev.get("delta", "")
            elif et == "input_audio_buffer.committed":
                self.committed += 1
            elif et == "conversation.item.input_audio_transcription.completed":
                self.tools.last_caller_utterance = ev.get("transcript", "")
                self.transcribed += 1
                self._log("caller_transcript", text=self.tools.last_caller_utterance)
                self._record_usage("input_transcription", ev.get("usage"), transcription_cost(ev.get("usage")),
                                   transcription_usage=ev.get("usage"))
            elif et == "conversation.item.input_audio_transcription.failed":
                self.transcribed += 1
                self._record_usage("input_transcription", None, 0.0)  # may still be billed: keep the turn held
            elif et == "response.function_call_arguments.done":
                await self._on_tool_call(ev)
            elif et == "response.created":
                self.responses_in_progress += 1
            elif et == "response.done":
                self.responses_in_progress = max(0, self.responses_in_progress - 1)
                usage = (ev.get("response") or {}).get("usage") or {}
                cost = openai_cost(usage, self.model_key)
                self._log("response_done", est_cost_usd=round(cost, 6), usage_reported=bool(usage))
                self._record_usage("dialog_response", usage, cost,
                                   audio_seconds=(self.current.received_ms / 1000 if self.current else 0.0),
                                   tokens=openai_usage_breakdown(usage), usage=usage)
            elif et == "error":
                self._log("error", error=ev.get("error"))

    def _record_usage(self, operation: str, reported: dict | None, cost: float, audio_seconds: float = 0.0,
                      **units) -> None:
        """Usage linked to the current turn's reservation. Missing usage keeps that reservation held."""
        if reported:
            (self.transcription_usage if operation == "input_transcription" else self.usage).append(reported)
        if self.ledger is None:
            return
        if not reported:
            if self.current_rid:
                self.rids_missing_usage.add(self.current_rid)
            self._log("usage_missing", operation=operation)
            return
        self.ledger.record(operation, PATH, cost, self.run_id, audio_seconds=audio_seconds,
                           reservation=self.current_rid, **units)

    async def _settle(self) -> None:
        """Close the turn reservations only after a clean finish: no response in progress, every committed
        caller turn transcribed (waits briefly), and no usage missing. Otherwise they stay held."""
        if self.ledger is None:
            return
        deadline = self._now() + TRANSCRIPTION_WAIT_S
        while self.transcription_model and self.transcribed < self.committed and self._now() < deadline:
            await asyncio.sleep(0.05)
        clean = self.responses_in_progress == 0 and (not self.transcription_model or self.transcribed >= self.committed)
        held = []
        for rid in self.turn_rids:
            if clean and rid not in self.rids_missing_usage:
                self.ledger.close(rid)
            else:
                held.append(rid)
        self._log("reservations", closed=len(self.turn_rids) - len(held), held=held)

    async def _on_tool_call(self, ev: dict) -> None:
        try:
            args = json.loads(ev.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        result = self.tools.handle(ev.get("name", ""), args, t_ms=round((self._now() - self.t0) * 1000))
        self._log("tool_call", name=ev.get("name"), args=args, result=result)
        await self.t.send({"type": "conversation.item.create", "item": {
            "type": "function_call_output", "call_id": ev.get("call_id"), "output": json.dumps(result,
                                                                                             ensure_ascii=False)}})
        await self.t.send({"type": "response.create"})

    # --- turn timing -----------------------------------------------------------

    async def _wait_ai_finished(self, since_index: int) -> None:
        deadline = self._now() + self.idle_timeout_s
        while self._now() < deadline:
            started = len(self.playbacks) > since_index
            cur = self.current
            if started and self.responses_in_progress == 0 and cur is not None and not cur.playing(self._now()):
                return
            await asyncio.sleep(0.02)
        self._log("timeout_waiting_ai_end")

    async def _wait_ai_started(self, since_index: int) -> None:
        deadline = self._now() + self.idle_timeout_s
        while len(self.playbacks) <= since_index and self._now() < deadline:
            self._new_audio.clear()
            try:
                await asyncio.wait_for(self._new_audio.wait(), timeout=0.05)
            except asyncio.TimeoutError:
                pass
        if len(self.playbacks) <= since_index:
            self._log("timeout_waiting_ai_start")

    async def _stream(self, pcmu: bytes) -> None:
        step = CHUNK_MS * BYTES_PER_MS
        start = self._now()
        for i in range(0, len(pcmu), step):
            await self.t.send({"type": "input_audio_buffer.append",
                               "audio": base64.b64encode(pcmu[i:i + step]).decode()})
            target = start + (i + step) / BYTES_PER_MS / 1000
            await asyncio.sleep(max(0.0, target - self._now()))

    # --- public ----------------------------------------------------------------

    async def run(self, turns: list[dict], load_audio: Callable[[dict], tuple[bytes, float]]) -> dict:
        """turns: caller turns with timing; load_audio(turn) -> (mu-law bytes, speech onset in ms)."""
        await self.t.send(session_update(self.voice, self.instructions, transcription_model=self.transcription_model))
        recv_task = asyncio.create_task(self._recv_loop())
        try:
            for n, turn in enumerate(turns, 1):
                since = len(self.playbacks)
                timing = turn.get("timing", {"type": "immediate"})
                if timing["type"] == "after_ai_end" and n > 1:
                    await self._wait_ai_finished(since_index=self._playbacks_before_last_turn)
                    await asyncio.sleep(timing.get("delay_ms", 0) / 1000)
                elif timing["type"] == "during_ai_speech":
                    await self._wait_ai_started(since_index=self._playbacks_before_last_turn)
                    await asyncio.sleep(timing.get("offset_ms", 0) / 1000)
                pcmu, onset_ms = load_audio(turn)
                if self.ledger is not None:  # raises BudgetExceeded before any audio of this turn is sent
                    caller_s = len(pcmu) / BYTES_PER_MS / 1000
                    est = (TURN_RESPONSES * openai_upper_bound(TURN_AUDIO_S, self.model_key)
                           + (transcription_upper_bound(caller_s) if self.transcription_model else 0.0))
                    self.current_rid = self.ledger.reserve(f"dialog_turn:{n}", PATH, est, self.run_id,
                                                           audio_seconds=TURN_AUDIO_S)
                    self.turn_rids.append(self.current_rid)
                self.caller_turn_start, self.caller_onset_ms = self._now(), onset_ms
                self._log("caller_turn_start", n=n, text=turn.get("text", ""))
                await self._stream(pcmu)
                self.caller_turn_end = self._now()
                self._playbacks_before_last_turn = len(self.playbacks)
                self._log("caller_turn_end", n=n)
            await self._wait_ai_finished(since_index=self._playbacks_before_last_turn)
            await self._settle()
        finally:
            recv_task.cancel()
            try:
                await recv_task
            except (asyncio.CancelledError, Exception):
                pass
        return self.result()

    _playbacks_before_last_turn = 0

    def result(self) -> dict:
        return {
            "measurement_point": "api_side_websocket_no_phone_network",
            "voice": self.voice,
            "events": self.events,
            "ai_items": [{"item_id": p.item_id, "received_ms": round(p.received_ms), "truncated": p.stopped,
                          "transcript": self.transcripts.get(p.item_id, "")} for p in self.playbacks],
            "tool_calls": self.tools.calls,
            "usage": self.usage,
            "transcription_usage": self.transcription_usage,
            "est_cost_usd": round(sum(openai_cost(u, self.model_key) for u in self.usage)
                                  + sum(transcription_cost(u) for u in self.transcription_usage), 6),
            "fields": self.tools.store.snapshot(),
        }
