"""既存の素材だけで、合成・音・編集を入れた仮編集の動画を作る。追加の生成・課金はしない。

作品固有の値（座標・時間・素材のパス）は連載ディレクトリの編集指示 JSON（例: edits/ep001_roughcut.json）に書く。
このモジュールは作品に依存しない処理だけを持つ。

編集指示の形（主な項目）:
- output: {width, height, fps}
- assets: 名前 → 下ごしらえ（すべて ffmpeg / 標準ライブラリで作る）
    {"op": "mask", "size": [w, h], "shapes": [{"ellipse": [cx, cy, rx, ry]}, {"rect": [x, y, w, h]}], "grow": px, "feather": px}
    {"op": "plate", "src": 画像, "mask": "@マスク"}         … マスク部分を周りの色で埋める（仮の背景）
    {"op": "cutout", "src": 画像, "mask": "@マスク"}        … マスク部分だけを切り抜く（透明つき）
    {"op": "crop", "src": 画像, "rect": [x, y, w, h]}
    {"op": "key", "src": 画像, "rect": [...], "color": "0xRRGGBB", "similarity": 0.1, "height": px} … 単色背景を抜く
    {"op": "glow", "radius": px, "color": [r, g, b]}
    {"op": "rainbow", "radius": px, "thickness": px}
    {"op": "rain", "size": [w, h], "density": 1.0, "seed": n}  … 縦につながる雨の模様
- shots: 順に並べるショット
    {"duration": 秒, "base": 画像, "camera": [[t, zoom, fx, fy], ...], "shake": [{"at", "dur", "amp"}],
     "layers": [...], "transition": {"type": "cut|fade|fadewhite|...", "duration": 秒}}
  layer: {"asset": 名前, "x": 値, "y": 値, "anchor": "center|topleft", "angle": 度, "opacity": 0〜1,
          "fade_in": [開始, 長さ], "fade_out": [開始, 長さ], "rain": {"asset", "speed", "dir": "down|up", "opacity", "inset"},
          "clip": [x, y, w, h]}
  x / y / angle は数値か、[[t, 値], ...] のキーフレーム（なめらかに補間）。時間はショットの先頭からの秒。
- sound: soundtrack.py の指示（効果音・環境音・音楽）

画像のパスは連載ディレクトリからの相対パス。"@名前" は assets の下ごしらえ結果。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import random
import shutil
import struct
import subprocess
import tempfile
import zlib
from pathlib import Path
from typing import Any

from .media import inspect_media, load_records, save_records
from .series import Series
from .storage import content_hash, exclusive_lock, read_json

ROUGHCUT_VERSION = 1
UPSCALE = 2


class RoughcutError(RuntimeError):
    pass


# ---------------- 小道具 ----------------
def _ff(args: list[str], cwd: Path | None = None) -> None:
    try:
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True,
                       capture_output=True, cwd=cwd)
    except subprocess.CalledProcessError as e:
        raise RoughcutError(f"ffmpeg が失敗しました: {(e.stderr or b'').decode(errors='replace')[-600:]}") from None


def write_png_rgba(path: Path, w: int, h: int, px: bytearray) -> None:
    raw = b"".join(b"\x00" + bytes(px[y * w * 4:(y + 1) * w * 4]) for y in range(h))

    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def rain_texture(path: Path, w: int, h: int, *, density: float = 1.0, seed: int = 1,
                 color: tuple[int, int, int] = (225, 238, 255)) -> None:
    """縦方向に継ぎ目なくつながる雨の模様（高さ 2h。上半分と下半分が同じ）。"""
    rnd = random.Random(seed)
    px = bytearray(w * 2 * h * 4)
    n = int(w * h / 700 * density)
    for _ in range(n):
        x, y0 = rnd.randrange(w), rnd.randrange(h)
        length, a_max = rnd.randint(10, 26), rnd.randint(110, 230)
        width = 2 if rnd.random() < 0.3 else 1
        for i in range(length):
            a = int(a_max * (i + 1) / length)  # 下（進む向き）ほど明るい
            for dx in range(width):
                xx = (x + dx) % w
                for copy in (0, h):
                    yy = (y0 + i) % h + copy
                    o = (yy * w + xx) * 4
                    if px[o + 3] < a:
                        px[o:o + 4] = bytes((*color, a))
    write_png_rgba(path, w, 2 * h, px)


def kf(v: Any, var: str = "t", scale: float = 1.0) -> str:
    """数値かキーフレーム [[t, 値], ...] を ffmpeg の式にする（区間ごとになめらかに補間）。"""
    if isinstance(v, (int, float)):
        return repr(float(v) * scale)
    pts = sorted((float(t), float(x) * scale) for t, x in v)
    expr = repr(pts[-1][1])
    for (t0, v0), (t1, v1) in reversed(list(zip(pts, pts[1:]))):
        p = f"clip(({var}-{t0})/{max(t1 - t0, 1e-6)},0,1)"
        seg = f"({v0}+({v1 - v0})*{p}*{p}*(3-2*{p}))"
        expr = f"if(lt({var},{t1}),{seg},{expr})"
    return expr


# ---------------- 下ごしらえ ----------------
class Assets:
    def __init__(self, series: Series, spec: dict[str, Any], work: Path) -> None:
        self.series, self.spec, self.work = series, spec.get("assets") or {}, work
        self.done: dict[str, Path] = {}

    def path(self, ref: str) -> Path:
        if ref.startswith("@"):
            return self.get(ref[1:])
        p = self.series.resolve(ref)
        if not p.exists():
            raise RoughcutError(f"素材がありません: {ref}")
        return p

    def get(self, name: str) -> Path:
        if name in self.done:
            return self.done[name]
        a = self.spec.get(name)
        if a is None:
            raise RoughcutError(f"下ごしらえ {name} が定義されていません")
        out = self.work / f"asset-{name}.png"
        op = a["op"]
        if op == "mask":
            w, h = a["size"]
            g = float(a.get("grow", 0))
            terms = []
            for s in a["shapes"]:
                if "ellipse" in s:
                    cx, cy, rx, ry = s["ellipse"]
                    terms.append(f"lte(pow((X-{cx})/{rx + g},2)+pow((Y-{cy})/{ry + g},2),1)")
                elif "rect" in s:
                    x, y, rw, rh = s["rect"]
                    terms.append(f"between(X,{x - g},{x + rw + g})*between(Y,{y - g},{y + rh + g})")
            expr = "255*gt(" + "+".join(terms) + ",0)"
            vf = f"geq=lum='{expr}'"
            if a.get("feather"):
                vf += f",gblur=sigma={a['feather']}"
            _ff(["-f", "lavfi", "-i", f"color=black:s={w}x{h}", "-vf", vf + ",format=gray", "-frames:v", "1", str(out)])
        elif op == "fill":  # マスク部分を周りの色でなめらかに埋める（正規化したぼかし）。仮の背景
            sig = float(a.get("sigma", 60))
            _ff(["-i", str(self.path(a["src"])), "-i", str(self.path(a["mask"])), "-filter_complex",
                 f"[1]format=gray,negate[inv];[0]format=rgba[s];[s][inv]alphamerge,format=rgba64le,split[a][keep];"
                 f"[a]premultiply=inplace=1,gblur=sigma={sig}:steps=3,unpremultiply=inplace=1,format=rgba64le[f];"
                 f"[f][keep]overlay=format=auto,format=rgb24,noise=alls=6:allf=t",
                 "-frames:v", "1", str(out)])
            for k, pt in enumerate(a.get("patches") or []):  # 同じ画像の別の場所から模様を写す（ふちはぼかす）
                fx, fy, fw, fh = pt["from"]
                tx, ty = pt["to"]
                f = float(pt.get("feather", 24))
                nxt = self.work / f"asset-{name}-p{k}.png"
                _ff(["-i", str(out), "-i", str(self.path(a["src"])), "-filter_complex",
                     f"[1]crop={fw}:{fh}:{fx}:{fy},format=rgba,"
                     f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='255*min(1,min(min(X,Y),min(W-1-X,H-1-Y))/{f})'[p];"
                     f"[0][p]overlay={tx}:{ty}:format=auto,format=rgb24", "-frames:v", "1", str(nxt)])
                os.replace(nxt, out)
        elif op == "plate":
            _ff(["-i", str(self.path(a["src"])), "-vf", f"removelogo=f={self.path(a['mask'])}", "-frames:v", "1", str(out)])
        elif op == "cutout":
            _ff(["-i", str(self.path(a["src"])), "-i", str(self.path(a["mask"])), "-filter_complex",
                 "[1]format=gray[m];[0]format=rgba[s];[s][m]alphamerge,format=rgba", "-frames:v", "1", str(out)])
        elif op == "crop":
            x, y, w, h = a["rect"]
            vf = f"crop={w}:{h}:{x}:{y},format=rgba" + (",vflip" if a.get("vflip") else "")
            if a.get("height"):
                vf += f",scale=-2:{a['height']}:flags=lanczos"
            if a.get("tint"):  # 光の色に合わせる（色ごとの倍率）
                tr, tg, tb = a["tint"]
                vf += f",colorchannelmixer=rr={tr}:gg={tg}:bb={tb}"
            _ff(["-i", str(self.path(a["src"])), "-vf", vf, "-frames:v", "1", str(out)])
        elif op == "blank":
            w, h = a["size"]
            _ff(["-f", "lavfi", "-i", f"color=black@0.0:s={w}x{h},format=rgba", "-frames:v", "1", str(out)])
        elif op == "key":
            x, y, w, h = a["rect"]
            vf = (f"crop={w}:{h}:{x}:{y},format=rgba,colorkey={a['color']}:{a.get('similarity', 0.1)}:"
                  f"{a.get('blend', 0.05)}")
            if a.get("height"):
                vf += f",scale=-2:{a['height']}:flags=lanczos"
            _ff(["-i", str(self.path(a["src"])), "-vf", vf, "-frames:v", "1", str(out)])
        elif op == "glow":
            r = int(a["radius"])
            ry = int(a.get("ry", r))
            cr, cg, cb = a.get("color", [255, 190, 90])
            peak = float(a.get("alpha", 1.0)) * 255
            _ff(["-f", "lavfi", "-i", f"color=black:s={2 * r}x{2 * ry}", "-vf",
                 f"format=rgba,geq=r={cr}:g={cg}:b={cb}:"
                 f"a='{peak}*exp(-pow((X-{r})/({r}*0.45),2)-pow((Y-{ry})/({ry}*0.45),2))'",
                 "-frames:v", "1", str(out)])
        elif op == "rainbow":
            r, th = int(a["radius"]), float(a["thickness"])
            cols = [(235, 80, 80), (245, 160, 70), (245, 225, 90), (110, 200, 110), (90, 150, 230), (150, 100, 210)]
            band = f"floor(({r}-hypot(X-{r},Y-{r}))/({th}/6))"

            def ch(i: int) -> str:
                e = "0"
                for k in reversed(range(6)):
                    e = f"if(eq(B,{k}),{cols[k][i]},{e})"
                return e.replace("B", band)
            alpha = f"if(between({band},0,5)*lt(Y,{r}),215,0)"
            _ff(["-f", "lavfi", "-i", f"color=black:s={2 * r}x{r + 2}", "-vf",
                 f"format=rgba,geq=r='{ch(0)}':g='{ch(1)}':b='{ch(2)}':a='{alpha}',gblur=sigma=1.2",
                 "-frames:v", "1", str(out)])
        elif op == "rain":
            w, h = a["size"]
            rain_texture(out, int(w), int(h), density=float(a.get("density", 1.0)), seed=int(a.get("seed", 1)))
        else:
            raise RoughcutError(f"知らない下ごしらえ: {op}")
        self.done[name] = out
        return out

    def size(self, name_or_path: str | Path) -> tuple[int, int]:
        p = self.path(name_or_path) if isinstance(name_or_path, str) else name_or_path
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(p)],
                           check=True, capture_output=True, text=True)
        w, h = r.stdout.strip().split("\n")[0].split(",")
        return int(w), int(h)


# ---------------- ショット ----------------
def _camera(cam: list[list[float]], shake: list[dict[str, float]], fps: int, out_w: int, out_h: int) -> str:
    t = f"(in/{fps})"
    z = kf([[k[0], k[1]] for k in cam], t)
    fx = kf([[k[0], k[2]] for k in cam], t)
    fy = kf([[k[0], k[3]] for k in cam], t)
    sx = sy = "0"
    for s in shake or []:
        d = f"({t}-{s['at']})"
        env = f"(gte({d},0)*lte({d},{s['dur']})*exp(-{d}*{6 / s['dur']}))"
        sx += f"+{s['amp']}*{UPSCALE}*{env}*sin(2*PI*23*{d})"
        sy += f"+{s['amp']}*{UPSCALE}*{env}*sin(2*PI*31*{d}+1)"
    x = f"clip({fx}*iw-iw/zoom/2+({sx}),0,iw-iw/zoom)"
    y = f"clip({fy}*ih-ih/zoom/2+({sy}),0,ih-ih/zoom)"
    return f"zoompan=z='{z}':x='{x}':y='{y}':d=1:s={out_w}x{out_h}:fps={fps}"


def render_shot(shot: dict[str, Any], assets: Assets, out: Path, *, fps: int, out_w: int, out_h: int,
                extra: float) -> None:
    dur = float(shot["duration"]) + extra
    base = assets.path(shot["base"])
    bw, bh = assets.size(base)
    inputs = ["-loop", "1", "-framerate", str(fps), "-t", f"{dur:.4f}", "-i", str(base)]
    graph, last, idx = [], "[0:v]", 1
    graph.append(f"{last}format=rgba,setsar=1[b0]")
    last = "[b0]"
    for li, layer in enumerate(shot.get("layers") or []):
        src = assets.path("@" + layer["asset"])
        inputs += ["-loop", "1", "-framerate", str(fps), "-t", f"{dur:.4f}", "-i", str(src)]
        lab = f"[{idx}:v]"
        idx += 1
        chain = [f"{lab}format=rgba,setsar=1"]
        cur = f"[l{li}a]"
        graph.append(",".join(chain) + cur)
        if layer.get("rain"):
            rn = layer["rain"]
            tex = assets.path("@" + rn["asset"])
            tw, th2 = assets.size(tex)
            th = th2 // 2
            inputs += ["-loop", "1", "-framerate", str(fps), "-t", f"{dur:.4f}", "-i", str(tex)]
            rl = f"[{idx}:v]"
            idx += 1
            speed = float(rn.get("speed", 500))
            yexpr = f"{th}-mod(t*{speed},{th})" if rn.get("dir", "down") == "down" else f"mod(t*{speed},{th})"
            op = float(rn.get("opacity", 0.6))
            ins = int(rn.get("inset", 0))
            graph.append(f"{rl}format=rgba,crop={tw}:{th}:0:'{yexpr}',colorchannelmixer=aa={op}[l{li}r]")
            graph.append(f"{cur}[l{li}r]overlay={ins}:{ins}:format=auto[l{li}b]")
            cur = f"[l{li}b]"
        post = []
        if "angle" in layer:
            ang = kf(layer["angle"], "t", 3.141592653589793 / 180)
            post.append(f"rotate=a='{ang}':ow='hypot(iw,ih)':oh=ow:c=none")
        if layer.get("opacity", 1) != 1:
            post.append(f"colorchannelmixer=aa={layer['opacity']}")
        if layer.get("fade_in"):
            st, d = layer["fade_in"]
            post.append(f"fade=t=in:st={st}:d={d}:alpha=1")
        if layer.get("fade_out"):
            st, d = layer["fade_out"]
            post.append(f"fade=t=out:st={st}:d={d}:alpha=1")
        if post:
            graph.append(f"{cur}{','.join(post)}[l{li}p]")
            cur = f"[l{li}p]"
        center = layer.get("anchor", "topleft") == "center" or "angle" in layer
        x = kf(layer.get("x", 0))
        y = kf(layer.get("y", 0))
        ox = f"({x})-w/2" if center else x
        oy = f"({y})-h/2" if center else y
        if layer.get("clip"):
            cx, cy, cw, ch = layer["clip"]
            graph.append(f"color=black@0:s={cw}x{ch}:r={fps}:d={dur:.4f},format=rgba[l{li}c]")
            graph.append(f"[l{li}c]{cur}overlay=x='({ox})-{cx}':y='({oy})-{cy}':format=auto[l{li}q]")
            graph.append(f"{last}[l{li}q]overlay={cx}:{cy}:format=auto[o{li}]")
        else:
            graph.append(f"{last}{cur}overlay=x='{ox}':y='{oy}':format=auto[o{li}]")
        last = f"[o{li}]"
    cam = shot.get("camera") or [[0, 1.0, 0.5, 0.5], [dur, 1.0, 0.5, 0.5]]
    graph.append(f"{last}format=rgb24,scale={bw * UPSCALE}:{bh * UPSCALE}:flags=lanczos,"
                 f"{_camera(cam, shot.get('shake') or [], fps, out_w, out_h)},setsar=1,format=yuv420p[v]")
    _ff([*inputs, "-filter_complex", ";".join(graph), "-map", "[v]", "-t", f"{dur:.4f}", "-r", str(fps),
         "-c:v", "libx264", "-preset", "medium", "-crf", "14", "-pix_fmt", "yuv420p", str(out)])


def _join(parts: list[tuple[Path, float, dict[str, Any] | None]], out: Path, fps: int) -> None:
    """ショットをつなぐ。各ショットは次の切り替えの長さだけ長めに作ってある。"""
    inputs, graph = [], []
    for p, _, _ in parts:
        inputs += ["-i", str(p)]
    last, acc = "[0:v]", 0.0
    for i in range(1, len(parts)):
        acc += parts[i - 1][1]
        tr = parts[i - 1][2] or {"type": "cut"}
        typ = tr.get("type", "cut")
        d = float(tr.get("duration", 1 / fps)) if typ != "cut" else 1 / fps
        typ = "fade" if typ == "cut" else typ
        graph.append(f"{last}[{i}:v]xfade=transition={typ}:duration={d:.4f}:offset={acc:.4f}[x{i}]")
        last = f"[x{i}]"
    if not graph:
        shutil.copy(parts[0][0], out)
        return
    _ff([*inputs, "-filter_complex", ";".join(graph), "-map", last, "-r", str(fps), "-c:v", "libx264",
         "-preset", "medium", "-crf", "16", "-pix_fmt", "yuv420p", str(out)])


# ---------------- 全体 ----------------
def load_spec(series: Series, spec_path: str | Path) -> dict[str, Any]:
    p = Path(spec_path)
    if not p.is_absolute():
        p = series.resolve(str(spec_path)) if not p.exists() else p
    if not p.exists():
        raise RoughcutError(f"編集指示がありません: {spec_path}")
    return read_json(p)


def plan(series: Series, n: int, spec: dict[str, Any]) -> dict[str, Any]:
    files: dict[str, str] = {}

    def collect(v: Any) -> None:
        if isinstance(v, str) and not v.startswith("@") and v.lower().endswith((".png", ".jpg", ".jpeg")):
            files[v] = inspect_media(series.resolve(v))["sha256"]
        elif isinstance(v, dict):
            for x in v.values():
                collect(x)
        elif isinstance(v, list):
            for x in v:
                collect(x)
    collect(spec)
    cond = {"kind": "roughcut", "version": ROUGHCUT_VERSION, "episode": n, "spec": spec, "inputs": files}
    total = sum(float(s["duration"]) for s in spec["shots"])
    return {"key": content_hash(cond), "conditions": cond, "total_sec": total}


def build(series: Series, n: int, spec: dict[str, Any]) -> dict[str, Any]:
    from . import soundtrack
    if not shutil.which("ffmpeg"):
        raise RoughcutError("ffmpeg がありません")
    o = spec.get("output") or {}
    fps, ow, oh = int(o.get("fps", 30)), int(o.get("width", 1080)), int(o.get("height", 1920))
    with exclusive_lock(series.lock_path):
        pl = plan(series, n, spec)
        rec = load_records(series, n)
        slot = rec["slots"].setdefault("roughcut", {"kind": "video", "versions": []})
        for v in slot["versions"]:
            if v.get("status") == "complete" and v["key"] == pl["key"] and series.resolve(v["file"]).exists():
                if inspect_media(series.resolve(v["file"]))["sha256"] == v["sha256"]:
                    return {"action": "reuse", "version": v}
        out_dir = series.resolve(series.media_rel(n, "videos"))
        out_dir.mkdir(parents=True, exist_ok=True)
        final = out_dir / f"roughcut-{pl['key'].split(':', 1)[1][:16]}.mp4"
        work = Path(tempfile.mkdtemp(prefix="roughcut-", dir=out_dir))
        try:
            assets = Assets(series, spec, work)
            parts = []
            shots = spec["shots"]
            for i, shot in enumerate(shots):
                tr = shot.get("transition")
                extra = float(tr.get("duration", 0)) if tr and tr.get("type", "cut") != "cut" and i < len(shots) - 1 \
                    else (1 / fps if i < len(shots) - 1 else 0)
                p = work / f"shot{i:02d}.mp4"
                render_shot(shot, assets, p, fps=fps, out_w=ow, out_h=oh, extra=extra)
                parts.append((p, float(shot["duration"]), tr))
            video = work / "video.mp4"
            _join(parts, video, fps)
            wav = work / "sound.wav"
            soundtrack.render(spec.get("sound") or {}, wav, duration=pl["total_sec"])
            tmp = work / "out.mp4"
            _ff(["-i", str(video), "-i", str(wav), "-map", "0:v", "-map", "1:a", "-t", f"{pl['total_sec']:.3f}",
                 "-c:v", "copy", "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                 "-movflags", "+faststart", "-metadata", "comment=roughcut (temporary edit for review)", str(tmp)])
            info = inspect_media(tmp)
            if (info["width"], info["height"]) != (ow, oh) or abs(info["duration"] - pl["total_sec"]) > 0.25:
                raise RoughcutError(f"出力が想定と違います（{info['width']}x{info['height']}、{info['duration']:.2f}秒）")
            os.replace(tmp, final)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        version = {"key": pl["key"], "status": "complete", "kind": "roughcut", "conditions": pl["conditions"],
                   "file": series.rel(final), "format": "mp4", "width": info["width"], "height": info["height"],
                   "duration": round(info["duration"], 3), "bytes": info["bytes"], "sha256": info["sha256"],
                   "cost_usd": 0.0, "created_at": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat(),
                   "storage": {"status": "local_only", "note": "persist で永続保存する"}}
        slot["versions"].append(version)
        save_records(series, n, rec)
        return {"action": "built", "version": version}


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1)
