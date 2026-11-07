"""Turn a short melody (note names + beats) into a chiptune WAV for the radio."""
from __future__ import annotations

import io
import math
import re
import struct
import wave

RATE = 22050
_SEMI = {'C': 0, 'D': 2, 'E': 4, 'F': 5, 'G': 7, 'A': 9, 'B': 11}


def freq(name: str) -> float | None:
    m = re.fullmatch(r'([A-Ga-g])([#b]?)(\d)', name.strip())
    if not m:
        return None  # rest or unknown
    step = _SEMI[m.group(1).upper()] + {'#': 1, 'b': -1, '': 0}[m.group(2)]
    midi = 12 * (int(m.group(3)) + 1) + step
    return 440.0 * 2 ** ((midi - 69) / 12)


def melody_wav(notes, tempo=120, repeat=2, max_seconds=40) -> bytes:
    """notes: [{"n": "E5", "b": 1}, ...]; 'R' is a rest."""
    beat = 60.0 / max(60, min(200, tempo))
    seq = [(str(n.get('n', 'R')), float(n.get('b', 1))) for n in notes][:160]
    seq = [(n, max(.125, min(4.0, b))) for n, b in seq] * max(1, repeat)
    total = min(max_seconds, sum(b for _, b in seq) * beat)
    out = [0.0] * int(total * RATE + RATE * .3)
    t = 0.0
    for name, beats in seq:
        f = freq(name)
        start, count = int(t * RATE), int(beats * beat * RATE)
        if start >= len(out):
            break
        if f:
            for i in range(min(count, len(out) - start)):
                age = i / RATE
                env = min(1, age / .005) * math.exp(-2.6 * age) * min(1, (count - i) / (.02 * RATE))
                phase = (f * age) % 1
                square = 1.0 if phase < .25 else -1.0                  # NES-style 25% pulse
                tri = 4 * abs(phase - .5) - 1
                bass = math.sin(math.tau * f / 2 * age)
                out[start + i] += (.11 * square + .16 * tri + .07 * bass) * env
        t += beats * beat
    pcm = bytearray()
    n = len(out)
    for i, v in enumerate(out):
        fade = min(1, i / (.02 * RATE), (n - i) / (.15 * RATE))
        pcm += struct.pack('<h', int(max(-1, min(1, v * fade)) * 32000))
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setparams((1, 2, RATE, 0, 'NONE', 'not compressed'))
        w.writeframes(pcm)
    return buf.getvalue()
