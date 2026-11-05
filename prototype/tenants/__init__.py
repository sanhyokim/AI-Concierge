"""The company settings (テナント設定, done for the customer: DFY). Design memo, 2026-11-05 (Boss): everything that
changes from one company to the next (its name and trade, the questions and their order, the FAQ seeds, the triage
table, the on-call roster, where notices go, what may not be said) is kept out of the engine and read from one file
per company; the instructions and the fixed lines are built from it. A second company should need a new file, not
new code.

The file is JSON (the standard library reads it; prototype/cost/candidates.json is the same kind of file). It is set
by the provider; the company does not edit it, so there is no screen for it. The FAQ and the voices stay editable
on the admin page: the file only seeds the FAQ of a new company.

Which file: AI_TENANT (a file path, or the id of a file in this folder), else noda.json.
TODO (spec 1-12): on the server, the company is chosen from the number that was dialled, and every record carries
its tenant id. Until then, one process serves one company.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
from dataclasses import dataclass, field

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_ID = "noda"
# The engine's items. A company names them, words the questions and sets the order; adding items of its own is a
# TODO (the read-back and the confirmation are built for these).
ENGINE_FIELDS = ("request", "preferred_datetime", "shop_name", "caller_name", "callback_number")
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


class TenantError(ValueError):
    """The company settings cannot be used; the message says what to fix."""


@dataclass(frozen=True)
class FollowUp:
    """A question on the request that the engine asks right after it, when the request calls for it."""
    key: str
    label: str
    question: str
    request_has_any: tuple[str, ...] = ()
    request_has_none: tuple[str, ...] = ()
    request_or_shop_has_any: tuple[str, ...] = ()
    count_not_said: bool = False
    count_units: tuple[str, ...] = ()
    answers: tuple[tuple[tuple[str, ...], str], ...] = ()
    asked_when_ai_says: tuple[tuple[str, ...], ...] = ()

    def needed(self, request: str, shop: str, count_said) -> bool:
        if not request:
            return False
        if self.request_has_any and not any(w in request for w in self.request_has_any):
            return False
        if any(w in request for w in self.request_has_none):
            return False
        if self.request_or_shop_has_any and not any(w in request + shop for w in self.request_or_shop_has_any):
            return False
        return not (self.count_not_said and count_said(request))

    def asked_in(self, ai_text: str) -> bool:
        """The AI asked this itself (every group has one of its words in what it said)."""
        return bool(self.asked_when_ai_says) and all(any(w in ai_text for w in g) for g in self.asked_when_ai_says)


@dataclass(frozen=True)
class Tenant:
    id: str
    company_name: str
    industry: str
    operator_name: str
    greeting: str
    greeting_question: str
    closing_words: str
    goodbye: str
    fields: tuple[dict, ...]
    request_words: tuple[str, ...]
    follow_ups: tuple[FollowUp, ...]
    follow_up_example: str
    examples: dict
    kana: tuple[tuple[str, str], ...]
    keywords: tuple[str, ...]
    faq_seeds: tuple[tuple[str, str, str, str], ...]
    hours_faq: str
    demo: dict
    business_hours: dict
    triage_rules: dict
    on_call_roster: tuple[dict, ...]
    notification: dict
    prohibitions: tuple[str, ...]
    fallback: dict
    source: str = ""
    raw: dict = field(default_factory=dict, repr=False, compare=False)

    # --- the words the engine builds from the settings ------------------------------------------------------
    def field(self, key: str) -> dict:
        return next(f for f in self.fields if f["key"] == key)

    @property
    def order(self) -> tuple[str, ...]:
        """The items in the order they are asked (2026-10-23 for Noda: the request and the date first)."""
        return tuple(f["key"] for f in self.fields)

    @property
    def labels(self) -> dict:
        return {f["key"]: f["label"] for f in self.fields}

    def ask_alone(self, key: str) -> str:
        """The question for this item asked by itself (Noda: the shop when the name is already known)."""
        f = self.field(key)
        return f.get("question_alone") or f["question"]

    @property
    def questions(self) -> dict:
        """The fixed question for each item and follow-up (ASK_FOR)."""
        return {**{f["key"]: f["question"] for f in self.fields}, **{u.key: u.question for u in self.follow_ups}}

    def ask_items(self) -> list[tuple[str, str, str]]:
        """(key, how the item is named, the question) for each question asked; an item asked with another
        (Noda: the name with the shop) is named with it and has no question of its own."""
        items = []
        for f in self.fields:
            if f.get("asked_with"):
                continue
            with_ = [g for g in self.fields if g.get("asked_with") == f["key"]]
            name = "と".join([f["polite"]] + [g["polite"] for g in with_])
            items.append((f["key"], name, f["question"]))
        return items

    def ask_names(self, full: bool = False) -> list[str]:
        """「ご用件」「ご希望の日時」「店舗名とお名前」「折り返しのお電話番号」 (full: the longer name where there is one)."""
        out = []
        for key, name, _ in self.ask_items():
            f = self.field(key)
            out.append(f.get("polite_full", name) if full and not [g for g in self.fields if g.get("asked_with") == key]
                       else name)
        return out

    @property
    def moshimoshi_reply(self) -> str:
        return f"はい、{self.company_name}、{self.operator_name}でございます。"

    @property
    def short_greeting(self) -> str:
        """The offline agent's first words: the company, then the first question."""
        return f"お電話ありがとうございます。{self.company_name}、{self.operator_name}でございます。" + self.field(self.order[0])["question"]

    def values(self) -> dict:
        """What the instruction templates may use: {company}, {industry}, {shop} and the rest."""
        ex = self.examples
        return {"company": self.company_name, "industry": self.industry, "operator": self.operator_name,
                "goodbye": self.goodbye, "moshimoshi": self.moshimoshi_reply,
                "shop": self.field("shop_name")["polite"], "name_label": self.field("caller_name")["polite"],
                "ask_request": self.field("request")["question"], "ask_date": self.field("preferred_datetime")["question"],
                "ask_shop": self.field("shop_name")["question"], "ask_number": self.field("callback_number")["question"],
                "ask_order": "、".join(self.ask_names()), "ask_order_full": "、".join(self.ask_names(full=True)),
                "all_polite": "・".join(f["polite"] for f in self.fields),
                # 「お電話ありがとうございます。…お気軽にお話しください。」: the opening named by its first and last words
                "greeting_quote": (self.greeting.split("。")[0] + "。…" + self.greeting_question.rsplit("、", 1)[-1]),
                "ex_shop": ex.get("shop", ""), "ex_name": ex.get("name", ""), "ex_request": ex.get("request", ""),
                "ex_date": ex.get("date", ""), "follow_up_example": self.follow_up_example}

    def fill(self, template: str, **more: str) -> str:
        """The template with the company's words in it. Only known names are replaced, so other braces (the
        engine's own {end}, {summary}) stay for the engine to fill later."""
        vals = {**self.values(), **more}
        return _PLACEHOLDER.sub(lambda m: str(vals[m.group(1)]) if m.group(1) in vals else m.group(0), template)


def _need(d: dict, key: str, where: str):
    if key not in d or d[key] in (None, ""):
        raise TenantError(f"会社別の設定に「{where}{key}」がありません")
    return d[key]


def _strs(value, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise TenantError(f"会社別の設定の「{where}」は、文字の並びにしてください")
    return tuple(value)


def from_dict(data: dict, source: str = "") -> Tenant:
    """Check the settings and turn them into a Tenant. Raises TenantError with what is wrong."""
    if not isinstance(data, dict):
        raise TenantError("会社別の設定の形式が正しくありません")
    t = _need(data, "tenant", "")
    fields = _need(data, "intake_fields", "")
    keys = [f.get("key") for f in fields]
    if sorted(keys) != sorted(ENGINE_FIELDS):
        raise TenantError(f"伺う項目（intake_fields）は {', '.join(ENGINE_FIELDS)} の5つを、1回ずつ並べてください")
    for f in fields:
        for k in ("label", "polite", "question"):
            _need(f, k, f"intake_fields の {f['key']} の ")
        if f.get("asked_with") and f["asked_with"] not in keys:
            raise TenantError(f"intake_fields の {f['key']} の asked_with が、伺う項目にありません")
    ups = []
    for u in data.get("follow_ups", []):
        w = u.get("ask_when", {})
        ups.append(FollowUp(
            key=_need(u, "key", "follow_ups の "), label=_need(u, "label", "follow_ups の "),
            question=_need(u, "question", "follow_ups の "),
            request_has_any=_strs(w.get("request_has_any", []), "ask_when.request_has_any"),
            request_has_none=_strs(w.get("request_has_none", []), "ask_when.request_has_none"),
            request_or_shop_has_any=_strs(w.get("request_or_shop_has_any", []), "ask_when.request_or_shop_has_any"),
            count_not_said=bool(w.get("count_not_said")), count_units=_strs(u.get("count_units", []), "count_units"),
            answers=tuple((_strs(a["words"], "answers.words"), a["name"]) for a in u.get("answers", [])),
            asked_when_ai_says=tuple(_strs(g, "asked_when_ai_says") for g in u.get("asked_when_ai_says", []))))
    if len({u.key for u in ups} | set(keys)) != len(ups) + len(keys):
        raise TenantError("追加の質問（follow_ups）の key が、ほかの項目と重なっています")
    if any(u.count_not_said and not u.count_units for u in ups):
        raise TenantError("数を伺う追加の質問には、数の単位（count_units）が要ります")
    seeds = tuple((s["code"], s["question"], s["answer"], s.get("keywords", "")) for s in data.get("faq_seeds", []))
    speech = data.get("speech", {})
    return Tenant(
        id=_need(t, "id", "tenant."), company_name=_need(t, "company_name", "tenant."),
        industry=_need(t, "industry", "tenant."), operator_name=_need(t, "operator_name", "tenant."),
        greeting=_need(t, "greeting", "tenant."), greeting_question=_need(t, "greeting_question", "tenant."),
        closing_words=_need(t, "closing_words", "tenant."), goodbye=_need(t, "goodbye", "tenant."),
        fields=tuple(fields), request_words=_strs(data.get("request_words", []), "request_words"),
        follow_ups=tuple(ups), follow_up_example=data.get("follow_up_example", ""),
        examples=dict(data.get("examples", {})),
        kana=tuple((a, b) for a, b in speech.get("kana", [])), keywords=_strs(speech.get("keywords", []), "speech.keywords"),
        faq_seeds=seeds, hours_faq=data.get("hours_faq", ""), demo=dict(data.get("demo", {})),
        business_hours=dict(t.get("business_hours") or {}), triage_rules=dict(data.get("triage_rules") or {}),
        on_call_roster=tuple(data.get("on_call_roster") or ()), notification=dict(data.get("notification") or {}),
        prohibitions=_strs(data.get("prohibitions", []), "prohibitions"), fallback=dict(data.get("fallback") or {}),
        source=source, raw=data)


def path_for(choice: str | None = None) -> pathlib.Path:
    choice = choice or os.environ.get("AI_TENANT") or DEFAULT_ID
    p = pathlib.Path(choice)
    if p.suffix == ".json":
        return p
    if not re.fullmatch(r"[a-z0-9_-]+", choice):
        raise TenantError(f"会社別の設定の名前が正しくありません：{choice}")
    return HERE / f"{choice}.json"


def load(choice: str | None = None) -> Tenant:
    p = path_for(choice)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise TenantError(f"会社別の設定のファイルがありません：{p}") from None
    except json.JSONDecodeError as e:
        raise TenantError(f"会社別の設定のファイルが読めません（{p} の {e.lineno} 行目）") from None
    return from_dict(data, str(p))


_CURRENT: Tenant | None = None


def current() -> Tenant:
    """The company this process serves (read once)."""
    global _CURRENT
    if _CURRENT is None:
        _CURRENT = load()
    return _CURRENT
