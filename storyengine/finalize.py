"""投稿用 MP4 の書き出しと最終確認（無料。生成はしない）。

- 元は生成記録の完成済みの動画（既定は最新の roughcut）。映像は投稿向けに作り直し（キーフレーム間隔・色の情報・
  先頭に目次（faststart））、音声はそのまま写す。
- 書き出し後に機械で確認する: 大きさ・フレームレート（一定）・長さ・映像と音声の長さの差・音量（ラウドネス）・
  ピーク（音割れ）・キーフレーム間隔・コーデック・faststart。
- 結果は生成記録の "final" に、確認結果つきで記録する（persist / restore の対象）。

設定は series.json の "publish_export"（任意）で上書きできる:
  {"crf": 17, "gop_sec": 1.0, "loudness": [-18, -13], "true_peak_max": -1.0, "max_keyframe_gap_sec": 2.0}
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .media import inspect_media, load_records, save_records
from .series import Series
from .storage import content_hash, exclusive_lock

EXPORT_VERSION = 1
DEFAULTS = {"crf": 17, "preset": "slow", "gop_sec": 1.0, "loudness": [-18.0, -13.0], "true_peak_max": -1.0,
            "max_keyframe_gap_sec": 2.0, "max_av_diff_sec": 0.05}


class FinalizeError(RuntimeError):
    pass


def _run(args: list[str]) -> str:
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        raise FinalizeError(f"{args[0]} が失敗しました: {r.stderr[-400:]}")
    return r.stdout + r.stderr


def probe(path: Path) -> dict[str, Any]:
    out = _run(["ffprobe", "-v", "error", "-show_entries",
                "format=duration:format_tags=comment:stream=codec_type,codec_name,profile,pix_fmt,width,height,"
                "r_frame_rate,avg_frame_rate,sample_rate,channels,duration,color_primaries,color_transfer,color_space",
                "-of", "json", str(path)])
    return json.loads(out[out.index("{"):])


def loudness(path: Path) -> dict[str, float]:
    out = _run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128=peak=true:framelog=quiet",
                "-f", "null", "-"])
    summary = out[out.rfind("Summary:"):]
    i = re.search(r"I:\s+(-?[\d.]+) LUFS", summary)
    lra = re.search(r"LRA:\s+(-?[\d.]+) LU", summary)
    tp = re.search(r"Peak:\s+(-?[\d.]+|-inf) dBFS", summary)
    if not (i and tp):
        raise FinalizeError("音量を測れませんでした")
    return {"integrated_lufs": float(i.group(1)), "lra_lu": float(lra.group(1)) if lra else None,
            "true_peak_dbfs": float(tp.group(1)) if tp.group(1) != "-inf" else -99.0}


def keyframes(path: Path) -> list[float]:
    out = _run(["ffprobe", "-v", "error", "-select_streams", "v", "-skip_frame", "nokey", "-show_entries",
                "frame=pts_time", "-of", "csv=p=0", str(path)])
    vals = [x.strip().strip(",") for x in out.split()]
    return [float(x) for x in vals if x]


def faststart(path: Path) -> bool:
    with open(path, "rb") as f:
        head = f.read(1 << 20)
    m, d = head.find(b"moov"), head.find(b"mdat")
    return m != -1 and (d == -1 or m < d)


def check(path: Path, fmt: dict[str, Any], opts: dict[str, Any] | None = None) -> dict[str, Any]:
    """書き出したファイルを確認し、項目ごとの結果を返す。fmt は series.json の format。"""
    o = dict(DEFAULTS, **(opts or {}))
    p = probe(path)
    v = next((s for s in p["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in p["streams"] if s["codec_type"] == "audio"), None)
    if not v or not a:
        raise FinalizeError("映像または音声がありません")
    num, den = (int(x) for x in v["avg_frame_rate"].split("/"))
    fps = num / den if den else 0
    rnum, rden = (int(x) for x in v["r_frame_rate"].split("/"))
    want_w, want_h = (int(x) for x in str(fmt.get("resolution", "1080x1920")).split("x"))
    want_fps = float(fmt.get("fps", 30))
    want_dur = float(fmt.get("duration_sec") or fmt.get("total_seconds") or 0) or None
    vd, ad, fd = float(v.get("duration", 0)), float(a.get("duration", 0)), float(p["format"]["duration"])
    loud = loudness(path)
    kf = keyframes(path)
    gaps = [b - a_ for a_, b in zip(kf, kf[1:])] + ([fd - kf[-1]] if kf else [])
    items = [
        ("大きさ", f"{v['width']}x{v['height']}", (v["width"], v["height"]) == (want_w, want_h)),
        ("フレームレート（一定）", f"{fps:g}fps", abs(fps - want_fps) < 0.01 and rden and abs(rnum / rden - fps) < 0.01),
        ("映像", f"{v['codec_name']} {v.get('profile')} {v.get('pix_fmt')}",
         v["codec_name"] == "h264" and v.get("pix_fmt") == "yuv420p"),
        ("音声", f"{a['codec_name']} {a.get('sample_rate')}Hz {a.get('channels')}ch",
         a["codec_name"] == "aac" and int(a.get("sample_rate", 0)) == 48000 and int(a.get("channels", 0)) == 2),
        ("長さ", f"{fd:.3f}秒", want_dur is None or abs(fd - want_dur) <= 1 / want_fps + 1e-6),
        ("映像と音声の長さの差", f"{abs(vd - ad) * 1000:.0f}ms", abs(vd - ad) <= o["max_av_diff_sec"]),
        ("音量（ラウドネス）", f"{loud['integrated_lufs']:.1f} LUFS",
         o["loudness"][0] <= loud["integrated_lufs"] <= o["loudness"][1]),
        ("ピーク（音割れ）", f"{loud['true_peak_dbfs']:.1f} dBTP", loud["true_peak_dbfs"] <= o["true_peak_max"]),
        ("キーフレーム間隔（最大）", f"{max(gaps) if gaps else 0:.2f}秒",
         bool(kf) and kf[0] == 0.0 and max(gaps) <= o["max_keyframe_gap_sec"] + 1e-6),
        ("faststart（先頭に目次）", "あり" if faststart(path) else "なし", faststart(path)),
        ("色の情報", f"{v.get('color_primaries', '?')}/{v.get('color_transfer', '?')}/{v.get('color_space', '?')}",
         v.get("color_space") == "bt709"),
    ]
    return {"ok": all(ok for _, _, ok in items),
            "items": [{"item": k, "value": val, "ok": bool(ok)} for k, val, ok in items], "loudness": loud}


def _source(series: Series, n: int, slot: str) -> dict[str, Any]:
    rec = load_records(series, n)
    vs = [v for v in rec["slots"].get(slot, {}).get("versions", []) if v.get("status") == "complete"]
    if not vs:
        raise FinalizeError(f"第{n}話の {slot} に完成済みの動画がありません")
    v = vs[-1]
    info = inspect_media(series.resolve(v["file"]))
    if info["sha256"] != v["sha256"]:
        raise FinalizeError(f"{v['file']} が記録と一致しません（restore で取り戻してください）")
    return v


def export(series: Series, n: int, *, source_slot: str = "roughcut", note: str = "") -> dict[str, Any]:
    if not shutil.which("ffmpeg"):
        raise FinalizeError("ffmpeg がありません")
    o = dict(DEFAULTS, **(series.config.get("publish_export") or {}))
    fmt = dict(series.format)
    with exclusive_lock(series.lock_path):
        src = _source(series, n, source_slot)
        fps = float(fmt.get("fps", 30))
        cond = {"kind": "final", "version": EXPORT_VERSION, "source": {"slot": source_slot, "sha256": src["sha256"]},
                "crf": o["crf"], "preset": o["preset"], "gop_sec": o["gop_sec"], "fps": fps}
        key = content_hash(cond)
        rec = load_records(series, n)
        slot = rec["slots"].setdefault("final", {"kind": "video", "versions": []})
        for v in slot["versions"]:
            if v.get("status") == "complete" and v["key"] == key and series.resolve(v["file"]).exists():
                if inspect_media(series.resolve(v["file"]))["sha256"] == v["sha256"]:
                    return {"action": "reuse", "version": v}
        out_dir = series.resolve(series.media_rel(n, "videos"))
        final = out_dir / f"ep{n:03d}-final-{key.split(':', 1)[1][:16]}.mp4"
        work = Path(tempfile.mkdtemp(prefix="final-", dir=out_dir))
        try:
            tmp = work / "out.mp4"
            gop = max(1, int(round(o["gop_sec"] * fps)))
            title = f"{series.config.get('title', series.id)} 第{n}話"
            _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(series.resolve(src["file"])),
                  "-map", "0:v:0", "-map", "0:a:0", "-map_metadata", "-1",
                  "-c:v", "libx264", "-preset", o["preset"], "-crf", str(o["crf"]), "-profile:v", "high",
                  "-pix_fmt", "yuv420p", "-r", f"{fps:g}", "-fps_mode", "cfr", "-g", str(gop), "-keyint_min", str(gop),
                  "-sc_threshold", "0", "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
                  "-c:a", "copy", "-movflags", "+faststart", "-metadata", f"title={title}", str(tmp)])
            result = check(tmp, fmt, o)
            info = inspect_media(tmp)
            os.replace(tmp, final)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        version = {"key": key, "status": "complete", "kind": "final", "role": "final_candidate", "conditions": cond,
                   "file": series.rel(final), "format": "mp4", "width": info["width"], "height": info["height"],
                   "duration": round(info["duration"], 3), "bytes": info["bytes"], "sha256": info["sha256"],
                   "source_file": src["file"], "checks": result, "note": note, "cost_usd": 0.0,
                   "created_at": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat(),
                   "storage": {"status": "local_only", "note": "persist で永続保存する"}}
        slot["versions"].append(version)
        save_records(series, n, rec)
        return {"action": "built", "version": version}
