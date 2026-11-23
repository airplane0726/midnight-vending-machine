"""採用済みの開始画像だけで、構成確認用の動画（アニマティック）を作る。追加の生成・課金はしない。

- 各カットの長さは episode.json の duration_sec。画像は生成記録で採用中（再利用できる）の開始画像。
- カメラの動きは episode.json のカットの `animatic`（任意）で指定する:
    {"zoom": [開始, 終了], "focus": [[x, y], [x, y]], "note": "..."}
  focus は画面の中心にする位置（画像の幅・高さに対する 0〜1）。未指定なら中央へのゆっくりした寄り。
- 字幕・せりふは入れない。音声は無音（投稿先との互換のため無音の音声トラックだけ付ける）。
- 出力は media/epNNN/videos/animatic-<条件キー>.mp4。生成記録の "animatic" に記録し、persist / restore の対象になる。
"""
from __future__ import annotations

import datetime as _dt
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .media import inspect_media, load_records, plan_images, save_records
from .series import Series
from .state import load_episodes
from .storage import content_hash, exclusive_lock

FPS = 30
OUT_W, OUT_H = 1080, 1920
UPSCALE = 4  # 拡大してから切り出し、ゆっくりした動きのガタつきを抑える
ANIMATIC_VERSION = 1
DEFAULT_MOVE = {"zoom": [1.0, 1.06], "focus": [[0.5, 0.5], [0.5, 0.5]]}


class AnimaticError(RuntimeError):
    pass


def _move(cut: dict[str, Any]) -> dict[str, Any]:
    m = dict(DEFAULT_MOVE)
    m.update({k: v for k, v in (cut.get("animatic") or {}).items() if k in ("zoom", "focus")})
    z0, z1 = (float(v) for v in m["zoom"])
    (fx0, fy0), (fx1, fy1) = ((float(a), float(b)) for a, b in m["focus"])
    if min(z0, z1) < 1.0 or max(z0, z1) > 3.0:
        raise AnimaticError("zoom は 1.0〜3.0 の範囲で指定してください")
    for v in (fx0, fy0, fx1, fy1):
        if not 0.0 <= v <= 1.0:
            raise AnimaticError("focus は 0〜1 の範囲で指定してください")
    return {"zoom": [z0, z1], "focus": [[fx0, fy0], [fx1, fy1]]}


def _zoompan(move: dict[str, Any], frames: int) -> str:
    z0, z1 = move["zoom"]
    (fx0, fy0), (fx1, fy1) = move["focus"]
    n = max(frames - 1, 1)
    e = f"(on/{n})*(on/{n})*(3-2*(on/{n}))"  # なめらかに始まって止まる
    z = f"({z0}+({z1}-{z0})*{e})"
    x = f"clip(({fx0}+({fx1}-{fx0})*{e})*iw-iw/zoom/2,0,iw-iw/zoom)"
    y = f"clip(({fy0}+({fy1}-{fy0})*{e})*ih-ih/zoom/2,0,ih-ih/zoom)"
    return (f"scale={OUT_W * UPSCALE}:{OUT_H * UPSCALE}:flags=lanczos,"
            f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={OUT_W}x{OUT_H}:fps={FPS},"
            f"setsar=1,format=yuv420p")


def plan(series: Series, n: int) -> dict[str, Any]:
    eps = {e["episode_number"]: e for e in load_episodes(series)}
    if n not in eps:
        raise AnimaticError(f"第{n}話は登録されていません")
    ep = eps[n]
    plans = {p.cut_number: p for p in plan_images(series, n)[0]}
    cuts = []
    for cut in ep["cuts"]:
        p = plans.get(cut["cut_number"])
        if not p or p.action != "reuse":
            raise AnimaticError(f"カット{cut['cut_number']}の採用済みの開始画像がありません（{p.action if p else 'なし'}）")
        cuts.append({"cut_number": cut["cut_number"], "duration_sec": cut["duration_sec"],
                     "image": p.current["file"], "image_sha256": p.current["sha256"], "move": _move(cut)})
    cond = {"kind": "animatic", "version": ANIMATIC_VERSION, "fps": FPS, "size": f"{OUT_W}x{OUT_H}",
            "fade": {"in": 0.3, "out": 0.5}, "cuts": cuts}
    return {"key": content_hash(cond), "conditions": cond,
            "total_sec": sum(c["duration_sec"] for c in cuts)}


def build(series: Series, n: int, *, runner=subprocess.run) -> dict[str, Any]:
    """動画を作って生成記録に登録する。同じ条件の完成品があれば作らずに再利用する。"""
    if not shutil.which("ffmpeg"):
        raise AnimaticError("ffmpeg がありません")
    with exclusive_lock(series.lock_path):
        pl = plan(series, n)
        rec = load_records(series, n)
        slot = rec["slots"].setdefault("animatic", {"kind": "video", "versions": []})
        for v in slot["versions"]:
            if v.get("status") == "complete" and v["key"] == pl["key"]:
                try:
                    info = inspect_media(series.resolve(v["file"]))
                    if info["sha256"] == v["sha256"]:
                        return {"action": "reuse", "version": v}
                except Exception:  # noqa: BLE001 - 壊れていれば作り直す（無料）
                    pass
        out_dir = series.resolve(series.media_rel(n, "videos"))
        out_dir.mkdir(parents=True, exist_ok=True)
        final = out_dir / f"animatic-{pl['key'].split(':', 1)[1][:16]}.mp4"
        work = Path(tempfile.mkdtemp(prefix="animatic-", dir=out_dir))
        try:
            parts = []
            for c in pl["conditions"]["cuts"]:
                frames = int(round(c["duration_sec"] * FPS))
                part = work / f"cut{c['cut_number']:02d}.mp4"
                runner(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(series.resolve(c["image"])),
                        "-vf", _zoompan(c["move"], frames), "-frames:v", str(frames),
                        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", str(part)],
                       check=True, capture_output=True)
                parts.append(part)
            listing = work / "list.txt"
            listing.write_text("".join(f"file '{p.name}'\n" for p in parts))
            total = pl["total_sec"]
            tmp = work / "out.mp4"
            runner(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(listing), "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                    "-vf", f"fade=t=in:st=0:d=0.3,fade=t=out:st={total - 0.5}:d=0.5",
                    "-t", str(total), "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
                    "-r", str(FPS), "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart",
                    "-metadata", "comment=animatic (structure check only)", str(tmp)],
                   check=True, capture_output=True, cwd=work)
            info = inspect_media(tmp)
            if (info["width"], info["height"]) != (OUT_W, OUT_H) or abs(info["duration"] - total) > 0.25:
                raise AnimaticError(f"出力が想定と違います（{info['width']}x{info['height']}、{info['duration']:.2f}秒）")
            os.replace(tmp, final)
        except subprocess.CalledProcessError as e:
            raise AnimaticError(f"ffmpeg が失敗しました: {(e.stderr or b'').decode(errors='replace')[-300:]}") from None
        finally:
            shutil.rmtree(work, ignore_errors=True)
        version = {"key": pl["key"], "status": "complete", "kind": "animatic", "conditions": pl["conditions"],
                   "file": series.rel(final), "format": "mp4", "width": info["width"], "height": info["height"],
                   "duration": round(info["duration"], 3), "bytes": info["bytes"], "sha256": info["sha256"],
                   "cost_usd": 0.0, "created_at": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat(),
                   "storage": {"status": "local_only", "note": "persist で永続保存する"}}
        slot["versions"].append(version)
        save_records(series, n, rec)
        return {"action": "built", "version": version}
