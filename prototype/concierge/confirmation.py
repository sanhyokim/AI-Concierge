"""Confirmation state of a captured field (callback number, preferred date, ...).

A value becomes ``confirmed_by_caller`` only when the caller explicitly affirms
the exact value that was just read back. A reply that contains a negation
("はい、違います") is never treated as an affirmation.

In the conversational prototypes the model proposes ``confirm_field`` through a
tool call; the server applies it through ``FieldState.confirm`` so this guard
holds regardless of what the model decides.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Status(str, Enum):
    EMPTY = "empty"
    HEARD = "heard"
    AWAITING = "awaiting_confirmation"
    CORRECTED = "corrected_unconfirmed"
    REJECTED = "rejected_by_caller"
    CONFIRMED = "confirmed_by_caller"


class Reply(str, Enum):
    AFFIRMATIVE = "affirmative"
    NEGATIVE = "negative"
    UNCLEAR = "unclear"


# Affirmations that contain a negative-looking substring (間違い) must be checked first.
_AFFIRM_WITH_NEGATIVE_CHARS = ("間違いない", "間違いありません", "まちがいない", "まちがいありません")
_NEGATIVE = ("違", "ちが", "いいえ", "いや", "じゃなく", "ではなく", "間違", "まちが", "そうじゃない")
_AFFIRMATIVE = ("合って", "あって", "そうです", "その通り", "そのとおり", "それで大丈夫", "大丈夫です",
                "それでいい", "それでお願いします", "はい", "ええ", "うん")


def classify_reply(text: str) -> Reply:
    t = text.replace(" ", "").replace("　", "")
    if any(p in t for p in _AFFIRM_WITH_NEGATIVE_CHARS):
        return Reply.AFFIRMATIVE
    if any(p in t for p in _NEGATIVE):
        return Reply.NEGATIVE
    if any(p in t for p in _AFFIRMATIVE):
        return Reply.AFFIRMATIVE
    return Reply.UNCLEAR


@dataclass
class FieldState:
    name: str
    value: str | None = None
    status: Status = Status.EMPTY
    source: str | None = None
    read_back_value: str | None = None
    history: list[dict] = field(default_factory=list)

    def _log(self, event: str, t_ms: int | None, **extra) -> None:
        self.history.append({"event": event, "value": self.value, "status": self.status.value,
                             "t_ms": t_ms, **extra})

    def hear(self, value: str, source: str = "speech", t_ms: int | None = None) -> Status:
        """A value was heard (or typed via DTMF). A different value after a previous one is a correction."""
        had_value = self.status != Status.EMPTY
        changed = value != self.value
        self.value, self.source, self.read_back_value = value, source, None
        self.status = Status.CORRECTED if had_value and changed else Status.HEARD
        self._log("hear", t_ms)
        return self.status

    def read_back(self, value: str, t_ms: int | None = None) -> Status:
        """The agent read the value back and asked for confirmation."""
        if value != self.value:
            raise ValueError(f"read back {value!r} but the stored value is {self.value!r}")
        self.read_back_value = value
        self.status = Status.AWAITING
        self._log("read_back", t_ms)
        return self.status

    def answer(self, utterance: str, t_ms: int | None = None) -> Reply:
        """The caller replied after a read-back. Only an affirmative reply confirms."""
        reply = classify_reply(utterance)
        if self.status == Status.AWAITING:
            if reply == Reply.AFFIRMATIVE and self.read_back_value == self.value:
                self.status = Status.CONFIRMED
            elif reply == Reply.NEGATIVE:
                self.status = Status.REJECTED
        self._log("answer", t_ms, utterance=utterance, reply=reply.value)
        return reply

    def confirm(self, value: str, last_caller_utterance: str, t_ms: int | None = None) -> tuple[bool, str]:
        """Guard for a model-proposed confirmation (tool call). Returns (accepted, reason)."""
        if self.status != Status.AWAITING:
            return False, f"not awaiting confirmation (status={self.status.value}); read the value back first"
        if value != self.read_back_value:
            return False, "value differs from the value that was read back"
        reply = classify_reply(last_caller_utterance)
        if reply != Reply.AFFIRMATIVE:
            return False, f"caller reply is {reply.value}; ask again"
        self.answer(last_caller_utterance, t_ms)
        return True, "confirmed"


class FieldStore:
    def __init__(self) -> None:
        self.fields: dict[str, FieldState] = {}

    def get(self, name: str) -> FieldState:
        return self.fields.setdefault(name, FieldState(name))

    def snapshot(self) -> dict:
        return {n: {"value": f.value, "status": f.status.value, "source": f.source}
                for n, f in self.fields.items()}
