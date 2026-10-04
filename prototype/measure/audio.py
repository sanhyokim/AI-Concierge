"""Audio helpers in pure Python (no numpy, no audioop): WAV I/O, G.711 mu-law, frame energy.

The mu-law round trip simulates the telephone codec only; it does not model
network loss, jitter or the band-pass of a real phone line.
"""
from __future__ import annotations

import math
import struct
import wave
from array import array

_BIAS = 0x84
_CLIP = 32635
_EXP_LUT = [0] * 256
for _i in range(1, 256):
    _EXP_LUT[_i] = int(math.log2(_i))


def read_wav(path: str) -> tuple[array, int]:
    with wave.open(path, "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError("only 16-bit PCM WAV is supported")
        rate, channels = w.getframerate(), w.getnchannels()
        raw = w.readframes(w.getnframes())
    samples = array("h")
    samples.frombytes(raw)
    if channels > 1:  # keep the first channel
        samples = array("h", samples[::channels])
    return samples, rate


def write_wav(path: str, samples: array, rate: int) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())


def pcm16_from_bytes(raw: bytes) -> array:
    samples = array("h")
    samples.frombytes(raw[: len(raw) // 2 * 2])
    return samples


def ulaw_encode_sample(sample: int) -> int:
    sign = 0
    if sample < 0:
        sign, sample = 0x80, -sample
    sample = min(sample, _CLIP) + _BIAS
    exponent = _EXP_LUT[(sample >> 7) & 0xFF]
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def ulaw_decode_byte(byte: int) -> int:
    byte = ~byte & 0xFF
    sign, exponent, mantissa = byte & 0x80, (byte >> 4) & 0x07, byte & 0x0F
    sample = (((mantissa << 3) + _BIAS) << exponent) - _BIAS
    return -sample if sign else sample


def ulaw_encode(samples: array) -> bytes:
    return bytes(ulaw_encode_sample(s) for s in samples)


def ulaw_decode(data: bytes) -> array:
    return array("h", (ulaw_decode_byte(b) for b in data))


def decimate(samples: array, factor: int) -> array:
    """Integer downsampling with a boxcar average (adequate for 16k -> 8k in this prototype)."""
    if factor == 1:
        return array("h", samples)
    out = array("h")
    for i in range(0, len(samples) - factor + 1, factor):
        out.append(int(sum(samples[i:i + factor]) / factor))
    return out


def to_phone_band(samples: array, rate: int) -> tuple[array, int]:
    """Resample to 8 kHz (integer factors only) and pass through G.711 mu-law."""
    if rate % 8000:
        raise ValueError(f"unsupported sample rate {rate}; use a multiple of 8000")
    narrow = decimate(samples, rate // 8000)
    return ulaw_decode(ulaw_encode(narrow)), 8000


def frame_db(samples: array, rate: int, frame_ms: int = 10) -> list[float]:
    """RMS level per frame in dBFS."""
    n = max(1, rate * frame_ms // 1000)
    out = []
    for i in range(0, len(samples) - n + 1, n):
        frame = samples[i:i + n]
        energy = sum(s * s for s in frame) / n
        out.append(10 * math.log10(energy / (32768.0 ** 2) + 1e-12))
    return out


def voiced_threshold_db(levels: list[float], *, drop_db: float = 30.0, floor_margin_db: float = 6.0) -> float:
    """Adaptive threshold: max(95th percentile - drop_db, 10th percentile + floor_margin_db)."""
    ordered = sorted(levels)
    p = lambda q: ordered[min(len(ordered) - 1, int(q * (len(ordered) - 1)))]
    return max(p(0.95) - drop_db, p(0.10) + floor_margin_db)


def pack_pcm16(samples: list[int]) -> array:
    return array("h", (max(-32768, min(32767, int(s))) for s in samples))


def pcm16_bytes(samples: array) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)
