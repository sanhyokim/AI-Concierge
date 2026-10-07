"""Server-side handling of the model's tool calls, shared by plan A and plan B prototypes.

Confirmation is enforced here (FieldState.confirm), not left to the model.
"""
from __future__ import annotations

import datetime as dt

from ..concierge.confirmation import FieldStore
from ..concierge.readings import date_phrase, datetime_phrase, digits_only, phone_reading


class ToolHandler:
    def __init__(self) -> None:
        self.store = FieldStore()
        self.last_caller_utterance = ""
        self.calls: list[dict] = []

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
                state.read_back(state.value, t_ms=t_ms)
                reading = self._reading(field, state.value)
                result = {"ok": True, "read_this_clearly": reading, "then_ask": "こちらでお間違いないでしょうか。"}
        elif name == "confirm_field":
            ok, reason = self.store.get(field).confirm(self._normalize(field, args.get("value", "")),
                                                       self.last_caller_utterance, t_ms=t_ms)
            result = {"ok": ok, "reason": reason}
        else:
            result = {"ok": False, "reason": f"unknown tool {name}"}
        self.calls.append({"t_ms": t_ms, "name": name, "args": args, "result": result})
        return result
