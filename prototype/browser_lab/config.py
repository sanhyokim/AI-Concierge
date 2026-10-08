"""Browser conversation lab: candidates, spend caps and the shared reception task.

Every candidate gets the same reception task and tools. The business logic behind the tools is the common
ReceptionService (FAQ from the admin settings frozen at session start, the confirmation guard, the intake
record). Keys are read only from environment variables on the machine that runs the lab and never sent to the
browser.
"logic_path" says how a candidate's tool calls reach that logic; every vendor path is written from the public
docs and has not been confirmed on a real account. A session's business-logic result counts as evaluated
only if the server actually received tool calls in it.
The per-minute "upper" price is what a session reserves in the ledger (max session length x upper);
it is a proposal for the browser stage (measurement plan v2), not an approved budget.
"""
from __future__ import annotations

from ..reception.tooldefs import RECEPTION_TOOLS as TOOLS
from ..engines.budget import Limits

MAX_SESSION_MIN = 5.0        # test calls stop at this length (user, 2026-10-08); for real calls it is undecided
TOOL_DELAY_MS_C5 = 4000      # C5: business processing deliberately slowed down

HOLD_BY_USER = "利用者の判断で保留（費用と性能のバランス。2026-10-07）。採用の候補からは外していない"

CANDIDATES = {
    "fake": {
        "name": "オフラインの模擬（業者に接続しない・画面と計測の確認用）", "adapter": "fake", "env": [],
        "vendor": None, "upper_usd_per_min": 0.0, "increment_s": 1, "verified": True,
        "logic_path": "client_tools", "applies": {"instructions": "n/a", "voice": "n/a"}},
    "gpt-live-1": {
        "name": "OpenAI GPT-Live 1", "adapter": "gpt-live", "env": ["OPENAI_API_KEY"], "vendor": "openai_lab",
        "model": "gpt-live-1", "upper_usd_per_min": 0.06, "increment_s": 1, "verified": False,
        "logic_path": "delegation_responses", "applies": {"instructions": "session", "voice": "session"},
        "delegation_model_env": "GPT_LIVE_DELEGATION_MODEL", "delegation_model_default": "gpt-6-luna",
        "note": "日本語の対応は公式に明記がない（最初に確かめる）。業務処理は delegation（Responses）の裏方のモデルへ。"
                "裏方のモデルは別料金で、GPT_LIVE_DELEGATION_MODEL で指定（既定 gpt-6-luna）"},
    "gemini-3.8-live": {
        "name": "Google Gemini 3.8 Live", "adapter": "gemini-live", "env": ["GEMINI_API_KEY"], "vendor": "google_lab",
        "model": "models/gemini-3.8-live", "upper_usd_per_min": 0.06, "increment_s": 60, "verified": False,
        "logic_path": "function_call", "applies": {"instructions": "session", "voice": "session"}},
    "elevenagents": {
        "name": "ElevenLabs ElevenAgents（Expressive mode）", "adapter": "elevenlabs",
        "env": ["ELEVENLABS_API_KEY", "ELEVENLABS_AGENT_ID"], "vendor": "elevenlabs_lab",
        "upper_usd_per_min": 0.12, "increment_s": 60, "verified": False, "logic_path": "client_tools",
        "applies": {"instructions": "override", "voice": "override"}, "hold": HOLD_BY_USER,
        "note": "エージェントと client tools（5つ）はElevenLabsの画面で作る（docs/browser-lab-setup.md）"},
    "cartesia-agents": {
        "name": "Cartesia Managed Agents", "adapter": "cartesia", "env": ["CARTESIA_API_KEY", "CARTESIA_AGENT_ID"],
        "vendor": "cartesia_lab", "upper_usd_per_min": 0.10, "increment_s": 60, "verified": False,
        "cartesia_version": "2026-08-14", "logic_path": "client_tools",
        "applies": {"instructions": "agent_fixed", "voice": "agent_fixed"}, "hold": HOLD_BY_USER,
        "note": "エージェントと Client function（5つ）はCartesiaの画面で作る。新しいWebSocket（/v1/agents/websocket）で接続"},
    "gpt-realtime-2.1": {
        "name": "OpenAI GPT-Realtime-2.1（比較の基準）", "adapter": "openai-realtime", "env": ["OPENAI_API_KEY"],
        "vendor": "openai_lab", "model": "gpt-realtime-2.1", "voice": "marin", "upper_usd_per_min": 0.30,
        "increment_s": 60, "verified": False, "logic_path": "function_call",
        "applies": {"instructions": "session", "voice": "session"}, "hold": HOLD_BY_USER},
}
LOGIC_PATHS = {"client_tools": "ブラウザーのclient tools → この端末のサーバー",
               "delegation_responses": "delegation（Responses）の関数呼び出し → ブラウザー → この端末のサーバー",
               "function_call": "関数呼び出し → ブラウザー → この端末のサーバー"}
APPLY_LABELS = {"session": "接続のときに指定する（公式資料のとおり。実接続では未確認）",
                "override": "業者の画面で上書きを許可し、ELEVENLABS_OVERRIDES に書いたときだけ反映",
                "agent_fixed": "業者のエージェント設定で固定（この画面からは変えられない）",
                "n/a": "対象外（模擬）"}

# GPT-Live: the backend (delegation) model is billed separately. Only models with a confirmed price can run.
# USD per 1M tokens (input, cached input, output), official pricing page read 2026-10-07.
DELEGATION_PRICES = {"gpt-6-luna": (0.10, 0.01, 0.50), "gpt-6-sol": (2.00, 0.20, 10.00),
                     "gpt-5.6-luna": (0.20, 0.02, 1.20), "gpt-5.6-terra": (2.00, 0.20, 12.00)}
# In-app limits per session. They stop the session; they do not cap what the vendor bills.
MAX_TOOL_CALLS = 40            # business-logic tool calls
MAX_BACKEND_RESPONSES = 60     # GPT-Live backend responses (counted from response.event)
MAX_RESPONSE_CREATES = 40      # response.create sent by the page after function outputs
# Assumptions for the backend cost of one session. The context the backend receives is not documented:
# input = instructions (up to 16,384 tokens) + conversation history, so neither bound is guaranteed.
BACKEND_TYPICAL = {"responses": 10, "input_tokens": 6_000, "output_tokens": 300}
BACKEND_STRICT = {"responses": MAX_BACKEND_RESPONSES, "input_tokens": 32_000, "output_tokens": 1_200}
SESSION_GRACE_S = 30           # the server expires a session this long after MAX_SESSION_MIN
MAX_MINT_FAILURES = 2          # per candidate and stage; a person resets it (--reset-mint-failures)


def backend_usd(model: str, assumption: dict) -> float:
    pin, _, pout = DELEGATION_PRICES[model]
    n = assumption["responses"]
    return n * (assumption["input_tokens"] * pin + assumption["output_tokens"] * pout) / 1_000_000


def session_reserve_usd(cfg: dict, env: dict) -> float:
    """What one session holds in the ledger: voice at the upper price for the maximum length, plus (GPT-Live)
    the backend under the strict assumption."""
    usd = MAX_SESSION_MIN * cfg["upper_usd_per_min"]
    if cfg.get("delegation_model_env"):
        usd += backend_usd(delegation_model(cfg, env), BACKEND_STRICT)
    return round(usd, 6)


def delegation_model(cfg: dict, env: dict) -> str:
    return env.get(cfg["delegation_model_env"]) or cfg["delegation_model_default"]


# Sessions allowed per candidate and stage. None = no count limit: the user removed the limits for the two tested
# candidates (2026-10-08); sessions are still counted for the record. Held candidates get none until released.
SESSIONS = {"connection": {"gpt-live-1": None, "gemini-3.8-live": None, "elevenagents": 0, "cartesia-agents": 0,
                           "gpt-realtime-2.1": 0},
            "detailed": {"gpt-live-1": None, "gemini-3.8-live": None, "elevenagents": 0, "cartesia-agents": 0,
                         "gpt-realtime-2.1": 0}}
_NO_CAP = Limits(max_requests=None, max_audio_seconds=None, max_cost_usd=None)   # user decision 2026-10-08
_HELD = Limits(max_requests=0, max_audio_seconds=0, max_cost_usd=0.0)
LAB_LIMITS_BY_STAGE = {   # the ledger still records every session and its reservation; it no longer refuses
    stage: {"openai_lab": _NO_CAP, "google_lab": _NO_CAP, "elevenlabs_lab": _HELD, "cartesia_lab": _HELD}
    for stage in ("connection", "detailed")
}
LAB_STAGES = {"connection": "接続の予備試験", "detailed": "詳しい会話比較"}
LAB_LIMITS = LAB_LIMITS_BY_STAGE["detailed"]   # overall caps (kept for the budget table and older callers)

ENV_SETUP = {   # shown on the lab page: how to set a variable on the tester's PC (never paste a key into chat)
    "OPENAI_API_KEY": "OpenAIの管理画面（Platform）で作るAPIキー。試験専用のプロジェクトで作る",
    "GEMINI_API_KEY": "Google AI Studio で作るAPIキー",
    "GPT_LIVE_DELEGATION_MODEL": "任意。GPT-Liveの裏方のモデル（既定 gpt-6-luna）",
}

MOCK_FAQ = """\
【模擬FAQ（未承認の推奨案。試験専用）】
- 点検は無料です。追加の費用は担当者が確認します。
- 無煙ロースターの清掃は、1台あたり2万5千円から3万5千円（税別）が目安です。台数や汚れ具合で変わり、正式な金額は無料の現地調査のあとにお見積もりします。
- フードやダクトの清掃は、現地を調査したうえでお見積もりします。
- 担当者がお電話をお受けする時間は、9時から17時です。
- 清掃の作業は、深夜や早朝にも対応しています。日時は担当者から折り返してご相談します。
- 福岡を中心に、九州・中国・四国で対応しています。
- FAQにないことは「担当者が確認してご連絡します」と答え、確認事項として記録します。
"""

LAB_RULES = """\
あなたは株式会社野田（飲食店の排煙ダクト・厨房換気設備の清掃と保守）の電話受付AIです。これは試験です。
- 冒頭で「AIによる受付」であることを短く伝えます。丁寧で自然な敬語で、一度に一つだけ質問します。
- お客様が話し始めたら、すぐに話すのをやめて聞きます。「はい」「うん」などの短い相づちだけなら、話を続けます。
  「違います」「ちょっと待って」「訂正します」には、すぐに応じます。言い終えていない内容を、伝えたことにしません。
- 既に聞いた情報は聞き直しません。分からない項目は無理に聞き続けません。
- 料金・条件は下の模擬FAQの範囲だけで答え、予約や訪問を確定・約束しません。担当者へのライブ転送はできないので、折り返しを案内します。
- 電話番号・日時は、区切りごとにはっきり復唱し、合っているかを確認します。「はい、違います」は肯定ではありません。
- ツールがある場合：聞き取った値は save_field、復唱の前に request_readback、明確に肯定されたら confirm_field を呼びます。
  処理を待つ間もお客様の話を聞き、追加や訂正を受け付けます。結果が出る前に約束しません。
"""
LAB_INSTRUCTIONS = LAB_RULES + "\n" + MOCK_FAQ   # used only when no reception service is attached

GREETING = "お電話ありがとうございます。株式会社野田のAI受付です。ご用件をお伺いします。"


def openai_tools() -> list:
    return TOOLS


def gemini_function_declarations() -> list:
    return [{"name": t["name"], "description": t["description"], "parameters": t["parameters"]} for t in TOOLS]


def released(env: dict) -> set[str]:
    return {c.strip() for c in (env.get("LAB_RELEASE_HOLD") or "").split(",") if c.strip()}


def apply_modes(candidate: str, env: dict) -> dict:
    """How the admin settings reach this candidate on this machine: session | override | agent_fixed | n/a.
    ElevenLabs overrides apply only for the fields listed in ELEVENLABS_OVERRIDES (prompt, voice), which must
    also be allowed in the agent's Security tab."""
    cfg = CANDIDATES[candidate]
    over = {x.strip() for x in (env.get("ELEVENLABS_OVERRIDES") or "").split(",") if x.strip()}
    out = {}
    for kind, key in (("instructions", "prompt"), ("voice", "voice")):
        mode = cfg["applies"][kind]
        out[kind] = ("override" if key in over else "agent_fixed") if mode == "override" else mode
    return out


def voice_applies(candidate: str, env: dict) -> bool:
    """Whether a voice chosen in the admin app is actually sent to this candidate."""
    return candidate in CANDIDATES and apply_modes(candidate, env)["voice"] in ("session", "override", "n/a")


def public_candidates(env: dict) -> list[dict]:
    """Candidate list for the page: never includes key values, only whether they are present."""
    out = []
    for cid, c in CANDIDATES.items():
        missing = [k for k in c["env"] if not env.get(k)]
        hold = c.get("hold") if cid not in released(env) else None
        out.append({"id": cid, "name": c["name"], "adapter": c["adapter"], "ready": not missing and not hold,
                    "missing_env": missing, "hold": hold, "verified": c["verified"], "note": c.get("note", ""),
                    "logic_path": LOGIC_PATHS[c["logic_path"]],
                    "applies": {k: APPLY_LABELS[v] for k, v in apply_modes(cid, env).items()},
                    "env_help": {k: ENV_SETUP.get(k, "") for k in c["env"]},
                    "max_session_min": MAX_SESSION_MIN, "upper_usd_per_min": c["upper_usd_per_min"]})
    return out
