"""Which route an incoming call takes. The admin page, the API and the tests all use decide().

Rule (spec 2, made concrete by the implementation instruction v1):
1. AI failure at call start (forced action) - only when the result would be AI.
2. Manual modes ignore the schedule: always_ai -> AI, always_normal -> normal.
3. Schedule mode, judged in Japan time (Asia/Tokyo):
   special day (whole day) > weekly band [start, end) > the 'outside hours' setting.
Manual-override expiry (spec 2, recommendation) is not implemented yet: a manual mode stays until changed,
and the page warns while it is on.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass

from .settings import MODE_LABELS, ROUTE_LABELS, WEEKDAY_LABELS, WEEKDAYS
from .store import JST, iso, parse_at


@dataclass(frozen=True)
class Decision:
    route: str            # ai | normal | dtmf
    rule: str             # ai_failure | manual_always_ai | manual_always_normal | special_day | weekly_band | outside_hours
    reason: str           # Japanese explanation shown on the page
    at_jst: str
    within_staff_hours: bool   # inside a weekly 'normal' band (used for the after-hours notice A-04)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["route_label"] = ROUTE_LABELS[self.route]
        return d


def _minute_of_day(t: dt.datetime) -> float:
    return t.hour * 60 + t.minute + t.second / 60 + t.microsecond / 60_000_000


def _band_at(config: dict, t: dt.datetime) -> dict | None:
    m = _minute_of_day(t)
    for b in config["schedule"]["weekly"].get(WEEKDAYS[t.weekday()], []):
        sh, sm = map(int, b["start"].split(":"))
        eh, em = map(int, b["end"].split(":"))
        if sh * 60 + sm <= m < eh * 60 + em:
            return b
    return None


def decide(config: dict, at: dt.datetime | str | None = None, ai_available: bool = True) -> Decision:
    t = parse_at(at)
    day_label = f"{t:%Y-%m-%d}（{WEEKDAY_LABELS[WEEKDAYS[t.weekday()]]}）{t:%H:%M:%S}"
    band = _band_at(config, t)
    within_staff = bool(band and band["mode"] == "normal")
    mode = config["mode"]
    if mode == "always_ai":
        route, rule, why = "ai", "manual_always_ai", f"{MODE_LABELS[mode]}（スケジュールは見ない）"
    elif mode == "always_normal":
        route, rule, why = "normal", "manual_always_normal", f"{MODE_LABELS[mode]}（スケジュールは見ない）"
    else:
        special = next((s for s in config["schedule"]["special_days"] if s["date"] == t.date().isoformat()), None)
        if special:
            route, rule = special["mode"], "special_day"
            why = f"スケジュール運用：特別日 {special['date']}{'（' + special['note'] + '）' if special['note'] else ''}"
        elif band:
            route, rule = band["mode"], "weekly_band"
            why = f"スケジュール運用：{WEEKDAY_LABELS[WEEKDAYS[t.weekday()]]}曜日 {band['start']}〜{band['end']} の時間帯"
        else:
            route, rule = config["schedule"]["outside"], "outside_hours"
            why = "スケジュール運用：設定した時間帯の外（時間外の扱い）"
    if route == "ai" and not ai_available:
        forced = config["ai_failure_action"]
        return Decision(forced, "ai_failure",
                        f"AIが使えないため、障害時の動作（{ROUTE_LABELS[forced]}）。本来は {why} でAI受電", iso(t),
                        within_staff)
    return Decision(route, rule, f"{why} → {ROUTE_LABELS[route]}", iso(t), within_staff)


def to_jst(t: dt.datetime) -> dt.datetime:
    return t.astimezone(JST)
