"""Rule-based summary and notification text (no language model).

Only fields the settings allow are used. Each value carries its confirmation state; a value the caller has
not affirmed after a read-back is never written as confirmed. This is a fixed rule for the prototype; it says
nothing about the quality of a model-written summary.
"""
from __future__ import annotations

import datetime as dt

from ..concierge.readings import date_phrase, datetime_phrase, group_phone_number
from .settings import FIELD_KEYS, FIELD_LABELS

STATUS_LABELS = {
    "confirmed_by_caller": "本人確認済み",
    "heard": "聞き取りのみ・未確認",
    "awaiting_confirmation": "復唱済み・返事待ち（未確認）",
    "corrected_unconfirmed": "訂正後・未確認",
    "rejected_by_caller": "本人が否定・未確認",
    "corrected_by_staff": "担当者が補正",
    "empty": "未取得",
}
SOURCE_LABELS = {"speech": "発話", "dtmf": "プッシュボタン", "line": "回線の番号", "staff": "担当者"}
KIND_LABELS = {"intake": "受付", "emergency_obvious": "緊急", "emergency_ambiguous": "要注意",
               "emergency_undetermined": "要注意", "ai_failure": "AI障害", "ai_refused_intake": "AI拒否・プッシュボタンで受付",
               "ai_failure_intake": "AI障害・プッシュボタンで受付", "ai_refused_callback_missing": "AI拒否・折り返し先未取得",
               "ai_failure_callback_missing": "AI障害・折り返し先未取得"}
KEY_FIELDS = ("callback_number", "request")


def display_value(name: str, value: str | None) -> str:
    if not value:
        return ""
    if name == "callback_number":
        return "-".join(group_phone_number(value)) or value
    if name == "preferred_datetime":
        try:
            if len(value) == 10:
                return date_phrase(dt.date.fromisoformat(value))[0]
            return datetime_phrase(dt.datetime.fromisoformat(value))[0]
        except ValueError:
            return value
    return value


def build_summary(fields: dict[str, dict], config: dict, open_questions: list[str], kinds: list[str],
                  corrected: bool = False) -> dict:
    """fields: name -> {value, status, source}. Returns {text, items, review}."""
    items, review = [], []
    for name in FIELD_KEYS:
        rule = config["fields"][name]
        f = fields.get(name) or {"value": None, "status": "empty", "source": None}
        if not rule["store"]:
            continue
        status = f["status"] if f.get("value") else "empty"
        items.append({"name": name, "label": FIELD_LABELS[name], "value": f.get("value"),
                      "display": display_value(name, f.get("value")), "status": status,
                      "status_label": STATUS_LABELS.get(status, status),
                      "source_label": SOURCE_LABELS.get(f.get("source") or "", ""),
                      "in_summary": rule["summary"], "in_notify": rule["notify"]})
        if name in KEY_FIELDS and status == "empty":
            review.append(f"{FIELD_LABELS[name]}が未取得")
        elif status in ("heard", "awaiting_confirmation", "corrected_unconfirmed", "rejected_by_caller") and \
                name in ("callback_number", "preferred_datetime"):
            review.append(f"{FIELD_LABELS[name]}が未確認（{STATUS_LABELS[status]}）")
    if open_questions:
        review.append(f"確認事項 {len(open_questions)}件")
    if corrected:
        review.append("担当者による補正あり")
    lines = [f"【{kind_label(kinds)}】"]
    for it in items:
        if it["in_summary"] and it["status"] != "empty":
            lines.append(f"{it['label']}：{it['display']}（{it['status_label']}）")
    missing = [it["label"] for it in items if it["in_summary"] and it["status"] == "empty"]
    if missing:
        lines.append(f"未取得：{'・'.join(missing)}")
    if open_questions:
        lines.append("確認事項：" + "／".join(open_questions))
    lines.append("要確認：" + ("、".join(review) if review else "なし"))
    return {"text": "\n".join(lines), "items": items, "review": review}


def kind_label(kinds: list[str]) -> str:
    for k in ("emergency_obvious", "emergency_ambiguous", "emergency_undetermined", "ai_failure_callback_missing",
              "ai_refused_callback_missing", "ai_failure_intake", "ai_refused_intake", "ai_failure"):
        if k in kinds:
            return KIND_LABELS[k]
    return KIND_LABELS["intake"]


def notification_body(summary: dict, call_id: str, kinds: list[str], open_questions: list[str],
                      prefix: str = "") -> str:
    """Text for one notification. Only fields marked 'notify' are included; the state goes with every value."""
    lines = []
    if prefix:
        lines.append(prefix)
    lines.append(f"【{kind_label(kinds)}】（デモ・架空のデータ）")
    for it in summary["items"]:
        if it["in_notify"] and it["status"] != "empty":
            lines.append(f"{it['label']}：{it['display']}（{it['status_label']}）")
    unconf = [it["label"] for it in summary["items"] if it["in_notify"] and it["status"] not in
              ("confirmed_by_caller", "corrected_by_staff", "empty")]
    if unconf:
        lines.append("未確認：" + "・".join(unconf))
    if open_questions:
        lines.append(f"確認事項：{len(open_questions)}件（管理画面で確認）")
    lines.append(f"詳細：管理画面の受付 {call_id}（ログインが必要）")
    return "\n".join(lines)
