"""Per-turn spend accounting for Realtime dialogues, linked by event ids.

Each caller turn gets a reservation before its audio is sent. Usage is linked to a reservation by id,
never by "whatever turn is current when the event arrives":
- speech_started / committed carry the caller item_id: the item belongs to the turn whose audio was
  being streamed when the item started.
- response.created carries the response id: the response belongs to the turn of the most recently
  committed caller item (tool follow-up responses stay with that turn).
- response.done is matched by response id, input transcription by item_id.
A reservation is closed only when every response linked to it finished with usage and every caller item
linked to it was transcribed with usage. Missing usage keeps that reservation held. Usage that cannot be
linked is recorded as unmatched (kept in the totals) and, if its usage is missing too, every reservation
stays held. Duplicate events (same id) are counted once.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .budget import UsageLedger


@dataclass
class TurnState:
    rid: str
    estimate_usd: float
    items: set = field(default_factory=set)
    transcribed: set = field(default_factory=set)
    responses: set = field(default_factory=set)
    done: set = field(default_factory=set)
    missing: list = field(default_factory=list)
    linked_usd: float = 0.0


class TurnAccounting:
    def __init__(self, ledger: UsageLedger | None, path: str, run_id: str, transcription: bool = True) -> None:
        self.ledger, self.path, self.run_id, self.transcribe_enabled = ledger, path, run_id, transcription
        self.turns: dict[str, TurnState] = {}
        self.order: list[str] = []
        self.streaming: str | None = None
        self.item_turn: dict[str, str] = {}
        self.response_turn: dict[str, str] = {}
        self.last_committed: str | None = None
        self.seen_done: set[str] = set()
        self.seen_transcription: set[str] = set()
        self.unmatched: list[dict] = []
        self.unattributed_missing = False
        self.exceeded: list[str] = []
        self.duplicates = 0
        self._anon = 0
        self._anon_pending: list[str] = []

    # --- turns -------------------------------------------------------------------------

    def open_turn(self, operation: str, estimate_usd: float, audio_seconds: float = 0.0) -> str | None:
        """Reserve before the turn's audio is sent (raises BudgetExceeded through the ledger)."""
        if self.ledger is None:
            return None
        rid = self.ledger.reserve(operation, self.path, estimate_usd, self.run_id, audio_seconds=audio_seconds)
        self.turns[rid] = TurnState(rid, estimate_usd)
        self.order.append(rid)
        self.streaming = rid
        return rid

    def _turn_for_new_item(self) -> str | None:
        return self.streaming or (self.order[-1] if self.order else None)

    # --- caller items ------------------------------------------------------------------

    def speech_started(self, item_id: str | None) -> None:
        if item_id and item_id not in self.item_turn and self._turn_for_new_item():
            self.item_turn[item_id] = self._turn_for_new_item()

    def committed(self, item_id: str | None) -> None:
        rid = self.item_turn.get(item_id) if item_id else None
        rid = rid or self._turn_for_new_item()
        if rid is None:
            return
        if item_id:
            self.item_turn[item_id] = rid
            self.turns[rid].items.add(item_id)
        self.last_committed = rid

    def transcription(self, item_id: str | None, usage: dict | None, cost: float, **units) -> str:
        if item_id and item_id in self.seen_transcription:
            self.duplicates += 1
            return "duplicate"
        if item_id:
            self.seen_transcription.add(item_id)
        rid = self.item_turn.get(item_id) if item_id else None
        if rid is None:
            return self._unmatched("input_transcription", usage, cost, item_id=item_id, **units)
        state = self.turns[rid]
        state.transcribed.add(item_id)
        if not usage:
            state.missing.append(("transcription", item_id))
            return "missing"
        self._link(rid, "input_transcription", cost, transcription_usage=usage, **units)
        return "recorded"

    # --- responses ---------------------------------------------------------------------

    def response_created(self, response_id: str | None) -> str:
        if not response_id:
            self._anon += 1
            response_id = f"anon-{self._anon}"
            self._anon_pending.append(response_id)
        rid = self.last_committed or self._turn_for_new_item()
        if rid is not None:
            self.response_turn[response_id] = rid
            self.turns[rid].responses.add(response_id)
        return response_id

    def responses_for(self, rid: str | None) -> int:
        return len(self.turns[rid].responses) if rid in self.turns else 0

    def response_done(self, response_id: str | None, usage: dict | None, cost: float,
                      audio_seconds: float = 0.0, **units) -> str:
        if not response_id and self._anon_pending:
            response_id = self._anon_pending.pop(0)
        if response_id and response_id in self.seen_done:
            self.duplicates += 1
            return "duplicate"
        if response_id:
            self.seen_done.add(response_id)
        rid = self.response_turn.get(response_id) if response_id else None
        if rid is None:
            return self._unmatched("dialog_response", usage, cost, response_id=response_id,
                                   audio_seconds=audio_seconds, **units)
        state = self.turns[rid]
        state.done.add(response_id)
        if not usage:
            state.missing.append(("response", response_id))
            return "missing"
        self._link(rid, "dialog_response", cost, audio_seconds=audio_seconds, usage=usage, **units)
        return "recorded"

    # --- ledger ------------------------------------------------------------------------

    def _link(self, rid: str, operation: str, cost: float, audio_seconds: float = 0.0, **units) -> None:
        state = self.turns[rid]
        state.linked_usd += cost
        if self.ledger is not None:
            self.ledger.record(operation, self.path, cost, self.run_id, audio_seconds=audio_seconds,
                               reservation=rid, **units)
        if state.linked_usd > state.estimate_usd and rid not in self.exceeded:
            self.exceeded.append(rid)

    def _unmatched(self, operation: str, usage: dict | None, cost: float, audio_seconds: float = 0.0,
                   **units) -> str:
        self.unmatched.append({"operation": operation, "usage_reported": bool(usage), **units})
        if not usage:
            self.unattributed_missing = True
            return "unmatched_missing"
        if self.ledger is not None:
            self.ledger.record(operation, self.path, cost, self.run_id, audio_seconds=audio_seconds,
                               unmatched=True, **units)
        return "unmatched"

    # --- settlement --------------------------------------------------------------------

    def pending_transcriptions(self) -> int:
        if not self.transcribe_enabled:
            return 0
        return sum(len(s.items - s.transcribed) for s in self.turns.values())

    def complete(self, rid: str) -> bool:
        s = self.turns[rid]
        if s.missing or not s.responses <= s.done:
            return False
        return not self.transcribe_enabled or s.items <= s.transcribed

    def settle(self, clean: bool) -> dict:
        """Close the complete reservations (only after a clean finish); everything else stays held."""
        closed, held = [], []
        for rid in self.order:
            if clean and not self.unattributed_missing and self.complete(rid):
                if self.ledger is not None:
                    self.ledger.close(rid)
                closed.append(rid)
            else:
                held.append(rid)
        return {"closed": closed, "held": held, "unmatched": len(self.unmatched), "duplicates": self.duplicates,
                "exceeded": list(self.exceeded)}
