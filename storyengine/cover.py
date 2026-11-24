"""投稿用の表紙（カバー画像）を、完成動画の1コマ＋文字で作る（無料。生成はしない）。

作品固有の値は編集指示 JSON（例: edits/ep001_covers.json）に書く:
{
  "source_slot": "final",
  "font": "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf",
  "covers": [{"name": "float", "time": 13.0, "shade": {"top": 0.55, "height": 0.32},
              "lines": [{"text": "午前0時の自販機", "size": 64, "y": 300, "color": "white"}]}]
}
- 文字は動画には入れない（表紙だけ）。y は 1920 の高さでの位置（上端）。x を省くと中央。
- 出力は media/epNNN/covers/cover-<name>-<条件キー>.png（1080x1920）。生成記録 "cover_<name>" に記録し、persist の対象。
- 確認用に、Instagram のプロフィール一覧（3:4）で切り取られる範囲を示した見本も作れる（preview=True）。
"""
from __future__ import annotations

import datetime as _dt
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .media import inspect_media, load_records, save_records
from .series import Series
from .storage import content_hash, exclusive_lock

COVER_VERSION = 1


class CoverError(RuntimeError):
    pass


def _ff(args: list[str]) -> None:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise CoverError(f"ffmpeg が失敗しました: {r.stderr[-400:]}")


def _source(series: Series, n: int, slot: str) -> dict[str, Any]:
    vs = [v for v in load_records(series, n)["slots"].get(slot, {}).get("versions", []) if v.get("status") == "complete"]
    if not vs:
        raise CoverError(f"第{n}話の {slot} に完成済みの動画がありません")
    return vs[-1]


def _filter(c: dict[str, Any], font: str, work: Path, w: int, h: int) -> str:
    parts = []
    sh = c.get("shade")
    if sh:  # 文字の後ろを上（または下）から暗くして読みやすくする
        top, hh = float(sh.get("top", 0.5)), float(sh.get("height", 0.3))
        y0 = int(sh.get("y", 0))
        span = int(hh * h)
        parts.append(f"geq=lum='lum(X,Y)*(1-{top}*clip(1-(Y-{y0})/{span},0,1)*gte(Y,{y0}))':"
                     f"cb='cb(X,Y)':cr='cr(X,Y)'")
    for i, ln in enumerate(c.get("lines") or []):
        tf = work / f"{c['name']}-{i}.txt"
        tf.write_text(ln["text"], encoding="utf-8")
        x = ln.get("x", "(w-text_w)/2")
        parts.append(f"drawtext=fontfile='{font}':textfile='{tf}':fontsize={ln.get('size', 64)}:"
                     f"fontcolor={ln.get('color', 'white')}:x={x}:y={ln['y']}:"
                     f"borderw={ln.get('border', 4)}:bordercolor={ln.get('border_color', 'black@0.55')}:"
                     f"shadowx=0:shadowy={ln.get('shadow', 4)}:shadowcolor=black@0.45")
    return ",".join(parts) if parts else "null"


def build(series: Series, n: int, spec: dict[str, Any], *, preview_dir: Path | None = None) -> list[dict[str, Any]]:
    if not shutil.which("ffmpeg"):
        raise CoverError("ffmpeg がありません")
    font = spec.get("font") or "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf"
    if not Path(font).exists():
        raise CoverError(f"字体がありません: {font}")
    results = []
    with exclusive_lock(series.lock_path):
        src = _source(series, n, spec.get("source_slot", "final"))
        src_path = series.resolve(src["file"])
        out_dir = series.resolve(series.media_rel(n, "covers"))
        out_dir.mkdir(parents=True, exist_ok=True)
        rec = load_records(series, n)
        work = Path(tempfile.mkdtemp(prefix="cover-", dir=out_dir))
        try:
            for c in spec["covers"]:
                cond = {"kind": "cover", "version": COVER_VERSION, "source_sha256": src["sha256"], "cover": c,
                        "font": Path(font).name}
                key = content_hash(cond)
                slot = rec["slots"].setdefault(f"cover_{c['name']}", {"kind": "image", "versions": []})
                hit = next((v for v in slot["versions"] if v.get("status") == "complete" and v["key"] == key
                            and series.resolve(v["file"]).exists()), None)
                if hit:
                    results.append({"action": "reuse", "version": hit})
                    continue
                final = out_dir / f"cover-{c['name']}-{key.split(':', 1)[1][:16]}.png"
                tmp = work / final.name
                _ff(["-ss", f"{float(c['time']):.3f}", "-i", str(src_path), "-frames:v", "1",
                     "-vf", "format=yuv444p," + _filter(c, font, work, src["width"], src["height"]) + ",format=rgb24",
                     str(tmp)])
                info = inspect_media(tmp)
                os.replace(tmp, final)
                v = {"key": key, "status": "complete", "kind": "cover", "conditions": cond, "file": series.rel(final),
                     "format": "png", "width": info["width"], "height": info["height"], "bytes": info["bytes"],
                     "sha256": info["sha256"], "cost_usd": 0.0, "source_file": src["file"],
                     "created_at": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat(),
                     "storage": {"status": "local_only", "note": "persist で永続保存する"}}
                slot["versions"].append(v)
                save_records(series, n, rec)
                results.append({"action": "built", "version": v})
        finally:
            shutil.rmtree(work, ignore_errors=True)
    if preview_dir is not None:
        preview(series, results, preview_dir)
    return results


def preview(series: Series, results: list[dict[str, Any]], out_dir: Path) -> Path:
    """表紙を横に並べ、3:4（プロフィール一覧）と 1:1 で切り取られる範囲を線で示した見本を作る。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    args, chains = [], []
    for i, r in enumerate(results):
        args += ["-i", str(series.resolve(r["version"]["file"]))]
        chains.append(f"[{i}]drawbox=x=0:y=240:w=1080:h=1440:c=yellow@0.9:t=6,"
                      f"drawbox=x=0:y=420:w=1080:h=1080:c=cyan@0.7:t=4,scale=360:-1[p{i}]")
    out = out_dir / "covers_preview.png"
    graph = ";".join(chains) + ";" + "".join(f"[p{i}]" for i in range(len(results))) + \
        f"hstack=inputs={len(results)}" if len(results) > 1 else chains[0].replace("[p0]", "")
    _ff([*args, "-filter_complex", graph, "-frames:v", "1", str(out)])
    return out
