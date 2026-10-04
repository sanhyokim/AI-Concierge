"""Consent, refusal and DTMF fallback flow (design review v2.1 sections 4-2 to 4-4).

The flow emits actions instead of playing audio, so it can be checked offline:
    ("say_ai", intent)            AI speaks (only while ai_processing is allowed)
    ("play", clip_id)             pre-recorded clip, no AI involved
    ("gather_dtmf", spec)         collect keypad input
    ("stop_ai_stream",)           stop sending caller audio to the AI vendor
    ("stop_recording",)
    ("mark_for_deletion", what)   pre-refusal recording / transcript
    ("save_field", name, value, source, status)
    ("notify", kind)
    ("end_call", reason)
Live transfer to a person is out of scope and is never emitted.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .readings import digits_only, is_valid_jp_number

MAX_DTMF_RETRIES = 2  # re-prompts after the first attempt


@dataclass
class Consent:
    recording: str = "allowed"        # allowed | refused
    ai_processing: str = "allowed"    # allowed | refused
    recording_announced: bool = True


@dataclass
class CallFlow:
    caller_id: str | None = None
    voicemail_enabled: bool = False
    consent: Consent = field(default_factory=Consent)
    state: str = "ai_conversation"
    intake_mode: str = "standard"     # standard | minimal
    ai_available: bool = True
    human_request_asked: bool = False
    retries: int = 0
    pending_number: str | None = None
    log: list[tuple] = field(default_factory=list)

    def _emit(self, *actions: tuple) -> list[tuple]:
        self.log.extend(actions)
        return list(actions)

    # --- recording / AI consent ------------------------------------------------

    def on_recording_refused(self) -> list[tuple]:
        if self.consent.recording == "refused":
            return []
        self.consent.recording = "refused"
        out = [("stop_recording",), ("mark_for_deletion", "recording_before_refusal")]
        if self.consent.ai_processing == "allowed" and self.ai_available:
            out.append(("say_ai", "ack_recording_stopped_ai_still_processes_voice"))
        return self._emit(*out)

    def on_human_request(self) -> list[tuple]:
        """'人と話したい': explain the callback and ask once whether a short AI intake may continue."""
        if self.human_request_asked:
            return self._emit(("say_ai", "explain_callback_only"))
        self.human_request_asked = True
        self.state = "human_request_pending"
        return self._emit(("say_ai", "explain_no_live_transfer_and_callback_ask_short_intake"))

    def on_human_request_answer(self, accepted: bool) -> list[tuple]:
        if self.state != "human_request_pending":
            return []
        if accepted:
            self.state, self.intake_mode = "ai_conversation", "minimal"
            return self._emit(("say_ai", "continue_minimal_intake"))
        return self._enter_dtmf_path(reason="ai_refused")

    def on_ai_refused(self) -> list[tuple]:
        return self._enter_dtmf_path(reason="ai_refused")

    def on_ai_failure(self) -> list[tuple]:
        """AI vendor failed mid-call: apologise with a clip, then DTMF. Consent is carried over unchanged."""
        self.ai_available = False
        return self._enter_dtmf_path(reason="ai_failure")

    def _enter_dtmf_path(self, reason: str) -> list[tuple]:
        out: list[tuple] = [("stop_ai_stream",)]
        if reason == "ai_refused":
            self.consent.ai_processing = "refused"
            # Default in the design: stop recording on AI refusal (data minimisation) and drop
            # the pre-refusal transcript; required intake fields are kept.
            if self.consent.recording == "allowed":
                out.append(("stop_recording",))
            out.append(("mark_for_deletion", "transcript_before_refusal"))
            out.append(("play", "clip_ai_refusal_ack"))
        else:
            out.append(("play", "clip_ai_failure_apology"))
            out.append(("notify", "ai_failure"))
        self.state, self.retries = "dtmf_entry", 0
        out += [("play", "clip_dtmf_enter_number"), ("gather_dtmf", {"finish_on": "#", "timeout_s": 8})]
        return self._emit(*out)

    def can_use_voicemail(self) -> bool:
        return (self.voicemail_enabled and self.consent.recording == "allowed"
                and self.consent.recording_announced)

    # --- DTMF ------------------------------------------------------------------

    def on_dtmf(self, digits: str | None) -> list[tuple]:
        """digits=None means the gather timed out with no input."""
        if self.state == "dtmf_entry":
            return self._on_entry(digits)
        if self.state == "dtmf_confirm":
            return self._on_confirm(digits)
        if self.state == "dtmf_offer_caller_id":
            return self._on_offer(digits)
        return []

    def _on_entry(self, digits: str | None) -> list[tuple]:
        number = digits_only(digits or "")
        if number and is_valid_jp_number(number):
            self.state, self.pending_number, self.retries = "dtmf_confirm", number, 0
            clips = [("play", "clip_readback_intro")] + [("play", f"clip_digit_{d}") for d in number]
            return self._emit(*clips, ("play", "clip_confirm_1_or_2"),
                              ("gather_dtmf", {"num_digits": 1, "timeout_s": 6}))
        self.retries += 1
        if self.retries <= MAX_DTMF_RETRIES:
            clip = "clip_dtmf_invalid_retry" if number else "clip_dtmf_no_input_retry"
            return self._emit(("play", clip), ("gather_dtmf", {"finish_on": "#", "timeout_s": 8}))
        if self.caller_id:
            self.state = "dtmf_offer_caller_id"
            clips = [("play", "clip_offer_caller_id_intro")] + [
                ("play", f"clip_digit_{d}") for d in digits_only(self.caller_id)]
            return self._emit(*clips, ("play", "clip_offer_caller_id_press_1"),
                              ("gather_dtmf", {"num_digits": 1, "timeout_s": 6}))
        return self._end_without_callback()

    def _on_confirm(self, digit: str | None) -> list[tuple]:
        if digit == "1":
            self.state = "ended"
            return self._emit(("save_field", "callback_number", self.pending_number, "dtmf",
                               "confirmed_by_caller"),
                              ("play", "clip_thanks_end"), ("notify", self._notify_kind()),
                              ("end_call", "callback_captured"))
        if digit == "2":
            self.state, self.pending_number = "dtmf_entry", None
            return self._emit(("play", "clip_dtmf_enter_number"),
                              ("gather_dtmf", {"finish_on": "#", "timeout_s": 8}))
        self.retries += 1
        if self.retries <= MAX_DTMF_RETRIES:
            return self._emit(("play", "clip_confirm_1_or_2"),
                              ("gather_dtmf", {"num_digits": 1, "timeout_s": 6}))
        self.state = "ended"
        return self._emit(("save_field", "callback_number", self.pending_number, "dtmf", "unconfirmed"),
                          ("play", "clip_thanks_end"), ("notify", self._notify_kind()),
                          ("end_call", "callback_unconfirmed"))

    def _on_offer(self, digit: str | None) -> list[tuple]:
        if digit == "1":
            self.state = "ended"
            return self._emit(("save_field", "callback_number", digits_only(self.caller_id or ""),
                               "line", "confirmed_by_caller"),
                              ("play", "clip_thanks_end"), ("notify", self._notify_kind()),
                              ("end_call", "callback_captured"))
        return self._end_without_callback()

    def _end_without_callback(self) -> list[tuple]:
        self.state = "ended"
        kind = "ai_refused_callback_missing" if self.consent.ai_processing == "refused" else \
            "ai_failure_callback_missing"
        return self._emit(("play", "clip_end_please_call_again"), ("notify", kind),
                          ("end_call", "callback_missing"))

    def _notify_kind(self) -> str:
        return "ai_refused_intake" if self.consent.ai_processing == "refused" else "ai_failure_intake"
