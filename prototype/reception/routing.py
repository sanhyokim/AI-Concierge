"""Which route a call that reached this app takes. The admin page, the API and the tests all use decide().

The AI is switched on and off outside this app, by changing the NTT call forwarding (line plan 2, user decision
2026-10-07/08): while it is on, calls are forwarded to the AI number; while it is off, they go to the staff as
before and never reach this app. So there is no reception mode or schedule here:
1. A call that reaches the app is answered by the AI.
2. When the AI cannot be used at call start, the saved 'AI stopped' action is taken (forward to the staff mobile,
   or the recorded guidance with push buttons) so that no call is lost.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass

from .settings import ROUTE_LABELS
from .store import iso, parse_at


@dataclass(frozen=True)
class Decision:
    route: str            # ai | forward | dtmf
    rule: str             # received | ai_failure | browser_lab
    reason: str           # Japanese explanation shown on the page
    at_jst: str

    def as_dict(self) -> dict:
        d = asdict(self)
        d["route_label"] = ROUTE_LABELS[self.route]
        return d


def decide(config: dict, at: dt.datetime | str | None = None, ai_available: bool = True) -> Decision:
    t = parse_at(at)
    if not ai_available:
        action = config["ai_failure_action"]
        how = {"forward": "担当者の携帯へ転送する", "dtmf": "録音の案内とプッシュボタンで受け付ける"}[action]
        return Decision(action, "ai_failure", f"AIが使えないため、基本設定の「AIが止まったときの扱い」で{how}", iso(t))
    return Decision("ai", "received", "NTTの転送でこのアプリに届いた通話 → AI受付", iso(t))
