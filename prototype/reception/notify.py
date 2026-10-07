"""Notification outbox: one row per (tenant x target x call x version), sent in version order.

Follows spec 1-8 and design review v2.1 4-6:
- the retry key (UUID) is created and stored with the row before the first attempt and never changes;
- 200, and 409 returned for the same key, mean accepted ("受付済み"); delivery/reading is not claimed;
- 5xx / timeout: retry with the same key and growing intervals, within 24 h of the first attempt;
- 429 "rate limit": a few retries; 429 "monthly limit": stop the channel, show it, no automatic resume;
- other 4xx: stop for a person; still unknown 24 h after the first attempt: "結果不明・重複の可能性あり",
  a person resends (with 【再送】 and a new key) or discards;
- a correction is a new version; an unsent older version is merged into it ("訂正済み").
Real LINE sending is NOT done in this prototype: LineSender builds the official Messaging API request
(push with X-Line-Retry-Key) for review and refuses to send until the user approves the account,
recipients and sending.
"""
from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from .store import Store, iso, now_jst

TENANT = "noda-demo"
RETRY_WINDOW = dt.timedelta(hours=24)
RATE_LIMIT_RETRIES = 3

CHANNELS = {"simulation": "シミュレーション（送信しない）",
            "line": "LINE公式アカウント（Messaging API・未接続。承認前は送らない）"}
SIMULATE = {
    "success": "成功（200）",
    "conflict_409": "同じキーで409（受付済みとして扱う）",
    "fail_500_once": "1回目だけ500（同じキーで再試行して成功）",
    "fail_500_always": "500が続く（24時間で結果不明）",
    "timeout_once": "1回目だけ時間切れ（同じキーで再試行）",
    "rate_limit_429": "429 一時的な上限が続く（3回で止めて人が確認）",
    "monthly_limit_429": "429 月の上限（その経路を止める）",
    "bad_request_400": "400（再試行せず、人が確認）",
}
STATUS_LABELS = {
    "pending": "送信待ち", "retrying": "再試行待ち", "accepted": "受付済み", "failed_stopped": "失敗・人の確認待ち",
    "channel_stopped": "経路が停止中（月の上限）", "unknown": "結果不明・重複の可能性あり", "superseded": "訂正に統合",
    "discarded": "破棄", "resent": "再送済み（新しい通知へ）", "blocked_unapproved": "未送信（LINEは未接続・承認前）",
}
PROBLEM = ("failed_stopped", "channel_stopped", "unknown")
OPEN = ("pending", "retrying")


@dataclass
class SendResult:
    status: int | None
    request_id: str | None = None
    body: str = ""
    timeout: bool = False


class SimulatedSender:
    """Returns the outcome chosen for the target. Nothing leaves this machine."""

    def send(self, n: dict, target: dict) -> SendResult:
        mode, k = target["simulate"], n["attempts"]
        if mode == "conflict_409":
            return SendResult(409, request_id=f"sim-accepted-{n['retry_key'][:8]}")
        if mode in ("fail_500_once", "timeout_once") and k == 0:
            return SendResult(None, timeout=True) if mode == "timeout_once" else SendResult(500, body="internal error")
        if mode == "fail_500_always":
            return SendResult(500, body="internal error")
        if mode == "rate_limit_429":
            return SendResult(429, body="The API rate limit has been exceeded.")
        if mode == "monthly_limit_429":
            return SendResult(429, body="You have reached your monthly limit.")
        if mode == "bad_request_400":
            return SendResult(400, body="Invalid request")
        return SendResult(200, request_id=f"sim-{uuid.uuid4().hex[:8]}")


class LineSender:
    """Candidate structure for the official LINE Messaging API (push). Never sends in this prototype."""
    URL = "https://api.line.me/v2/bot/message/push"

    def build_request(self, n: dict, target: dict) -> dict:
        return {"method": "POST", "url": self.URL,
                "headers": {"Authorization": "Bearer ＜チャネルアクセストークン：未設定・サーバーの環境変数に置く＞",
                            "Content-Type": "application/json", "X-Line-Retry-Key": n["retry_key"]},
                "body": {"to": target["address"] or "＜LINEのユーザーID：未登録＞",
                         "messages": [{"type": "text", "text": n["body"]}]}}


class Outbox:
    def __init__(self, store: Store, clock=now_jst, sender: SimulatedSender | None = None) -> None:
        self.store, self.clock = store, clock
        self.sender = sender or SimulatedSender()
        self.line = LineSender()

    # --- targets ---------------------------------------------------------------------------------------
    def targets(self, enabled_only: bool = False) -> list[dict]:
        rows = self.store.q("SELECT * FROM notify_targets ORDER BY id")
        for r in rows:
            r["channel_label"] = CHANNELS.get(r["channel"], r["channel"])
            r["simulate_label"] = SIMULATE.get(r["simulate"], r["simulate"])
        return [r for r in rows if r["enabled"]] if enabled_only else rows

    def save_target(self, data: dict, user: str | None, tid: int | None = None) -> int:
        name = str(data.get("name", "")).strip()[:60]
        channel, simulate = data.get("channel", "simulation"), data.get("simulate", "success")
        if not name:
            raise ValueError("宛先の名前を入力してください")
        if channel not in CHANNELS:
            raise ValueError("経路が正しくありません")
        if simulate not in SIMULATE:
            raise ValueError("シミュレーションの結果が正しくありません")
        address = str(data.get("address", "")).strip()[:80]
        enabled = 1 if data.get("enabled", True) else 0
        now = iso(self.clock())
        with self.store.tx():
            if tid is None:
                tid = self.store.x("INSERT INTO notify_targets(name, channel, address, enabled, simulate, created_at, "
                                   "updated_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                                   (name, channel, address, enabled, simulate, now, now)).lastrowid
                self.store.audit(user, "target_add", str(tid), {"name": name, "channel": channel})
            else:
                if not self.store.q1("SELECT id FROM notify_targets WHERE id = ?", (tid,)):
                    raise ValueError("宛先が見つかりません")
                self.store.x("UPDATE notify_targets SET name = ?, channel = ?, address = ?, enabled = ?, simulate = ?, "
                             "updated_at = ? WHERE id = ?", (name, channel, address, enabled, simulate, now, tid))
                self.store.audit(user, "target_update", str(tid), {"name": name, "enabled": bool(enabled),
                                                                   "simulate": simulate})
        return tid

    # --- enqueue ---------------------------------------------------------------------------------------
    def ident(self, target_id: int, call_id: str, version: int, suffix: str = "") -> str:
        return f"{TENANT}:t{target_id}:{call_id}:v{version}{suffix}"

    def enqueue(self, call_id: str, version: int, kind: str, body_for, suffix: str = "") -> dict:
        """Create one row per enabled target unless it already exists (re-injection is a no-op).

        body_for(prefix) returns the text; prefix marks a correction of an earlier version."""
        created, rows = 0, []
        now = iso(self.clock())
        with self.store.tx():
            for t in self.targets(enabled_only=True):
                ident = self.ident(t["id"], call_id, version, suffix)
                existing = self.store.q1("SELECT * FROM notifications WHERE ident = ?", (ident,))
                if existing:
                    rows.append(existing)
                    continue
                prefix = ""
                if version > 1 and not suffix:
                    older = self.store.q("SELECT * FROM notifications WHERE call_id = ? AND target_id = ? AND "
                                         "version >= 1 AND version < ? AND status NOT IN ('superseded', 'discarded')",
                                         (call_id, t["id"], version))
                    unsent = [o for o in older if o["status"] in OPEN or o["status"] == "blocked_unapproved"]
                    for o in unsent:
                        self._set(o["id"], "superseded", last_error=f"版{version}に統合（訂正）")
                    sent_or_unknown = [o for o in older if o not in unsent]
                    prefix = "【訂正】先ほどの通知を訂正します。" if sent_or_unknown else "【訂正済み】"
                nid = self.store.x(
                    "INSERT INTO notifications(ident, call_id, target_id, version, kind, retry_key, body, status, "
                    "created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                    (ident, call_id, t["id"], version, kind, str(uuid.uuid4()), body_for(prefix), now, now)).lastrowid
                created += 1
                rows.append(self.store.q1("SELECT * FROM notifications WHERE id = ?", (nid,)))
        return {"created": created, "ids": [r["id"] for r in rows]}

    # --- processing ------------------------------------------------------------------------------------
    def _set(self, nid: int, status: str, **cols) -> None:
        cols["status"], cols["updated_at"] = status, iso(self.clock())
        sets = ", ".join(f"{k} = ?" for k in cols)
        self.store.x(f"UPDATE notifications SET {sets} WHERE id = ?", (*cols.values(), nid))

    def _attempt(self, n: dict, outcome: str, http_status: int | None, detail: str) -> None:
        self.store.x("INSERT INTO notification_attempts(notification_id, at, retry_key, outcome, http_status, detail) "
                     "VALUES(?, ?, ?, ?, ?, ?)", (n["id"], iso(self.clock()), n["retry_key"], outcome, http_status,
                                                  detail))

    def channel_stopped(self, channel: str) -> bool:
        row = self.store.q1("SELECT stopped FROM channel_state WHERE channel = ?", (channel,))
        return bool(row and row["stopped"])

    def process(self, force_due: bool = False) -> dict:
        """Send what is due. force_due ignores the retry wait (simulation button); the 24 h rule still applies."""
        now = self.clock()
        done = {"sent": 0, "accepted": 0, "retrying": 0, "stopped": 0, "unknown": 0, "not_sent": 0, "skipped": 0}
        targets = {t["id"]: t for t in self.targets()}
        with self.store.tx():
            rows = self.store.q("SELECT * FROM notifications WHERE status IN ('pending', 'retrying') "
                                "ORDER BY call_id, target_id, version, id")
            for n in rows:
                t = targets.get(n["target_id"])
                if t is None:
                    continue
                if n["version"] >= 1 and self.store.q1(
                        "SELECT id FROM notifications WHERE call_id = ? AND target_id = ? AND version >= 1 AND "
                        "version < ? AND status IN ('pending', 'retrying')", (n["call_id"], n["target_id"], n["version"])):
                    done["skipped"] += 1          # versions go out one by one, in order
                    continue
                first = dt.datetime.fromisoformat(n["first_attempt_at"]) if n["first_attempt_at"] else None
                if first and now - first >= RETRY_WINDOW:
                    self._set(n["id"], "unknown", last_error="最初の送信から24時間を過ぎても結果が確定しない")
                    self._attempt(n, "unknown_after_24h", None, "自動の再送はしない。人が再送か破棄を選ぶ")
                    done["unknown"] += 1
                    continue
                if n["status"] == "retrying" and not force_due and n["next_attempt_at"] and \
                        dt.datetime.fromisoformat(n["next_attempt_at"]) > now:
                    continue
                if self.channel_stopped(t["channel"]):
                    continue
                if t["channel"] == "line":
                    self._set(n["id"], "blocked_unapproved",
                              last_error="実際のLINE送信は、送信先・接続設定・送信の承認が済むまで行いません")
                    self._attempt(n, "not_sent_unapproved", None, "LineSenderは要求の組み立てだけ（送信しない）")
                    done["not_sent"] += 1
                    continue
                res = self.sender.send(n, t)
                attempts = n["attempts"] + 1
                base = {"attempts": attempts, "first_attempt_at": n["first_attempt_at"] or iso(now)}
                done["sent"] += 1
                if res.status == 200 or res.status == 409:
                    self._set(n["id"], "accepted", accepted_request_id=res.request_id, next_attempt_at=None,
                              last_error=None, **base)
                    self._attempt(n, "accepted", res.status, "同じキーの409は受付済みとして扱う" if res.status == 409 else "")
                    done["accepted"] += 1
                elif res.timeout or (res.status is not None and res.status >= 500):
                    wait = dt.timedelta(minutes=min(60, 2 ** (attempts - 1)))
                    self._set(n["id"], "retrying", next_attempt_at=iso(now + wait),
                              last_error="時間切れ" if res.timeout else f"HTTP {res.status}", **base)
                    self._attempt(n, "retry_same_key", res.status, "時間切れ" if res.timeout else res.body)
                    done["retrying"] += 1
                elif res.status == 429 and "monthly limit" in res.body:
                    self.store.x("INSERT INTO channel_state(channel, stopped, reason, at) VALUES(?, 1, ?, ?) "
                                 "ON CONFLICT(channel) DO UPDATE SET stopped = 1, reason = excluded.reason, at = excluded.at",
                                 (t["channel"], "月の上限（429）。残りの通数を確かめ、人が再開する", iso(now)))
                    self._set(n["id"], "channel_stopped", last_error="429 月の上限", **base)
                    self._attempt(n, "channel_stopped", 429, res.body)
                    done["stopped"] += 1
                elif res.status == 429 and attempts < RATE_LIMIT_RETRIES:
                    self._set(n["id"], "retrying", next_attempt_at=iso(now + dt.timedelta(minutes=attempts)),
                              last_error="429 一時的な上限", **base)
                    self._attempt(n, "retry_same_key", 429, res.body)
                    done["retrying"] += 1
                else:
                    self._set(n["id"], "failed_stopped", last_error=f"HTTP {res.status}：再試行せず、人が確認", **base)
                    self._attempt(n, "failed_stopped", res.status, res.body)
                    done["stopped"] += 1
        return done

    # --- people's decisions ----------------------------------------------------------------------------
    def resend(self, nid: int, user: str | None) -> int:
        n = self.store.q1("SELECT * FROM notifications WHERE id = ?", (nid,))
        if not n or n["status"] not in PROBLEM:
            raise ValueError("再送できるのは、失敗・結果不明・停止中の通知だけです")
        now = iso(self.clock())
        with self.store.tx():
            k = self.store.q1("SELECT COUNT(*) AS c FROM notifications WHERE ident LIKE ?", (n["ident"] + ":r%",))["c"]
            new_id = self.store.x(
                "INSERT INTO notifications(ident, call_id, target_id, version, kind, retry_key, body, status, created_at, "
                "updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (f"{n['ident']}:r{k + 1}", n["call_id"], n["target_id"], n["version"], n["kind"], str(uuid.uuid4()),
                 "【再送】" + n["body"], now, now)).lastrowid
            self._set(nid, "resent", last_error=f"人が再送を選んだ（新しい通知 {new_id}）")
            self.store.audit(user, "notification_resend", str(nid), {"new": new_id})
        return new_id

    def discard(self, nid: int, user: str | None) -> None:
        n = self.store.q1("SELECT * FROM notifications WHERE id = ?", (nid,))
        if not n or n["status"] not in PROBLEM + ("blocked_unapproved",):
            raise ValueError("破棄できるのは、失敗・結果不明・停止中・未送信の通知だけです")
        with self.store.tx():
            self._set(nid, "discarded", last_error="人が破棄を選んだ")
            self.store.audit(user, "notification_discard", str(nid))

    def resume_channel(self, channel: str, user: str | None) -> None:
        with self.store.tx():
            self.store.x("UPDATE channel_state SET stopped = 0, reason = '人が再開', at = ? WHERE channel = ?",
                         (iso(self.clock()), channel))
            self.store.x("UPDATE notifications SET status = 'retrying', next_attempt_at = NULL, updated_at = ? "
                         "WHERE status = 'channel_stopped' AND target_id IN (SELECT id FROM notify_targets WHERE channel = ?)",
                         (iso(self.clock()), channel))
            self.store.audit(user, "channel_resume", channel)

    # --- views -----------------------------------------------------------------------------------------
    def for_call(self, call_id: str) -> list[dict]:
        rows = self.store.q("SELECT n.*, t.name AS target_name, t.channel FROM notifications n "
                            "JOIN notify_targets t ON t.id = n.target_id WHERE n.call_id = ? ORDER BY n.version, n.id",
                            (call_id,))
        for r in rows:
            r["status_label"] = STATUS_LABELS.get(r["status"], r["status"])
            if r["status"] == "accepted" and r["channel"] == "simulation":
                r["status_label"] = "受付済み（シミュレーション）"
            r["attempts_log"] = self.store.q("SELECT * FROM notification_attempts WHERE notification_id = ? ORDER BY id",
                                             (r["id"],))
            if r["channel"] == "line":
                r["line_request_preview"] = self.line.build_request(r, {"address": ""})
        return rows

    def problems(self) -> dict:
        rows = self.store.q("SELECT status, COUNT(*) AS c FROM notifications WHERE status IN "
                            "('failed_stopped', 'channel_stopped', 'unknown', 'blocked_unapproved') GROUP BY status")
        stopped = self.store.q("SELECT channel, reason, at FROM channel_state WHERE stopped = 1")
        return {"counts": {r["status"]: r["c"] for r in rows}, "stopped_channels": stopped}
