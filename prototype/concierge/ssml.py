"""SSML for partial slow-down with Amazon Polly (direct API or via ConversationRelay).

The slowed segment is opened and closed inside one string (one Relay text
token), because Relay's behaviour for tags spanning several tokens is not
documented. Marks before and after the segment give speech-mark timestamps
for the measurement.
"""
from __future__ import annotations

import re
from xml.sax.saxutils import escape

_RATE_RE = re.compile(r"^(\d{2,3})%$")
_NAMED_RATES = {"x-slow", "slow", "medium", "fast", "x-fast"}


def validate_rate(rate: str) -> str:
    """Polly accepts named rates or n% between 20 and 200 (prosody tag documentation)."""
    if rate in _NAMED_RATES:
        return rate
    m = _RATE_RE.match(rate)
    if not m or not 20 <= int(m.group(1)) <= 200:
        raise ValueError(f"invalid prosody rate: {rate!r}")
    return rate


def segment_ssml(pre: str, target: str, post: str, slow_rate: str | None, *, marks: bool = True,
                 wrap_speak: bool = True) -> str:
    """Build SSML for "normal -> target (optionally slowed) -> normal".

    slow_rate=None produces the normal version (N) with identical marks, so N and D
    are segmented the same way.
    """
    body = escape(target)
    if slow_rate is not None:
        body = f'<prosody rate="{validate_rate(slow_rate)}">{body}</prosody>'
    mark = (lambda name: f'<mark name="{name}"/>') if marks else (lambda name: "")
    inner = f"{escape(pre)}{mark('target_start')}{body}{mark('target_end')}{escape(post)}"
    return f"<speak>{inner}</speak>" if wrap_speak else inner


def relay_slow_token(text: str, rate: str) -> str:
    """One ConversationRelay text token that slows a single segment (no <speak> wrapper)."""
    return f'<prosody rate="{validate_rate(rate)}">{escape(text)}</prosody>'
