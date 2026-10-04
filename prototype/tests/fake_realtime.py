"""A small fake of the Realtime server, enough to exercise RealtimeRunner offline.

Behaviour (deterministic, real time):
- When caller audio arrives after silence: emits speech_started after ``vad_start_ms``.
- When caller audio stops for ``vad_end_ms``: emits speech_stopped, then after
  ``think_ms`` streams an AI response of ``response_ms`` audio, faster than real time.
- Records every client event, including conversation.item.truncate.
It does not model any vendor's quality, latency or behaviour; it only tests the harness.
"""
from __future__ import annotations

import asyncio
import base64
import itertools


class FakeRealtime:
    def __init__(self, *, vad_start_ms=60, vad_end_ms=200, think_ms=150, response_ms=1500,
                 delta_ms=100, send_interval_ms=20) -> None:
        self.cfg = dict(vad_start_ms=vad_start_ms, vad_end_ms=vad_end_ms, think_ms=think_ms,
                        response_ms=response_ms, delta_ms=delta_ms, send_interval_ms=send_interval_ms)
        self.out: asyncio.Queue = asyncio.Queue()
        self.client_events: list[dict] = []
        self._ids = itertools.count(1)
        self._speaking = False
        self._end_timer: asyncio.Task | None = None
        self._tasks: list[asyncio.Task] = []

    async def send(self, event: dict) -> None:
        self.client_events.append(event)
        if event["type"] == "input_audio_buffer.append":
            if not self._speaking:
                self._speaking = True
                self._tasks.append(asyncio.create_task(self._emit_later(
                    self.cfg["vad_start_ms"], {"type": "input_audio_buffer.speech_started"})))
            if self._end_timer:
                self._end_timer.cancel()
            self._end_timer = asyncio.create_task(self._end_of_speech())

    async def _emit_later(self, ms: int, event: dict) -> None:
        await asyncio.sleep(ms / 1000)
        await self.out.put(event)

    async def _end_of_speech(self) -> None:
        await asyncio.sleep(self.cfg["vad_end_ms"] / 1000)
        self._speaking = False
        await self.out.put({"type": "input_audio_buffer.speech_stopped"})
        await asyncio.sleep(self.cfg["think_ms"] / 1000)
        await self._respond()

    async def _respond(self) -> None:
        item_id = f"item_{next(self._ids)}"
        await self.out.put({"type": "response.created"})
        chunk = b"\xff" * (8 * self.cfg["delta_ms"])  # mu-law near-silence; content is irrelevant here
        for _ in range(self.cfg["response_ms"] // self.cfg["delta_ms"]):
            await self.out.put({"type": "response.output_audio.delta", "item_id": item_id,
                                "delta": base64.b64encode(chunk).decode()})
            await asyncio.sleep(self.cfg["send_interval_ms"] / 1000)
        await self.out.put({"type": "response.output_audio_transcript.delta", "item_id": item_id,
                            "delta": "（模擬応答）"})
        await self.out.put({"type": "response.done"})

    async def recv(self) -> dict:
        return await self.out.get()

    async def close(self) -> None:
        for task in self._tasks + ([self._end_timer] if self._end_timer else []):
            task.cancel()

    def truncates(self) -> list[dict]:
        return [e for e in self.client_events if e["type"] == "conversation.item.truncate"]
