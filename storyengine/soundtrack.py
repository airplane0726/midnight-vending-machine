"""効果音・環境音・音楽を、標準ライブラリだけで合成して WAV にする（無料・権利の心配がない仮の音）。

指示の形（作品固有の値は編集指示 JSON に書く）:
{
  "sample_rate": 44100, "seed": 1,
  "ambience": [{"type": "hum|wind|rain|rain_rise", "gain_db": -30, "start": 秒, "end": 秒,
                "fade": 秒, "pan": [[t, -1〜1], ...], "freq": 50}],
  "events": [{"t": 秒, "type": "bell|musicbox|celesta|pizz|glass|knock|click|thump|rattle|squeak|beep|whoosh|pop",
              "note": "A5" または "freq": Hz, "to": 終わりの Hz, "dur": 秒, "gain_db": -12, "pan": -1〜1, "count": 回数}],
  "music": {"gain_db": -14, "notes": [[t, "instr", "A5", dur]]}   … events の省略形
}
"""
from __future__ import annotations

import array
import math
import random
import wave
from pathlib import Path
from typing import Any

NOTE_INDEX = {"C": -9, "C#": -8, "Db": -8, "D": -7, "D#": -6, "Eb": -6, "E": -5, "F": -4, "F#": -3, "Gb": -3,
              "G": -2, "G#": -1, "Ab": -1, "A": 0, "A#": 1, "Bb": 1, "B": 2}


def note_freq(name: str) -> float:
    name = name.strip()
    pitch, octave = (name[:2], name[2:]) if len(name) > 2 and name[1] in "#b" else (name[:1], name[1:])
    return 440.0 * 2 ** ((NOTE_INDEX[pitch] + (int(octave) - 4) * 12) / 12)


def db(g: float) -> float:
    return 10 ** (g / 20)


class Mix:
    def __init__(self, duration: float, sr: int) -> None:
        self.sr = sr
        self.n = int(duration * sr) + 1
        self.l = array.array("f", bytes(4 * self.n))
        self.r = array.array("f", bytes(4 * self.n))

    def add(self, start: float, samples: list[float], gain: float, pan: float | list | None = 0.0) -> None:
        i0 = int(start * self.sr)
        for k, s in enumerate(samples):
            i = i0 + k
            if i < 0:
                continue
            if i >= self.n:
                break
            p = pan if isinstance(pan, (int, float)) else _interp(pan, i / self.sr)
            p = max(-1.0, min(1.0, p or 0.0))
            a = (p + 1) * math.pi / 4
            self.l[i] += s * gain * math.cos(a) * 1.4142
            self.r[i] += s * gain * math.sin(a) * 1.4142

    def write(self, path: Path) -> None:
        peak = max(1e-9, max(max(abs(x) for x in self.l), max(abs(x) for x in self.r)))
        k = min(1.0, 0.89 / peak)
        data = array.array("h", bytes(2 * 2 * self.n))
        for i in range(self.n):
            data[2 * i] = int(max(-1.0, min(1.0, self.l[i] * k)) * 32767)
            data[2 * i + 1] = int(max(-1.0, min(1.0, self.r[i] * k)) * 32767)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(self.sr)
            w.writeframes(data.tobytes())


def _interp(pts: list, t: float) -> float:
    pts = sorted(pts)
    if t <= pts[0][0]:
        return pts[0][1]
    for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
        if t <= t1:
            return v0 + (v1 - v0) * (t - t0) / max(t1 - t0, 1e-9)
    return pts[-1][1]


# ---------------- 音源 ----------------
def tone(sr: int, freq: float, dur: float, partials: list[tuple[float, float, float]], attack: float = 0.004,
         to: float | None = None, vibrato: float = 0.0) -> list[float]:
    """partials: (倍音の比, 大きさ, 減衰の速さ)。to を指定すると音の高さが滑らかに変わる。"""
    n = int(dur * sr)
    out = [0.0] * n
    phases = [0.0] * len(partials)
    for i in range(n):
        t = i / sr
        f = freq if to is None else freq * (to / freq) ** (t / dur)
        if vibrato:
            f *= 1 + vibrato * math.sin(2 * math.pi * 5.5 * t)
        env_a = min(1.0, t / attack) if attack > 0 else 1.0
        s = 0.0
        for j, (ratio, amp, decay) in enumerate(partials):
            phases[j] += 2 * math.pi * f * ratio / sr
            s += amp * math.exp(-decay * t) * math.sin(phases[j])
        out[i] = s * env_a
    tail = min(n, int(0.01 * sr))
    for i in range(tail):
        out[n - 1 - i] *= i / tail
    return out


def noise(sr: int, dur: float, rnd: random.Random, lp: float | list = 0.2, hp: float = 0.0,
          env: list | None = None) -> list[float]:
    """白色雑音に簡単なフィルタ（lp: 低域通過の係数 0〜1、時間で変えられる）。"""
    n = int(dur * sr)
    out, y, hy, px = [0.0] * n, 0.0, 0.0, 0.0
    for i in range(n):
        t = i / sr
        a = lp if isinstance(lp, (int, float)) else _interp(lp, t)
        x = rnd.uniform(-1, 1)
        y += a * (x - y)
        v = y
        if hp:
            hy = hp * (hy + v - px)
            px = v
            v = hy
        out[i] = v * (_interp(env, t) if env else 1.0)
    return out


def event(sr: int, e: dict[str, Any], rnd: random.Random) -> list[float]:
    typ = e["type"]
    f = note_freq(e["note"]) if e.get("note") else float(e.get("freq", 440))
    dur = float(e.get("dur", 0))
    if typ == "bell":
        return tone(sr, f, dur or 2.0, [(1, 1, 1.8), (2.0, 0.35, 3), (3.01, 0.15, 5), (4.2, 0.08, 8)], attack=0.003)
    if typ == "musicbox":
        return tone(sr, f, dur or 1.6, [(1, 1, 2.2), (2.0, 0.25, 4), (3.98, 0.12, 9), (5.9, 0.05, 14)], attack=0.002)
    if typ == "celesta":
        return tone(sr, f, dur or 1.0, [(1, 1, 4), (2.0, 0.3, 7), (4.0, 0.1, 12)], attack=0.002)
    if typ == "pizz":
        return tone(sr, f, dur or 0.5, [(1, 1, 7), (2, 0.5, 10), (3, 0.2, 14)], attack=0.006)
    if typ == "glass":
        d = dur or 1.5
        s = tone(sr, f, d, [(1, 1, 0.3), (2, 0.15, 0.6)], attack=min(0.35, d / 3), vibrato=0.003)
        rel = int(min(0.4, d / 3) * sr)
        for i in range(rel):
            s[len(s) - 1 - i] *= i / rel
        return s
    if typ == "knock":
        s = tone(sr, f or 520, dur or 0.09, [(1, 1, 60), (2.3, 0.4, 90)], attack=0.001)
        nz = noise(sr, 0.02, rnd, lp=0.5, env=[[0, 0.5], [0.02, 0]])
        return [a + (nz[i] if i < len(nz) else 0) for i, a in enumerate(s)]
    if typ == "click":
        s = tone(sr, f or 2600, dur or 0.04, [(1, 0.6, 120)], attack=0.0005)
        nz = noise(sr, 0.006, rnd, lp=0.9, env=[[0, 1], [0.006, 0]])
        return [a + (nz[i] if i < len(nz) else 0) for i, a in enumerate(s)]
    if typ == "thump":
        s = tone(sr, (f or 70) * 1.6, dur or 0.45, [(1, 1, 9), (2, 0.3, 14)], attack=0.002, to=f or 70)
        nz = noise(sr, 0.12, rnd, lp=0.05, env=[[0, 1.5], [0.12, 0]])
        return [a + (nz[i] if i < len(nz) else 0) for i, a in enumerate(s)]
    if typ == "rattle":
        out: list[float] = []
        for k in range(int(e.get("count", 3))):
            c = tone(sr, (f or 900) * (1 + 0.1 * rnd.random()), 0.03, [(1, 0.5, 120), (2.7, 0.3, 160)], attack=0.0005)
            gap = [0.0] * int(rnd.uniform(0.05, 0.09) * sr)
            out += c + gap
        return out
    if typ == "squeak":
        return tone(sr, f or 1700, dur or 0.25, [(1, 0.6, 2), (2, 0.15, 3)], attack=0.03,
                    to=float(e.get("to", (f or 1700) * 1.3)), vibrato=0.02)
    if typ == "beep":
        return tone(sr, f or 1500, dur or 0.09, [(1, 0.8, 4), (3, 0.12, 6)], attack=0.004, to=e.get("to"))
    if typ == "whoosh":
        d = dur or 0.4
        up = bool(e.get("up", True))
        lp = [[0, 0.04], [d, 0.35]] if up else [[0, 0.35], [d, 0.04]]
        return noise(sr, d, rnd, lp=lp, env=[[0, 0], [d * 0.6, 1], [d, 0]])
    if typ == "pop":
        return tone(sr, f or 900, dur or 0.06, [(1, 0.7, 70)], attack=0.001, to=(f or 900) * 1.5)
    raise ValueError(f"知らない音: {typ}")


def ambience(sr: int, a: dict[str, Any], rnd: random.Random, duration: float) -> tuple[float, list[float]]:
    st = float(a.get("start", 0))
    en = float(a.get("end", duration))
    d = max(0.0, en - st)
    fade = float(a.get("fade", 0.3))
    env = [[0, 0], [min(fade, d / 2), 1], [max(d - fade, d / 2), 1], [d, 0]]
    typ = a["type"]
    if typ == "hum":
        f = float(a.get("freq", 50))
        n = int(d * sr)
        out = [0.0] * n
        for i in range(n):
            t = i / sr
            m = 1 + 0.08 * math.sin(2 * math.pi * 0.7 * t)
            out[i] = m * (math.sin(2 * math.pi * f * t) + 0.5 * math.sin(4 * math.pi * f * t)
                          + 0.25 * math.sin(6 * math.pi * f * t)) * _interp(env, t)
        return st, out
    if typ == "wind":
        return st, noise(sr, d, rnd, lp=0.01, env=env)
    if typ in ("rain", "rain_rise"):
        if typ == "rain":
            base = noise(sr, d, rnd, lp=0.6, hp=0.9, env=env)
        else:  # 吸い上がる雨：こもった音から明るい音へ、だんだん大きく
            base = noise(sr, d, rnd, lp=[[0, 0.05], [d, 0.7]], hp=0.85,
                         env=[[0, 0], [d * 0.25, 0.6], [d * 0.9, 1.2], [d, 0]])
        for _ in range(int(d * 35)):  # 粒の音
            i = rnd.randrange(max(1, len(base) - 600))
            amp = rnd.uniform(0.2, 0.6) * _interp(env, i / sr)
            f = rnd.uniform(2500, 6000)
            for k in range(400):
                base[i + k] += amp * math.exp(-k / 60) * math.sin(2 * math.pi * f * k / sr)
        return st, base
    raise ValueError(f"知らない環境音: {typ}")


def render(spec: dict[str, Any], path: Path, *, duration: float) -> None:
    sr = int(spec.get("sample_rate", 44100))
    rnd = random.Random(int(spec.get("seed", 1)))
    mix = Mix(duration, sr)
    for a in spec.get("ambience") or []:
        st, s = ambience(sr, a, rnd, duration)
        mix.add(st, s, db(float(a.get("gain_db", -30))), a.get("pan", 0.0))
    for e in spec.get("events") or []:
        mix.add(float(e["t"]), event(sr, e, rnd), db(float(e.get("gain_db", -12))), e.get("pan", 0.0))
    mus = spec.get("music") or {}
    g = float(mus.get("gain_db", -14))
    for t, instr, note, dur, *rest in mus.get("notes") or []:
        extra = rest[0] if rest else {}
        mix.add(float(t), event(sr, {"type": instr, "note": note, "dur": dur}, rnd),
                db(g + float(extra.get("gain_db", 0))), extra.get("pan", 0.0))
    mix.write(path)
