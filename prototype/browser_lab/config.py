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

MAX_SESSION_MIN = 4.0        # the page hangs up automatically at this length
TOOL_DELAY_MS_C5 = 4000      # C5: business processing deliberately slowed down

CANDIDATES = {
    "fake": {
        "name": "オフラインの模擬（業者に接続しない・画面と計測の確認用）", "adapter": "fake", "env": [],
        "vendor": None, "upper_usd_per_min": 0.0, "increment_s": 1, "verified": True,
        "logic_path": "client_tools"},
    "gpt-live-1": {
        "name": "OpenAI GPT-Live 1", "adapter": "gpt-live", "env": ["OPENAI_API_KEY"], "vendor": "openai_lab",
        "model": "gpt-live-1", "upper_usd_per_min": 0.06, "increment_s": 1, "verified": False,
        "logic_path": "delegation_responses",
        "delegation_model_env": "GPT_LIVE_DELEGATION_MODEL", "delegation_model_default": "gpt-6-luna",
        "note": "業務処理は delegation（Responses）の関数呼び出しで共通の処理へ。バックエンドのモデルは別料金（記号 L_live）で、"
                "環境変数 GPT_LIVE_DELEGATION_MODEL で指定（既定は公式ガイドの例 gpt-6-luna）"},
    "gemini-3.8-live": {
        "name": "Google Gemini 3.8 Live", "adapter": "gemini-live", "env": ["GEMINI_API_KEY"], "vendor": "google_lab",
        "model": "models/gemini-3.8-live", "upper_usd_per_min": 0.06, "increment_s": 60, "verified": False,
        "logic_path": "function_call"},
    "elevenagents": {
        "name": "ElevenLabs ElevenAgents（Expressive mode）", "adapter": "elevenlabs",
        "env": ["ELEVENLABS_API_KEY", "ELEVENLABS_AGENT_ID"], "vendor": "elevenlabs_lab",
        "upper_usd_per_min": 0.12, "increment_s": 60, "verified": False, "logic_path": "client_tools",
        "note": "エージェントと client tools（5つ）はElevenLabsの画面で作る（docs/browser-lab-setup.md）"},
    "cartesia-agents": {
        "name": "Cartesia Managed Agents", "adapter": "cartesia", "env": ["CARTESIA_API_KEY", "CARTESIA_AGENT_ID"],
        "vendor": "cartesia_lab", "upper_usd_per_min": 0.10, "increment_s": 60, "verified": False,
        "cartesia_version": "2026-08-14", "logic_path": "client_tools",
        "note": "エージェントと Client function（5つ）はCartesiaの画面で作る。新しいWebSocket（/v1/agents/websocket）で接続"},
    "gpt-realtime-2.1": {
        "name": "OpenAI GPT-Realtime-2.1（比較の基準）", "adapter": "openai-realtime", "env": ["OPENAI_API_KEY"],
        "vendor": "openai_lab", "model": "gpt-realtime-2.1", "voice": "marin", "upper_usd_per_min": 0.30,
        "increment_s": 60, "verified": False, "logic_path": "function_call"},
}
LOGIC_PATHS = {"client_tools": "ブラウザーのclient tools → この端末のサーバー",
               "delegation_responses": "delegation（Responses）の関数呼び出し → ブラウザー → この端末のサーバー",
               "function_call": "関数呼び出し → ブラウザー → この端末のサーバー"}

# Proposed (not approved) run-side caps per vendor ledger, by stage (measurement plan v2.1, 5章).
# The ledger is cumulative, so the 'detailed' caps include what the connection check may have used.
# LAB_STAGE=connection (default) allows only the first 1-2 short sessions per candidate; the detailed comparison
# (C1-C6 twice) needs LAB_STAGE=detailed, set by the tester after the connection worked.
SESSIONS = {"connection": {"gpt-live-1": 2, "gemini-3.8-live": 2, "elevenagents": 2, "cartesia-agents": 2,
                           "gpt-realtime-2.1": 1},
            "detailed": {"gpt-live-1": 12, "gemini-3.8-live": 12, "elevenagents": 12, "cartesia-agents": 12,
                         "gpt-realtime-2.1": 6}}
LAB_LIMITS_BY_STAGE = {
    "connection": {
        "openai_lab": Limits(max_requests=3, max_audio_seconds=12 * 60, max_cost_usd=1.8),
        "google_lab": Limits(max_requests=2, max_audio_seconds=8 * 60, max_cost_usd=0.5),
        "elevenlabs_lab": Limits(max_requests=2, max_audio_seconds=8 * 60, max_cost_usd=1.0),
        "cartesia_lab": Limits(max_requests=2, max_audio_seconds=8 * 60, max_cost_usd=0.8),
    },
    "detailed": {
        "openai_lab": Limits(max_requests=30, max_audio_seconds=90 * 60, max_cost_usd=12.0),
        "google_lab": Limits(max_requests=20, max_audio_seconds=60 * 60, max_cost_usd=4.0),
        "elevenlabs_lab": Limits(max_requests=20, max_audio_seconds=60 * 60, max_cost_usd=7.0),
        "cartesia_lab": Limits(max_requests=20, max_audio_seconds=60 * 60, max_cost_usd=6.0),
    },
}
LAB_STAGES = {"connection": "接続の予備試験（各候補1〜2回）", "detailed": "詳しい会話比較（接続できた候補だけ）"}
LAB_LIMITS = LAB_LIMITS_BY_STAGE["detailed"]   # overall caps (kept for the budget table and older callers)

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


def public_candidates(env: dict) -> list[dict]:
    """Candidate list for the page: never includes key values, only whether they are present."""
    out = []
    for cid, c in CANDIDATES.items():
        missing = [k for k in c["env"] if not env.get(k)]
        out.append({"id": cid, "name": c["name"], "adapter": c["adapter"], "ready": not missing,
                    "missing_env": missing, "verified": c["verified"], "note": c.get("note", ""),
                    "logic_path": LOGIC_PATHS[c["logic_path"]],
                    "max_session_min": MAX_SESSION_MIN, "upper_usd_per_min": c["upper_usd_per_min"]})
    return out
