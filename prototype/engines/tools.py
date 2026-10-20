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

READBACK_FIELDS = ("callback_number", "preferred_datetime")   # read back digit by digit / with the weekday
THEN_ASK = "こちらでお間違いないでしょうか。"
READBACK_TAIL_MS = 2000   # about the time 「こちらでお間違いないでしょうか。」 takes to say


class ToolHandler:
    def __init__(self) -> None:
        self.store = FieldStore()
        self.calls: list[dict] = []
        self._utt_text = ""
        self._utt_seq = 0
        self._utt_ids: set[str] = set()
        self._consumed: set[int] = set()
        self._last_readback: str | None = None   # the field read back most recently

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

    def readback_interrupted(self, source: str = "", unplayed_ms: int | None = None) -> list[str]:
        """The vendor reported that the AI was cut off. Invalidate only read-backs the caller has not answered yet.

        unplayed_ms: how much of the AI's audio had not been played yet (the page knows). When only the closing
        question was left (READBACK_TAIL_MS), the caller heard the number or the date: the cut was the caller's
        「はい」 itself, and it still answers the read-back (2026-10-20, Gemini: every 「はい」 said a little early
        stopped the AI, the read-back was then refused, and the AI asked again)."""
        if unplayed_ms is not None and 0 <= unplayed_ms <= READBACK_TAIL_MS:
            return []
        out = []
        for name, f in self.store.fields.items():
            if f.status == Status.AWAITING and f.read_back_after_seq == self._utt_seq and f.interrupt_readback(source):
                out.append(name)
        return out

    def ai_said(self, text: str, t_ms: int | None = None) -> dict[str, str]:
        """The AI's own transcript of what it just said. A read-back that said a different number or date than
        the saved value is marked, so the caller's 「はい」 to it cannot confirm the value. Returns field -> what
        was said instead."""
        out = {}
        for name, f in self.store.fields.items():
            wrong = f.check_spoken(text, t_ms)
            if wrong:
                out[name] = wrong
        # The caller's 「はい」 answers what the AI just read back, not what was saved last: on 2026-10-19 the
        # number and the date were saved together, the AI read back the number, and the 「はい」 confirmed the date.
        spoken = [name for name, f in self.store.fields.items() if f.spoke_now]
        for name in spoken:   # the AI read an interrupted read-back again by itself: the next reply answers it
            f = self.store.fields[name]
            if f.readback_interrupted:
                f.read_back(f.value, t_ms=t_ms, after_seq=self._utt_seq)
                f.readback_spoken_ok = True
                f._log("read_back_again_by_ai", t_ms)
        if len(spoken) == 1:
            self._last_readback = spoken[0]
        elif spoken:      # both in one breath: one 「はい」 cannot tell which; the model confirms each itself
            self._last_readback = None
        return out

    def confirm_from_reply(self, t_ms: int | None = None) -> str | None:
        """After a caller utterance: confirm the item read back most recently, when the server itself saw the AI
        say the value (its transcript) and this utterance is a clear affirmation. The model often answers
        「ありがとうございます」 without calling confirm_field (2026-10-13: neither the date nor the number was
        confirmed), and every extra tool call is a pause in the conversation. Returns the confirmed field."""
        name = self._last_readback
        f = self.store.fields.get(name) if name else None
        if (not f or f.status != Status.AWAITING or not f.readback_spoken_ok or self._utt_seq in self._consumed
                or f.read_back_after_seq is None or self._utt_seq <= f.read_back_after_seq):
            return None
        ok, _ = f.confirm(f.value, self._utt_text, t_ms=t_ms, reply_seq=self._utt_seq)
        if not ok:
            return None
        self._consumed.add(self._utt_seq)
        f._log("confirmed_by_server", t_ms, reply_seq=self._utt_seq)
        return name

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

    def start_readback(self, field: str, t_ms: int | None = None) -> dict:
        """Mark the read-back of a saved value and return the exact words to say. The caller's next reply is the
        answer to it."""
        state = self.store.get(field)
        state.read_back(state.value, t_ms=t_ms, after_seq=self._utt_seq)
        self._last_readback = field
        reading = self._reading(field, state.value)
        return {"field": field, "read_this_clearly": reading, "then_ask": THEN_ASK,
                "say": f"復唱いたします。{reading}。{THEN_ASK}"}

    def save(self, field: str, value: str, t_ms: int | None = None, source: str = "speech") -> str:
        """Store a heard value. The same value again keeps a confirmation already given (the model often
        re-sends every value at the end of the call)."""
        state = self.store.get(field)
        value = self._normalize(field, value)
        # the same value again while it is read back or confirmed changes nothing (2026-10-20: save_fields at the
        # end of the call turned the number back to "heard" and started its read-back over)
        if state.status in (Status.CONFIRMED, Status.AWAITING) and state.value == value:
            return state.status.value
        return state.hear(value, source=source, t_ms=t_ms).value

    def needs_readback(self, field: str) -> bool:
        f = self.store.fields.get(field)
        return field in READBACK_FIELDS and bool(f and f.value) and f.status in (Status.HEARD, Status.CORRECTED)

    def handle(self, name: str, args: dict, t_ms: int | None = None) -> dict:
        field = args.get("field", "")
        if name == "save_field":
            status = self.save(field, args.get("value", ""), t_ms=t_ms)
            result = {"ok": True, "status": status}
            if self.needs_readback(field):   # one tool call instead of save + request_readback
                result["read_back_now"] = self.start_readback(field, t_ms)
                result["status"] = self.store.get(field).status.value
        elif name == "request_readback":
            state = self.store.get(field)
            if state.value is None:
                result = {"ok": False, "reason": "no value saved yet"}
            else:
                result = {"ok": True, **{k: v for k, v in self.start_readback(field, t_ms).items() if k != "field"}}
        elif name == "confirm_field":
            state = self.store.get(field)
            if (state.status == Status.CONFIRMED and state.value is not None
                    and self._normalize(field, args.get("value", "")) == state.value):
                result = {"ok": True, "reason": "already confirmed by the server from the read-back and the reply"}
            elif self._utt_seq in self._consumed:
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
