"""Confirmation state of a captured field (callback number, preferred date, ...).

A value becomes ``confirmed_by_caller`` only when the caller explicitly affirms
the exact value that was just read back. A reply that contains a negation
("はい、違います") is never treated as an affirmation.

In the conversational prototypes the model proposes ``confirm_field`` through a
tool call; the server applies it through ``FieldState.confirm`` so this guard
holds regardless of what the model decides.

Live conversations (ToolHandler) also require, for a confirmation:
- the caller's reply is a transcript event that arrived AFTER the read-back was requested
  (``reply_seq > read_back_after_seq``), so an earlier "はい" cannot be reused;
- the value and its revision are the ones that were read back (a later correction invalidates it);
- the vendor did not report that the read-back was interrupted;
- a reply to a phone number contains no digits that differ from the number read back.
Calling ``confirm`` without sequence numbers is kept for the offline scenario checks only.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum


class Status(str, Enum):
    EMPTY = "empty"
    HEARD = "heard"
    AWAITING = "awaiting_confirmation"
    CORRECTED = "corrected_unconfirmed"
    REJECTED = "rejected_by_caller"
    CONFIRMED = "confirmed_by_caller"
    STAFF_CORRECTED = "corrected_by_staff"   # changed by staff after the call; not a caller confirmation


class Reply(str, Enum):
    AFFIRMATIVE = "affirmative"
    NEGATIVE = "negative"
    UNCLEAR = "unclear"


# Affirmations that contain a negative-looking substring (間違い) are removed before the negative check,
# so that the rest of the reply is still examined for a negation or a correction.
_AFFIRM_WITH_NEGATIVE_CHARS = ("間違いない", "間違いありません", "まちがいない", "まちがいありません")
_NEGATIVE = ("違", "ちが", "いいえ", "いや", "じゃなく", "ではなく", "間違", "まちが", "そうじゃない")
_AFFIRMATIVE = ("合って", "あって", "そうです", "その通り", "そのとおり", "それで大丈夫", "大丈夫です",
                "それでいい", "それでお願いします", "はい", "ええ", "うん")
# A correction that follows an affirmation: 「はい、間違いありません。でも末尾は…」
_CORRECTION_WORDS = ("末尾", "最後は", "最後の", "下4桁", "下四桁", "変更", "訂正", "変えて", "けど", "けれど", "ですが",
                     "ただし")
_CORRECTION_LEADS = re.compile(r"(^|[、。,.!?！？])(でも|ただ|だけど|しかし)")


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text).replace(" ", "").replace("\u3000", "")


def classify_reply(text: str) -> Reply:
    t = _norm(text)
    affirm_phrase = False
    for p in _AFFIRM_WITH_NEGATIVE_CHARS:
        if p in t:
            t, affirm_phrase = t.replace(p, "〇"), True
    if any(p in t for p in _NEGATIVE) or any(p in t for p in _CORRECTION_WORDS) or _CORRECTION_LEADS.search(t):
        return Reply.NEGATIVE
    if affirm_phrase or any(p in t for p in _AFFIRMATIVE):
        return Reply.AFFIRMATIVE
    return Reply.UNCLEAR


def _foreign_digits(reply: str, value: str) -> list[str]:
    """Digit runs in the reply that do not occur in the value (a phone number given again, differently)."""
    digits = re.sub(r"\D", "", value or "")
    return [run for run in re.findall(r"\d+", _norm(reply)) if run not in digits]


@dataclass
class FieldState:
    name: str
    value: str | None = None
    status: Status = Status.EMPTY
    source: str | None = None
    read_back_value: str | None = None
    history: list[dict] = field(default_factory=list)
    revision: int = 0                       # +1 every time a value is heard (a correction is a new revision)
    read_back_revision: int | None = None
    read_back_after_seq: int | None = None  # caller-utterance number when the read-back was requested
    readback_interrupted: bool = False

    def _log(self, event: str, t_ms: int | None, **extra) -> None:
        self.history.append({"event": event, "value": self.value, "status": self.status.value,
                             "revision": self.revision, "t_ms": t_ms, **extra})

    def hear(self, value: str, source: str = "speech", t_ms: int | None = None) -> Status:
        """A value was heard (or typed via DTMF). A different value after a previous one is a correction."""
        had_value = self.status != Status.EMPTY
        changed = value != self.value
        self.value, self.source, self.read_back_value = value, source, None
        self.revision += 1
        self.read_back_revision, self.read_back_after_seq, self.readback_interrupted = None, None, False
        self.status = Status.CORRECTED if had_value and changed else Status.HEARD
        self._log("hear", t_ms)
        return self.status

    def read_back(self, value: str, t_ms: int | None = None, after_seq: int | None = None) -> Status:
        """The read-back was requested (prepared). It is not proof that the caller heard all of it."""
        if value != self.value:
            raise ValueError(f"read back {value!r} but the stored value is {self.value!r}")
        self.read_back_value = value
        self.read_back_revision, self.read_back_after_seq, self.readback_interrupted = self.revision, after_seq, False
        self.status = Status.AWAITING
        self._log("read_back", t_ms, after_seq=after_seq)
        return self.status

    def interrupt_readback(self, source: str = "", t_ms: int | None = None) -> bool:
        """The vendor reported that the AI's speech was cut off while this read-back was pending."""
        if self.status != Status.AWAITING:
            return False
        self.readback_interrupted = True
        self._log("read_back_interrupted", t_ms, source=source)
        return True

    def answer(self, utterance: str, t_ms: int | None = None) -> Reply:
        """The caller replied after a read-back. Only an affirmative reply confirms."""
        reply = classify_reply(utterance)
        if self.status == Status.AWAITING and not self.readback_interrupted:
            if reply == Reply.AFFIRMATIVE and self.read_back_value == self.value:
                self.status = Status.CONFIRMED
            elif reply == Reply.NEGATIVE:
                self.status = Status.REJECTED
        self._log("answer", t_ms, utterance=utterance, reply=reply.value)
        return reply

    def confirm(self, value: str, last_caller_utterance: str, t_ms: int | None = None,
                reply_seq: int | None = None) -> tuple[bool, str]:
        """Guard for a model-proposed confirmation (tool call). Returns (accepted, reason)."""
        if self.status != Status.AWAITING:
            return False, f"not awaiting confirmation (status={self.status.value}); read the value back first"
        if value != self.read_back_value or self.revision != self.read_back_revision:
            return False, "value differs from the value that was read back"
        if self.readback_interrupted:
            return False, "the read-back was interrupted; read it back again and ask"
        if self.read_back_after_seq is not None and (reply_seq is None or reply_seq <= self.read_back_after_seq):
            return False, "no caller reply after the read-back yet; wait for the reply"
        if self.name == "callback_number" and _foreign_digits(last_caller_utterance, value):
            return False, "the reply contains a different number; save it and read it back again"
        reply = classify_reply(last_caller_utterance)
        if reply != Reply.AFFIRMATIVE:
            return False, f"caller reply is {reply.value}; ask again"
        self.answer(last_caller_utterance, t_ms)
        self._log("confirmed_by_reply", t_ms, reply_seq=reply_seq)
        return True, "confirmed"


class FieldStore:
    def __init__(self) -> None:
        self.fields: dict[str, FieldState] = {}

    def get(self, name: str) -> FieldState:
        return self.fields.setdefault(name, FieldState(name))

    def snapshot(self) -> dict:
        return {n: {"value": f.value, "status": f.status.value, "source": f.source}
                for n, f in self.fields.items()}
