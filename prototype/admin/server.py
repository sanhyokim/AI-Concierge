"""Admin web app server (standard library only).

python3 -m prototype.admin [--port 8780] [--db PATH] [--host 127.0.0.1]
Open http://127.0.0.1:8780 and log in (the first start prints the admin password once).

- Every API except /api/login needs a session; every POST also needs the session's CSRF token and a
  same-origin Origin header. Static app code is public; data is not.
- Settings, FAQ, calls, corrections, summaries and the notification outbox live in SQLite and survive restarts.
- The browser conversation lab is served at /lab/ behind the same login and uses the same ReceptionService.
- Nothing here connects the company line or sends a real notification: the page says so on every screen.
"""
from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import json
import os
import pathlib
import re
import socket
import sys
import threading
import traceback
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from ..browser_lab import server as labsrv
from ..engines.budget import DEFAULT_LEDGER
from ..reception import settings as S
from ..reception.fake_agent import RuleAgent
from ..reception.notify import CHANNELS, SIMULATE, STATUS_LABELS
from ..reception.service import ReceptionService
from ..reception.store import DEFAULT_DB, Store, now_jst
from ..reception.summary import build_summary, notification_body
from .auth import COOKIE, SESSION_S, Auth

STATIC = pathlib.Path(__file__).resolve().parent / "static"
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
DEMO_SCRIPT = ["焼肉ほのか博多店の田中です。無煙ロースター8台の清掃をお願いしたいです。", "点検は無料ですか？",
               "折り返しは090-1234-5678です。", "はい、違います。090-1234-5679です。", "はい、合っています。",
               "ダクトの点検もお願いします。", "10月8日の午後3時でお願いします。",
               "すみません、9日の金曜日に変更してください。", "はい、それでお願いします。"]


class AdminApp:
    def __init__(self, store: Store, env: dict | None = None, clock=now_jst,
                 ledger_path: pathlib.Path = DEFAULT_LEDGER, results_dir: pathlib.Path = labsrv.RESULTS) -> None:
        self.store = store
        self.env = dict(os.environ if env is None else env)
        self.service = ReceptionService(store, env=self.env, clock=clock)
        self.auth = Auth(store)
        self.lab = labsrv.Lab(env=self.env, ledger_path=ledger_path, results_dir=results_dir, service=self.service)
        self.agents: dict[str, RuleAgent] = {}
        self.lock = threading.Lock()

    def agent(self, call_id: str) -> RuleAgent:
        with self.lock:
            if call_id not in self.agents:
                self.agents[call_id] = RuleAgent(self.service, call_id)
            return self.agents[call_id]

    # --- GET -------------------------------------------------------------------------------------------
    def get(self, path: str, query: dict, user: dict) -> dict:
        svc = self.service
        if path == "/api/me":
            return {"user": user["user"], "role": user["role"], "csrf": user["csrf"]}
        if path == "/api/status":
            return svc.status()
        if path == "/api/config":
            vid, cfg, meta = svc.config()
            return {"version": vid, "config": cfg, "meta": meta, "history": svc.config_history(),
                    "labels": {"modes": S.MODE_LABELS, "routes": S.ROUTE_LABELS, "weekdays": S.WEEKDAY_LABELS,
                               "fields": S.FIELD_LABELS, "failure_actions": {"normal": "普通受電として担当者へ",
                                                                            "dtmf": "録音の案内とプッシュボタン"}},
                    "hours_answer": S.hours_answer(cfg)}
        if path == "/api/route/check":
            return svc.check_route(query.get("at"), query.get("ai_available", "1") != "0")
        if path == "/api/faqs":
            return {"faqs": svc.faqs()}
        if path == "/api/voices":
            vid, cfg, _ = svc.config()
            return {"catalog": svc.voice_catalog(), "voices": cfg["voices"], "active_voice": cfg["active_voice"]}
        if path == "/api/calls":
            return {"calls": svc.list_calls()}
        m = re.fullmatch(r"/api/calls/([\w-]+)", path)
        if m:
            return svc.call_view(m.group(1))
        m = re.fullmatch(r"/api/calls/([\w-]+)/preview", path)
        if m:
            return self.preview(m.group(1))
        if path == "/api/targets":
            return {"targets": svc.outbox.targets(), "channels": CHANNELS, "simulate": SIMULATE,
                    "problems": svc.outbox.problems()}
        if path == "/api/notifications":
            rows = self.store.q("SELECT n.id, n.call_id, n.version, n.kind, n.status, n.attempts, n.last_error, "
                                "n.updated_at, t.name AS target_name FROM notifications n JOIN notify_targets t "
                                "ON t.id = n.target_id ORDER BY n.id DESC LIMIT 100")
            for r in rows:
                r["status_label"] = STATUS_LABELS.get(r["status"], r["status"])
            return {"notifications": rows}
        if path == "/api/audit":
            return {"audit": self.store.q("SELECT * FROM audit_log ORDER BY id DESC LIMIT 200")}
        if path == "/api/demo/script":
            return {"script": DEMO_SCRIPT}
        raise LookupError(path)

    def preview(self, call_id: str) -> dict:
        """What each enabled target would receive for this call now (no sending, no outbox row)."""
        svc = self.service
        view = svc.call_view(call_id)
        snap = svc.snapshot(call_id)
        kinds = view["notify_kinds"] + ([f"emergency_{view['emergency']}"] if view["emergency"] else [])
        if view["summaries"]:
            latest = view["summaries"][-1]
            summ = {"items": latest["items"]}
            version = latest["version"]
        else:   # call still open: preview from the current fields
            fields = {f["name"]: {"value": f["value"], "status": f["status"], "source": f["source"]}
                      for f in view["fields"] if f["value"]}
            summ = build_summary(fields, snap["config"], view["open_questions"], kinds)
            version = None
        out = []
        for t in svc.outbox.targets(enabled_only=True):
            out.append({"target": t["name"], "channel_label": t["channel_label"], "simulate_label": t["simulate_label"],
                        "body": notification_body(summ, call_id, kinds, view["open_questions"])})
        return {"call_id": call_id, "summary_version": version, "previews": out,
                "note": "プレビューは送信しません。訂正の版では、冒頭に訂正の表示が付きます。"}

    # --- POST ------------------------------------------------------------------------------------------
    def post(self, path: str, body: dict, user: dict) -> dict:
        svc, who = self.service, user["user"]
        if path == "/api/config":
            vid = svc.save_config(body.get("config") or {}, who, str(body.get("note", "")))
            return {"version": vid, "note": "保存しました。次の着信から使われます（通話中の着信には影響しません）"}
        if path == "/api/faqs":
            return {"id": svc.save_faq(body, who)}
        m = re.fullmatch(r"/api/faqs/(\d+)", path)
        if m:
            return {"id": svc.save_faq(body, who, int(m.group(1)))}
        m = re.fullmatch(r"/api/calls/([\w-]+)/correct", path)
        if m:
            return svc.correct_field(m.group(1), body.get("field", ""), body.get("value", ""), who,
                                     body.get("reason", ""))
        m = re.fullmatch(r"/api/calls/([\w-]+)/status", path)
        if m:
            svc.set_handled(m.group(1), body.get("status", ""), who)
            return {"ok": True}
        m = re.fullmatch(r"/api/calls/([\w-]+)/renotify", path)
        if m:
            return svc.renotify(m.group(1), who)
        if path == "/api/targets":
            return {"id": svc.outbox.save_target(body, who)}
        m = re.fullmatch(r"/api/targets/(\d+)", path)
        if m:
            return {"id": svc.outbox.save_target(body, who, int(m.group(1)))}
        if path == "/api/outbox/process":
            self.store.audit(who, "outbox_process", "", {"force_due": bool(body.get("force_due"))})
            return svc.outbox.process(force_due=bool(body.get("force_due")))
        m = re.fullmatch(r"/api/notifications/(\d+)/(resend|discard)", path)
        if m:
            nid = int(m.group(1))
            if m.group(2) == "resend":
                return {"new_id": svc.outbox.resend(nid, who)}
            svc.outbox.discard(nid, who)
            return {"ok": True}
        m = re.fullmatch(r"/api/channels/(\w+)/resume", path)
        if m:
            svc.outbox.resume_channel(m.group(1), who)
            return {"ok": True}
        # --- demo calls (fictitious; the company line is not connected) ---
        if path == "/api/demo/start":
            caller = None if body.get("withheld") else (body.get("caller_id") or None)
            view = svc.start_call(dialed=str(body.get("dialed") or "不明"), caller_id=caller, at=body.get("at") or None,
                                  ai_available=body.get("ai_available", True) is not False, source="demo")
            self.store.audit(who, "demo_call", view["id"], {"route": view["route"]})
            say = self.agent(view["id"]).greeting() if view["route"] == "ai" else []
            return {"call": svc.call_view(view["id"]), "say": say}
        if path == "/api/demo/say":
            cid = body.get("call_id", "")
            res = self.agent(cid).hear(str(body.get("text", ""))[:500])
            return {"result": res, "call": svc.call_view(cid)}
        if path == "/api/demo/consent":
            cid = body.get("call_id", "")
            res = svc.consent_event(cid, body.get("kind", ""), body.get("value"))
            return {"actions": res["actions"], "call": res["call"]}
        if path == "/api/demo/dtmf":
            res = svc.dtmf(body.get("call_id", ""), body.get("digits") or None)
            if res["call"]["ended_at"]:
                svc.outbox.process()
            return {"actions": res["actions"], "call": svc.call_view(body.get("call_id", ""))}
        if path == "/api/demo/end":
            cid = body.get("call_id", "")
            svc.end_call(cid, "テストで終了")
            processed = svc.outbox.process()
            return {"call": svc.call_view(cid), "processed": processed}
        raise LookupError(path)


def lan_addresses() -> set[str]:
    out = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            out.add(info[4][0])
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))   # no packet is sent; this only picks the outgoing interface
            out.add(s.getsockname()[0])
    except OSError:
        pass
    return {a for a in out if not ipaddress.ip_address(a.split("%")[0]).is_loopback}


def make_handler(app: AdminApp, allowed_hosts: set[str], log_requests: bool = True):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ConciergeAdmin/1"
        sys_version = ""

        def log_message(self, fmt, *args):   # method, path without the query, status; never headers or bodies
            pass

        def log_request(self, code="-", size="-"):
            if log_requests:
                sys.stderr.write(f"{dt.datetime.now():%H:%M:%S} {self.command} {urlsplit(self.path).path} {code}\n")

        # --- helpers ---
        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            return host in allowed_hosts

        def _origin_ok(self) -> bool:
            origin = self.headers.get("Origin")
            return origin is None or origin in (f"http://{self.headers.get('Host')}", f"https://{self.headers.get('Host')}")

        def _token(self) -> str | None:
            c = SimpleCookie(self.headers.get("Cookie") or "")
            return c[COOKIE].value if COOKIE in c else None

        def _send(self, status: int, data: bytes, ctype: str, extra: dict | None = None, page: bool = False) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            if page:
                self.send_header("Content-Security-Policy", CSP)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _json(self, status: int, obj, extra: dict | None = None) -> None:
            self._send(status, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8", extra)

        def _redirect(self, to: str) -> None:
            self._send(HTTPStatus.FOUND, b"", "text/plain", {"Location": to})

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 2_000_000:
                raise ValueError("request too large")
            data = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("JSON object expected")
            return data

        def _file(self, base: pathlib.Path, rel: str, page: bool = False) -> None:
            target = (base / rel).resolve()
            if not target.is_file() or base not in target.parents:
                return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                     ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}.get(target.suffix,
                                                                                   "application/octet-stream")
            self._send(200, target.read_bytes(), ctype, page=page)

        def _guard(self, fn):
            try:
                return fn()
            except LookupError:
                return self._json(HTTPStatus.NOT_FOUND, {"error": "見つかりません"})
            except PermissionError as exc:
                return self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": str(exc)})
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception:   # the trace goes to the console only; the page gets a generic message
                traceback.print_exc()
                return self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "内部エラー（サーバーのコンソールを確認）"})

        # --- verbs ---
        def do_GET(self):
            if not self._host_ok():
                return self._json(HTTPStatus.FORBIDDEN, {"error": "host not allowed"})
            url = urlsplit(self.path)
            path, query = url.path, {k: v[0] for k, v in parse_qs(url.query).items()}
            if path == "/login":
                return self._file(STATIC, "login.html", page=True)
            if path.startswith("/static/"):
                return self._file(STATIC, path[len("/static/"):])
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            user = app.auth.session(self._token())
            if path.startswith("/api/"):
                if not user:
                    return self._json(HTTPStatus.UNAUTHORIZED, {"error": "ログインが必要です"})
                return self._guard(lambda: self._json(200, app.get(path, query, user)))
            if path in ("/", "/index.html"):
                return self._file(STATIC, "index.html", page=True) if user else self._redirect("/login")
            if path == "/lab":
                return self._redirect("/lab/")
            if path.startswith("/lab/"):
                if not user:
                    return self._redirect("/login?next=/lab/")
                sub = path[len("/lab"):]
                if sub.startswith("/api/"):
                    got = labsrv.api_get(app.lab, sub, user["csrf"])
                    return self._json(200, got) if got is not None else self._json(404, {"error": "not found"})
                found = labsrv.static_file(sub)
                if found is None:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return self._send(200, found[0], found[1])
            return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        do_HEAD = do_GET

        def do_POST(self):
            if not self._host_ok():
                return self._json(HTTPStatus.FORBIDDEN, {"error": "host not allowed"})
            if not self._origin_ok():
                return self._json(HTTPStatus.FORBIDDEN, {"error": "別のサイトからの要求は受け付けません"})
            path = urlsplit(self.path).path
            if path == "/api/login":
                def login():
                    body = self._body()
                    got = app.auth.login(str(body.get("username", "")), str(body.get("password", "")))
                    if not got:
                        return self._json(HTTPStatus.UNAUTHORIZED, {"error": "ユーザー名かパスワードが違います"})
                    token, csrf = got
                    cookie = f"{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_S}"
                    return self._json(200, {"ok": True, "csrf": csrf}, {"Set-Cookie": cookie})
                return self._guard(login)
            user = app.auth.session(self._token())
            if not user:
                return self._json(HTTPStatus.UNAUTHORIZED, {"error": "ログインが必要です"})
            if self.headers.get("X-CSRF-Token") != user["csrf"]:
                return self._json(HTTPStatus.FORBIDDEN, {"error": "CSRFトークンが一致しません。画面を読み込み直してください"})
            if path == "/api/logout":
                app.auth.logout(self._token())
                return self._json(200, {"ok": True}, {"Set-Cookie": f"{COOKIE}=; HttpOnly; SameSite=Strict; Path=/; "
                                                                    "Max-Age=0"})
            if path.startswith("/lab/api/"):
                def lab_post():
                    status, payload = labsrv.api_post(app.lab, path[len("/lab"):], self._body())
                    return self._json(status, payload)
                return self._guard(lab_post)
            return self._guard(lambda: self._json(200, app.post(path, self._body(), user)))

    return Handler


def serve(app: AdminApp, host: str = "127.0.0.1", port: int = 8780,
          extra_hosts: set[str] | None = None, log_requests: bool = True) -> ThreadingHTTPServer:
    allowed = {"127.0.0.1", "localhost", "::1"} | set(extra_hosts or ())
    if host not in ("127.0.0.1", "localhost", "::1"):
        allowed |= lan_addresses()
    return ThreadingHTTPServer((host, port), make_handler(app, allowed, log_requests))


def main() -> int:
    ap = argparse.ArgumentParser(description="AI reception admin app (local prototype)")
    ap.add_argument("--port", type=int, default=8780)
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to try from a smartphone on the same Wi-Fi")
    ap.add_argument("--allow-host", action="append", default=[], help="extra Host name to accept")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--results-dir", default=str(labsrv.RESULTS))
    ap.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    args = ap.parse_args()
    app = AdminApp(Store(args.db), ledger_path=pathlib.Path(args.ledger), results_dir=pathlib.Path(args.results_dir))
    generated = app.auth.ensure_admin(app.env.get("ADMIN_PASSWORD"))
    httpd = serve(app, args.host, args.port, set(args.allow_host))
    app.lab.start_watchdog()   # expires browser-lab sessions past the maximum length
    port = httpd.server_address[1]
    print(f"admin app: http://127.0.0.1:{port}  (Ctrl+C to stop)", flush=True)
    print("デモ・試作：会社の回線には接続していません。LINE等の実際の通知も送りません。", flush=True)
    if generated:
        print(f"初回のログイン：ユーザー名 admin ／ パスワード {generated}  （この表示は1回だけ。控えてください）", flush=True)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        addrs = ", ".join(f"http://{a}:{port}" for a in sorted(lan_addresses())) or "（このPCのIPアドレス）"
        print(f"注意：同じネットワークの他の端末から開けます：{addrs}。HTTPSではないため、自宅・事務所のWi-Fiの中だけで、"
              "架空のデータで使ってください。", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
