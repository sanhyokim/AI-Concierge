"""Spend control for vendor calls: a usage ledger plus hard limits checked before every request.

- Ledger: JSONL, one line per request (time, vendor, operation, path, units, estimated USD, run id).
  Totals are summed from the file, so limits hold across separate runs.
- Limits: requests, audio seconds and estimated USD per vendor. A request whose projected
  total would exceed a limit is not sent (BudgetExceeded).
- Retries: at most one retry, only for network failures, timeouts and 5xx-like errors.
Vendor-side budgets and alerts should be set as well; this guard does not replace them.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import pathlib
from dataclasses import dataclass
from typing import Awaitable, Callable, TypeVar

PRICES = json.loads((pathlib.Path(__file__).resolve().parents[1] / "cost" / "prices.json").read_text())["items"]
DEFAULT_LEDGER = pathlib.Path(__file__).resolve().parents[1] / "results" / "usage-ledger.jsonl"

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


class UsageLedger:
    def __init__(self, vendor: str, limits: Limits | None = None, path: pathlib.Path = DEFAULT_LEDGER) -> None:
        self.vendor, self.limits, self.path = vendor, limits or LIMITS[vendor], pathlib.Path(path)

    def totals(self) -> dict:
        totals = {"requests": 0, "audio_seconds": 0.0, "est_cost_usd": 0.0}
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                rec = json.loads(line)
                if rec["vendor"] == self.vendor:
                    totals["requests"] += 1
                    totals["audio_seconds"] += rec.get("audio_seconds", 0.0)
                    totals["est_cost_usd"] += rec.get("est_cost_usd", 0.0)
        return totals

    def check(self, est_cost_usd: float, audio_seconds: float = 0.0) -> None:
        t, lim = self.totals(), self.limits
        if t["requests"] + 1 > lim.max_requests:
            raise BudgetExceeded(f"{self.vendor}: request limit {lim.max_requests} reached")
        if t["audio_seconds"] + audio_seconds > lim.max_audio_seconds:
            raise BudgetExceeded(f"{self.vendor}: audio limit {lim.max_audio_seconds}s would be exceeded")
        if t["est_cost_usd"] + est_cost_usd > lim.max_cost_usd:
            raise BudgetExceeded(f"{self.vendor}: cost limit ${lim.max_cost_usd} would be exceeded "
                                 f"(spent ${t['est_cost_usd']:.4f}, next ${est_cost_usd:.4f})")

    def record(self, operation: str, path: str, est_cost_usd: float, run_id: str, audio_seconds: float = 0.0,
               **units) -> dict:
        rec = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "vendor": self.vendor,
               "operation": operation, "path": path, "run_id": run_id, "audio_seconds": round(audio_seconds, 3),
               "est_cost_usd": round(est_cost_usd, 6), "units": units}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec


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

def openai_cost(usage: dict | None, model_key: str = "openai_rt21") -> float:
    """Cost of one Realtime response from its usage object (response.done)."""
    if not usage:
        return 0.0
    p = PRICES[model_key]
    inp = usage.get("input_token_details", {}) or {}
    cached = inp.get("cached_tokens_details", {}) or {}
    out = usage.get("output_token_details", {}) or {}
    cached_text, cached_audio = cached.get("text_tokens", 0), cached.get("audio_tokens", 0)
    text_in = max(0, inp.get("text_tokens", 0) - cached_text)
    audio_in = max(0, inp.get("audio_tokens", 0) - cached_audio)
    m = 1_000_000
    return (text_in * p["text_in"] + audio_in * p["audio_in"] + cached_text * p["cached_text_in"]
            + cached_audio * p["cached_audio_in"] + out.get("text_tokens", 0) * p["text_out"]
            + out.get("audio_tokens", 0) * p["audio_out"]) / m


def openai_upper_bound(audio_out_seconds: float, model_key: str = "openai_rt21") -> float:
    """Pessimistic pre-check for one response: output audio plus 20k uncached text tokens."""
    p = PRICES[model_key]
    return (audio_out_seconds * PRICES["openai_audio_tokens"]["output_per_s"] * p["audio_out"]
            + 20_000 * p["text_in"]) / 1_000_000


def polly_cost(characters: int, engine: str = "neural") -> float:
    return characters * PRICES["polly_neural" if engine == "neural" else "polly_standard"]["value"] / 1_000_000
