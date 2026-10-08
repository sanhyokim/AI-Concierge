"""Reception settings: provisional defaults, validation, FAQ seeds and the voice catalog.

Every default here is PROVISIONAL and FICTITIOUS (暫定・架空). FAQ wording and voices are still the user's
decisions (spec 4-1); nothing here is a decided setting.

There is no reception mode or schedule: the AI is switched on and off by changing the NTT call forwarding (line
plan 2, user decision 2026-10-07/08). Every call that reaches this app is answered by the AI; only what happens when
the AI cannot be used is a setting here.
"""
from __future__ import annotations

import copy

ROUTE_LABELS = {"ai": "AI受付", "forward": "担当者の携帯へ転送（AIが止まったとき）",
                "dtmf": "録音の案内とプッシュボタン（AIなし）"}
FAILURE_ACTIONS = ("forward", "dtmf")
FAILURE_LABELS = {"forward": "担当者の携帯へ転送する（転送先へ1回分の通話料がかかる）",
                  "dtmf": "録音の案内を流し、プッシュボタンで折り返し先を受け付ける"}
FIELD_KEYS = ("shop_name", "caller_name", "callback_number", "request", "preferred_datetime")
FIELD_LABELS = {"shop_name": "店舗名", "caller_name": "お名前", "callback_number": "折り返し先",
                "request": "用件", "preferred_datetime": "希望日時"}

DEFAULT_CONFIG = {
    "provisional": True,
    "ai_failure_action": "forward",
    "voices": [   # the first connection check uses GPT-Live and Gemini only (user decision 2026-10-07)
        {"id": "v1", "candidate": "gpt-live-1", "voice": "marin", "label": "GPT-Liveの既定の声（日本語は未確認）"},
        {"id": "v2", "candidate": "gemini-3.8-live", "voice": "Kore", "label": "Geminiの声（候補・日本語は未確認）"},
        {"id": "v3", "candidate": "fake", "voice": "tone", "label": "オフラインの模擬（音声の評価には使えない）"},
    ],
    "active_voice": "v1",
    "fields": {k: {"store": True, "summary": True, "notify": True} for k in FIELD_KEYS},
    "store_transcript": True,
    "retention_days": None,
}

# Seeds: the FAQ recommendations of spec 4-1 (NOT approved).
HOURS_ANSWER = ("担当者がお電話をお受けする時間は、9時から17時です。担当者が出られないときは、AIがご用件を伺い、"
                "担当者から折り返しご連絡します。")   # provisional; it used to be generated from the schedule
FAQ_SEEDS = [
    ("FAQ-01", "点検は無料ですか", "点検は無料です。", "点検,無料,タダ"),
    ("FAQ-02", "無煙ロースターの清掃はいくらですか",
     "無煙ロースターの清掃は、1台あたり2万5千円から3万5千円（税別）が目安です。台数や汚れ具合、ダクトの長さなどで変わります。"
     "正式な金額は、無料の現地調査のあとにお見積もりします。", "ロースター,無煙"),
    ("FAQ-03", "フードやダクトの清掃はいくらですか",
     "クリアフード、焼き場のフード、ダクトの清掃は、現地を調査したうえでお見積もりします。", "フード,ダクト,見積"),
    ("FAQ-04", "電話は何時まで受け付けていますか", HOURS_ANSWER, "何時,受付時間,営業時間,電話の受付"),
    ("FAQ-05", "夜中や早朝に作業してもらえますか",
     "清掃の作業は、深夜や早朝にも対応しています。ご希望の日時を伺って、担当者から折り返しご相談します。", "夜中,深夜,早朝,夜間"),
    ("FAQ-06", "どの地域に来てもらえますか", "福岡を中心に、九州・中国・四国で対応しています。", "地域,エリア,どこまで,対応地域"),
    ("FAQ-07", "どんな流れで進みますか",
     "お問い合わせのあと、現場の調査、打ち合わせ、清掃作業の順に進めます。作業のあとは、写真付きの報告書をお渡しします。", "流れ,手順,進め方"),
    ("FAQ-08", "どんなことを頼めますか",
     "飲食店の排煙ダクトや厨房の換気設備の設計・施工・保守と、ダクト・クリアフード・無煙ロースターの清掃、"
     "関連する機器の販売・取り付け・交換を行っています。", "頼め,どんなこと,業務内容,サービス"),
    ("FAQ-09", "うちの店でも頼めますか",
     "焼肉店、焼き鳥店、焼きとん店など、厨房や焼き場に排気設備があるお店を対象にしています。"
     "そのほかのお店も、ご相談の内容を伺って、担当者からご連絡します。", "うちの店,対象,焼肉,焼き鳥"),
]
FALLBACK_ANSWER = "その点は、担当者が確認してご連絡します。"   # FAQ-10: recorded as an open question


def validate_config(data: dict) -> dict:
    """Return a normalised copy or raise ValueError with a message the admin page can show."""
    if not isinstance(data, dict):
        raise ValueError("設定の形式が正しくありません")
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    for key in ("ai_failure_action", "active_voice", "store_transcript", "retention_days", "provisional"):
        if key in data:
            cfg[key] = data[key]
    if cfg["ai_failure_action"] == "normal":   # saved before the schedule was removed
        cfg["ai_failure_action"] = "forward"
    if cfg["ai_failure_action"] not in FAILURE_ACTIONS:
        raise ValueError("AIが止まったときの扱いが正しくありません")
    voices = data.get("voices", cfg["voices"])
    if not voices:
        raise ValueError("声を1つ以上登録してください")
    ids = set()
    norm_voices = []
    for v in voices:
        vid = str(v.get("id") or "").strip()
        if not vid or vid in ids:
            raise ValueError("声の識別子が空か、重複しています")
        ids.add(vid)
        if not str(v.get("candidate", "")).strip() or not str(v.get("voice", "")).strip():
            raise ValueError("声には、接続候補と声の名前（またはID）が必要です")
        norm_voices.append({"id": vid, "candidate": str(v["candidate"]).strip(), "voice": str(v["voice"]).strip(),
                            "label": str(v.get("label", "")).strip()[:60]})
    cfg["voices"] = norm_voices
    if cfg["active_voice"] not in ids:
        raise ValueError("使う声が、登録した声の中にありません")
    fields = data.get("fields", cfg["fields"])
    cfg["fields"] = {}
    for k in FIELD_KEYS:
        f = fields.get(k, {"store": True, "summary": True, "notify": True})
        store = bool(f.get("store", True))
        # A field that may not be stored cannot appear in the summary or the notification either.
        cfg["fields"][k] = {"store": store, "summary": store and bool(f.get("summary", True)),
                            "notify": store and bool(f.get("notify", True))}
    cfg["store_transcript"] = bool(cfg["store_transcript"])
    rd = cfg["retention_days"]
    if rd in ("", None):
        cfg["retention_days"] = None
    else:
        try:
            rd = int(rd)
        except (TypeError, ValueError):
            raise ValueError("保存期間は日数で入力してください") from None
        if rd < 1:
            raise ValueError("保存期間は1日以上にしてください")
        cfg["retention_days"] = rd
    cfg["provisional"] = bool(cfg.get("provisional", True))
    return cfg


# --- voices ------------------------------------------------------------------------------------------------
# Voice names come from the vendors' public docs where they exist; none has been heard on a real connection.
VOICE_CATALOG = {
    "fake": {"voices": ["tone"], "how": "オフラインの模擬音（トーン）"},
    "gpt-realtime-2.1": {"voices": ["marin", "cedar", "alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer",
                                    "verse"],
                         "how": "接続のときにサーバーが声を指定する"},
    "gpt-live-1": {"voices": ["marin"], "how": "接続のときにサーバーが audio.output.voice で指定する（既定は marin。"
                                               "公式の追加の声の表は英語・ポルトガル語だけで、日本語に合う声は未確認）"},
    "gemini-3.8-live": {"voices": ["Kore", "Aoede", "Leda", "Puck", "Charon", "Orus"],
                        "how": "接続のときに prebuiltVoiceConfig で指定する（3.8で同じ名前が使えるかは未確認）"},
    "elevenagents": {"voices": [], "how": "ElevenLabsの画面でエージェントの声を選ぶ。声のIDを入力して登録（上書きの許可が要る）"},
    "cartesia-agents": {"voices": [], "how": "Cartesiaの画面でエージェントの声を選ぶ。声のIDを入力して記録する"},
}


def voice_status(candidate: str, env: dict, candidates: dict) -> tuple[str, str]:
    """('available'|'unverified'|'no_key'|'unknown', label) for one candidate on this machine."""
    cfg = candidates.get(candidate)
    if cfg is None:
        return "unknown", "候補にない"
    missing = [k for k in cfg["env"] if not env.get(k)]
    if cfg.get("verified"):
        return "available", "利用可能（オフラインの模擬）" if candidate == "fake" else "利用可能"
    if missing:
        return "no_key", "接続未確認・鍵が未設定"
    return "unverified", "接続未確認（鍵はある）"
