"""Spend control for vendor calls: a usage ledger plus hard limits checked before every request.

- Ledger: JSONL. Four kinds of line:
    reservation  written BEFORE a request is sent, holding a pessimistic estimate
    usage        actual (or estimated) usage, optionally linked to a reservation
    adjustment   reconciliation with the vendor console: the difference that makes the reservation's
                 total equal the console amount (counts in cost, not in requests; keeps an audit trail)
    close        the reservation is settled; from then on only its linked usage counts
  A reservation that is never closed (no usage reported, timeout, abort, crash) keeps counting
  at max(reserved, linked usage), so a request that may have been billed never drops out.
  Lines without "kind" (older ledgers) are read as usage.
- Limits: requests, audio seconds and estimated USD per vendor. A request whose projected
  total would exceed a limit is not sent (BudgetExceeded).
- Retries: at most one retry, only for network failures, timeouts and 5xx-like errors.
- OpenAI usage is recorded with uncached and cached input split out (text and audio), and
  input transcription (gpt-4o-transcribe) is recorded as its own operation.
Vendor-side budgets and alerts should be set as well; this guard does not replace them. The ledger is
an estimate kept on our side: it does not by itself guarantee a vendor-side spending cap.

python3 -m prototype.engines.budget                      totals, open reservations, token breakdown
python3 -m prototype.engines.budget --close RID --vendor openai --actual-usd 0.012 --note "console 10/05"
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import pathlib
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable, TypeVar

PRICES = json.loads((pathlib.Path(__file__).resolve().parents[1] / "cost" / "prices.json").read_text())["items"]
DEFAULT_LEDGER = pathlib.Path(__file__).resolve().parents[1] / "results" / "usage-ledger.jsonl"
TOKEN_KEYS = ("text_in_uncached", "text_in_cached", "audio_in_uncached", "audio_in_cached", "text_out", "audio_out")

T = TypeVar("T")


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Limits:
    max_requests: int
    max_audio_seconds: float
    max_cost_usd: float
    request_timeout_s: float = 60.0
    dialog_timeout_s: float = 180.0
    max_retries: int = 1


# Proposed (not yet approved) test budgets, measurement plan v1: about twice the planned counts in
# cost/measurement_budget.py (one full rerun). OpenAI $15 (plan about $6), AWS Polly $2 (plan about $0.10).
LIMITS = {
    "openai": Limits(max_requests=700, max_audio_seconds=3600, max_cost_usd=15.0),
    "aws_polly": Limits(max_requests=120, max_audio_seconds=600, max_cost_usd=2.0),
}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class UsageLedger:
    def __init__(self, vendor: str, limits: Limits | None = None, path: pathlib.Path = DEFAULT_LEDGER) -> None:
        self.vendor, self.limits, self.path = vendor, limits or LIMITS[vendor], pathlib.Path(path)

    # --- reading -------------------------------------------------------------------

    def _lines(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [rec for rec in (json.loads(line) for line in self.path.read_text().splitlines() if line.strip())
                if rec.get("vendor") == self.vendor]

    def open_reservations(self) -> list[dict]:
        lines = self._lines()
        closed = {r["rid"] for r in lines if r.get("kind") == "close"}
        return [r for r in lines if r.get("kind") == "reservation" and r["rid"] not in closed]

    def totals(self) -> dict:
        totals = {"requests": 0, "audio_seconds": 0.0, "est_cost_usd": 0.0, "open_reservations": 0,
                  "held_usd": 0.0, "tokens": {k: 0 for k in TOKEN_KEYS}, "transcription_usd": 0.0}
        lines = self._lines()
        linked: dict[str, dict] = {}
        for rec in lines:
            kind = rec.get("kind", "usage")
            if kind == "adjustment":
                totals["est_cost_usd"] += rec.get("est_cost_usd", 0.0)
                rid = rec.get("reservation")
                if rid:
                    linked.setdefault(rid, {"usd": 0.0, "audio": 0.0, "n": 0})["usd"] += rec.get("est_cost_usd", 0.0)
                continue
            if kind != "usage":
                continue
            totals["requests"] += 1
            totals["audio_seconds"] += rec.get("audio_seconds", 0.0)
            totals["est_cost_usd"] += rec.get("est_cost_usd", 0.0)
            for k, v in (rec.get("units", {}).get("tokens") or {}).items():
                if k in totals["tokens"]:
                    totals["tokens"][k] += v
            if rec.get("operation", "").startswith("input_transcription"):
                totals["transcription_usd"] += rec.get("est_cost_usd", 0.0)
            rid = rec.get("reservation")
            if rid:
                agg = linked.setdefault(rid, {"usd": 0.0, "audio": 0.0, "n": 0})
                agg["usd"] += rec.get("est_cost_usd", 0.0)
                agg["audio"] += rec.get("audio_seconds", 0.0)
                agg["n"] += 1
        adjusted = {rec["reservation"] for rec in lines if rec.get("kind") == "adjustment" and rec.get("reservation")}
        open_ids = {r["rid"] for r in self.open_reservations()}
        for rid in adjusted - open_ids:  # reconciled request with no usage line: it was still a request
            if linked.get(rid, {}).get("n", 0) == 0:
                totals["requests"] += 1
        for res in self.open_reservations():
            agg = linked.get(res["rid"], {"usd": 0.0, "audio": 0.0, "n": 0})
            held = max(0.0, res["est_cost_usd"] - agg["usd"])
            totals["est_cost_usd"] += held
            totals["held_usd"] += held
            totals["audio_seconds"] += max(0.0, res.get("audio_seconds", 0.0) - agg["audio"])
            totals["open_reservations"] += 1
            if agg["n"] == 0:
                totals["requests"] += 1  # sent (or possibly sent) but nothing reported back
        return totals

    def check(self, est_cost_usd: float, audio_seconds: float = 0.0) -> None:
        t, lim = self.totals(), self.limits
        if t["requests"] + 1 > lim.max_requests:
            raise BudgetExceeded(f"{self.vendor}: request limit {lim.max_requests} reached")
        if t["audio_seconds"] + audio_seconds > lim.max_audio_seconds:
            raise BudgetExceeded(f"{self.vendor}: audio limit {lim.max_audio_seconds}s would be exceeded")
        if t["est_cost_usd"] + est_cost_usd > lim.max_cost_usd:
            raise BudgetExceeded(f"{self.vendor}: cost limit ${lim.max_cost_usd} would be exceeded "
                                 f"(spent or held ${t['est_cost_usd']:.4f}, next ${est_cost_usd:.4f})")

    # --- writing -------------------------------------------------------------------

    def _write(self, rec: dict) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    def reserve(self, operation: str, path: str, est_cost_usd: float, run_id: str, audio_seconds: float = 0.0) -> str:
        """Check the limits, then hold est_cost_usd for a request about to be sent. Returns the reservation id."""
        self.check(est_cost_usd, audio_seconds)
        rid = uuid.uuid4().hex[:12]
        self._write({"ts": _now(), "vendor": self.vendor, "kind": "reservation", "rid": rid, "operation": operation,
                     "path": path, "run_id": run_id, "audio_seconds": round(audio_seconds, 3),
                     "est_cost_usd": round(est_cost_usd, 6)})
        return rid

    def record(self, operation: str, path: str, est_cost_usd: float, run_id: str, audio_seconds: float = 0.0,
               reservation: str | None = None, **units) -> dict:
        rec = {"ts": _now(), "vendor": self.vendor, "kind": "usage", "operation": operation, "path": path,
               "run_id": run_id, "audio_seconds": round(audio_seconds, 3), "est_cost_usd": round(est_cost_usd, 6),
               "units": units}
        if reservation:
            rec["reservation"] = reservation
        return self._write(rec)

    def close(self, rid: str, note: str = "") -> dict:
        return self._write({"ts": _now(), "vendor": self.vendor, "kind": "close", "rid": rid, "note": note})


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (ConnectionError, TimeoutError, asyncio.TimeoutError, OSError)):
        return True
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    return isinstance(status, int) and status >= 500


async def async_with_retries(fn: Callable[[], Awaitable[T]], max_retries: int = 1) -> T:
    attempt = 0
    while True:
        try:
            return await fn()
        except Exception as exc:  # noqa: BLE001 - classified below
            if attempt >= max_retries or not is_retryable(exc):
                raise
            attempt += 1
            await asyncio.sleep(1.0)


def with_retries(fn: Callable[[], T], max_retries: int = 1) -> T:
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            if attempt >= max_retries or not is_retryable(exc):
                raise
            attempt += 1


# --- cost estimators (same prices as the cost model) ----------------------------

def openai_usage_breakdown(usage: dict | None) -> dict:
    """Split a Realtime response usage object into uncached / cached input and output, text and audio."""
    if not usage:
        return {k: 0 for k in TOKEN_KEYS}
    inp = usage.get("input_token_details", {}) or {}
    cached = inp.get("cached_tokens_details", {}) or {}
    out = usage.get("output_token_details", {}) or {}
    cached_text, cached_audio = cached.get("text_tokens", 0), cached.get("audio_tokens", 0)
    return {"text_in_uncached": max(0, inp.get("text_tokens", 0) - cached_text), "text_in_cached": cached_text,
            "audio_in_uncached": max(0, inp.get("audio_tokens", 0) - cached_audio), "audio_in_cached": cached_audio,
            "text_out": out.get("text_tokens", 0), "audio_out": out.get("audio_tokens", 0)}


def openai_cost(usage: dict | None, model_key: str = "openai_rt21") -> float:
    """Cost of one Realtime response from its usage object (response.done)."""
    if not usage:
        return 0.0
    p, b = PRICES[model_key], openai_usage_breakdown(usage)
    return (b["text_in_uncached"] * p["text_in"] + b["audio_in_uncached"] * p["audio_in"]
            + b["text_in_cached"] * p["cached_text_in"] + b["audio_in_cached"] * p["cached_audio_in"]
            + b["text_out"] * p["text_out"] + b["audio_out"] * p["audio_out"]) / 1_000_000


def openai_upper_bound(audio_out_seconds: float, model_key: str = "openai_rt21") -> float:
    """Pessimistic pre-check for one response: output audio plus 20k uncached text tokens."""
    p = PRICES[model_key]
    return (audio_out_seconds * PRICES["openai_audio_tokens"]["output_per_s"] * p["audio_out"]
            + 20_000 * p["text_in"]) / 1_000_000


# Run settings the per-response bound is derived from (kept in sync with session.update and the runners)
MAX_OUTPUT_TOKENS = 1200          # session max_output_tokens: about 60 s of audio at 20 tokens/s
MAX_INSTRUCTION_TOKENS = 4000     # instructions + tool definitions, upper side
MAX_HISTORY_S = 180.0             # dialogue timeout bounds the audio history (caller and AI)


def openai_response_bound(model_key: str = "openai_rt21", max_output_tokens: int = MAX_OUTPUT_TOKENS,
                          history_s: float = MAX_HISTORY_S,
                          instruction_tokens: int = MAX_INSTRUCTION_TOKENS) -> float:
    """Upper bound for one response under the run settings: every input token uncached (instructions,
    plus the whole audio history allowed by the dialogue timeout, billed as if all of it were AI audio)
    and the output capped by max_output_tokens at the dearer of the audio/text output prices."""
    p, tok = PRICES[model_key], PRICES["openai_audio_tokens"]
    history_tokens = history_s * max(tok["input_per_s"], tok["output_per_s"])
    return (instruction_tokens * p["text_in"] + history_tokens * p["audio_in"]
            + max_output_tokens * max(p["audio_out"], p["text_out"])) / 1_000_000


def transcription_cost(usage: dict | None) -> float:
    """Cost of one input transcription (gpt-4o-transcribe) from its usage object; 0 when absent."""
    if not usage:
        return 0.0
    p = PRICES["openai_transcribe"]
    if usage.get("type") == "duration":
        return usage.get("seconds", 0.0) / 60 * p["per_min_estimate"]
    return (usage.get("input_tokens", 0) * p["in"] + usage.get("output_tokens", 0) * p["out"]) / 1_000_000


TRANSCRIPT_OUT_TOKENS_PER_S = 15  # pessimistic output tokens per second of Japanese speech
TRANSCRIPT_PROMPT_TOKENS = 50


def transcription_upper_bound(audio_seconds: float) -> float:
    """Pessimistic pre-check for transcribing audio_seconds of caller speech."""
    p = PRICES["openai_transcribe"]
    tokens_in = audio_seconds * PRICES["openai_audio_tokens"]["input_per_s"] + TRANSCRIPT_PROMPT_TOKENS
    return (tokens_in * p["in"] + audio_seconds * TRANSCRIPT_OUT_TOKENS_PER_S * p["out"]) / 1_000_000


def polly_cost(characters: int, engine: str = "neural") -> float:
    return characters * PRICES["polly_neural" if engine == "neural" else "polly_standard"]["value"] / 1_000_000


def reconcile(ledger: UsageLedger, rid: str, actual_usd: float, note: str = "") -> str:
    """Make the reservation's total equal the amount seen on the vendor's usage page.

    Writes an adjustment line (console total minus what is already linked), then closes the reservation
    if it is still open. A second reconciliation with the same amount changes nothing; a different amount
    is refused, so re-running never adds the console total twice. Returns "adjusted" or "unchanged".
    """
    lines = ledger._lines()
    res = next((r for r in lines if r.get("kind") == "reservation" and r["rid"] == rid), None)
    if res is None:
        raise KeyError(f"no reservation {rid} for {ledger.vendor}")
    linked = sum(r.get("est_cost_usd", 0.0) for r in lines
                 if r.get("reservation") == rid and r.get("kind", "usage") in ("usage", "adjustment"))
    already = [r for r in lines if r.get("kind") == "adjustment" and r.get("reservation") == rid]
    if already:
        if abs(linked - actual_usd) < 1e-9:
            return "unchanged"
        raise ValueError(f"{rid} was already reconciled to ${linked:.6f}; refusing to change it to ${actual_usd:.6f}")
    ledger._write({"ts": _now(), "vendor": ledger.vendor, "kind": "adjustment", "reservation": rid,
                   "operation": "reconcile", "path": res["path"], "run_id": res["run_id"],
                   "est_cost_usd": round(actual_usd - linked, 6), "previous_linked_usd": round(linked, 6),
                   "console_total_usd": round(actual_usd, 6), "note": note, "source": "vendor console"})
    if any(r["rid"] == rid for r in ledger.open_reservations()):
        ledger.close(rid, note=note)
    return "adjusted"


def main() -> int:
    ap = argparse.ArgumentParser(description="Show ledger totals, or close a reservation after checking the vendor console.")
    ap.add_argument("--vendor", choices=sorted(LIMITS))
    ap.add_argument("--close", metavar="RID")
    ap.add_argument("--actual-usd", type=float, help="actual cost from the vendor console for the reservation")
    ap.add_argument("--note", default="")
    args = ap.parse_args()
    if args.close:
        if not args.vendor or args.actual_usd is None:
            ap.error("--close needs --vendor and --actual-usd")
        try:
            print(reconcile(UsageLedger(args.vendor), args.close, args.actual_usd, args.note))
        except (KeyError, ValueError) as exc:
            ap.error(str(exc))
    for vendor in ([args.vendor] if args.vendor else sorted(LIMITS)):
        ledger = UsageLedger(vendor)
        print(json.dumps({"vendor": vendor, "totals": ledger.totals(),
                          "open": [{k: r[k] for k in ("rid", "operation", "run_id", "est_cost_usd")}
                                   for r in ledger.open_reservations()]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
