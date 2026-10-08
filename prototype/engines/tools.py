"""Server-side handling of the model's tool calls, shared by every adapter.

Confirmation is enforced here (FieldState.confirm), not left to the model.

Caller utterances: the only way to register what the caller said is ``caller_said(text, utterance_id)``,
called from an actual transcript event (a vendor's final transcript, the browser lab's utterance post, a relay
prompt, a demo line). A repeated ``utterance_id`` is a duplicate and is ignored. Text attached to a tool call
is never taken as a new utterance. Each reply can confirm at most one item, and only a read-back that was
requested before that reply.
"""
from __future__ import annotations

import datetime as dt

from ..concierge.confirmation import FieldStore, Status
from ..concierge.readings import date_phrase, datetime_phrase, digits_only, phone_reading


class ToolHandler:
    def __init__(self) -> None:
        self.store = FieldStore()
        self.calls: list[dict] = []
        self._utt_text = ""
        self._utt_seq = 0
        self._utt_ids: set[str] = set()
        self._consumed: set[int] = set()

    # --- caller utterances (transcript events only) ----------------------------------------------------
    @property
    def last_caller_utterance(self) -> str:
        return self._utt_text

    @property
    def utterance_seq(self) -> int:
        return self._utt_seq

    def caller_said(self, text: str, utterance_id: str | int | None = None) -> bool:
        """Register a caller utterance from a transcript event. Returns False for a duplicate id."""
        if utterance_id is not None:
            key = str(utterance_id)
            if key in self._utt_ids:
                return False
            self._utt_ids.add(key)
        self._utt_seq += 1
        self._utt_text = text
        return True

    def readback_interrupted(self, source: str = "") -> list[str]:
        """The vendor reported that the AI was cut off. Invalidate only read-backs the caller has not answered yet."""
        out = []
        for name, f in self.store.fields.items():
            if f.status == Status.AWAITING and f.read_back_after_seq == self._utt_seq and f.interrupt_readback(source):
                out.append(name)
        return out

    def awaiting_reply(self, field: str) -> bool:
        """True while a read-back of this field has no caller utterance after it."""
        f = self.store.fields.get(field)
        return bool(f and f.status == Status.AWAITING and f.read_back_after_seq is not None
                    and self._utt_seq <= f.read_back_after_seq)

    # --- tools ------------------------------------------------------------------------------------------
    def _normalize(self, field: str, value: str) -> str:
        return digits_only(value) if field == "callback_number" else value.strip()

    @staticmethod
    def _reading(field: str, value: str) -> str:
        """Phone numbers digit by digit in groups; a date-time given as ISO 8601 as a phrase with the weekday."""
        if field == "callback_number":
            return phone_reading(value)
        if field == "preferred_datetime":
            try:
                if len(value) == 10:   # a date without a time
                    return date_phrase(dt.date.fromisoformat(value))[0]
                return datetime_phrase(dt.datetime.fromisoformat(value))[0]
            except ValueError:
                return value
        return value

    def handle(self, name: str, args: dict, t_ms: int | None = None) -> dict:
        field = args.get("field", "")
        if name == "save_field":
            status = self.store.get(field).hear(self._normalize(field, args.get("value", "")), t_ms=t_ms)
            result = {"ok": True, "status": status.value}
        elif name == "request_readback":
            state = self.store.get(field)
            if state.value is None:
                result = {"ok": False, "reason": "no value saved yet"}
            else:
                state.read_back(state.value, t_ms=t_ms, after_seq=self._utt_seq)
                result = {"ok": True, "read_this_clearly": self._reading(field, state.value),
                          "then_ask": "こちらでお間違いないでしょうか。"}
        elif name == "confirm_field":
            if self._utt_seq in self._consumed:
                result = {"ok": False, "reason": "this reply was already used to confirm another item; ask again"}
            else:
                ok, reason = self.store.get(field).confirm(self._normalize(field, args.get("value", "")),
                                                           self._utt_text, t_ms=t_ms, reply_seq=self._utt_seq)
                if ok:
                    self._consumed.add(self._utt_seq)
                result = {"ok": ok, "reason": reason}
        else:
            result = {"ok": False, "reason": f"unknown tool {name}"}
        self.calls.append({"t_ms": t_ms, "name": name, "args": args, "result": result})
        return result
