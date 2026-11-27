"""公開準備の共通処理：作者ブランド・文章の型・作者ロゴの重ね・投稿パッケージ・承認台帳と関門。

作品に依存しない。作品固有の値は次に置く:
- リポジトリ直下 brand/brand.json … 作者ブランド（全作品共通。名前が未確定なら status: "provisional"）
- brand/templates/*.md … 作者プロフィール（日本語・英語）
- series/<id>/publish/config.json … 投稿先・ロゴの位置・表紙と静止画の既定
- series/<id>/publish/templates/*.md … 作品紹介・各投稿先の文章の型
- series/<id>/edits/epNNN_publish.json … その話の表紙・静止画の時刻・差し込む文
- series/<id>/publish/ledger.json … 承認台帳（自動で更新。承認・投稿の記録）

文章の型の書き方: {{brand.name_ja}}, {{episode.title}}, {{posts.instagram.hashtags_line}} のように
ドットで値を指す。値がない（未取得の URL など）ときは {{brand.links.site|準備中}} のように | の後ろを使う。
| がなく値もないときは誤りにする（存在しない URL やアカウントを作らないため）。

関門:
- 予算（budget.check）と、前の話の公開状態（台帳）を、有料の生成・次の話の登録の前に確認する。
- 承認は「その時点の投稿版ファイルの sha256」に結びつける。ファイルが変わったら承認は無効。
- 同じ話を同じ投稿先へ二度記録しない（二重投稿の防止）。仮のブランド名のままでは承認できない。
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
from .state import load_episodes
from .storage import atomic_write_json, atomic_write_text, content_hash, exclusive_lock, read_json

PUBLISH_VERSION = 1
DONE_STATUSES = ("published", "withdrawn")
FONT_DEFAULT = "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf"


class PublishError(RuntimeError):
    pass


class GateError(PublishError):
    pass


# ---------------- ブランド・文章の型 ----------------
def repo_root(series: Series) -> Path:
    p = series.root.resolve()
    for cand in [p, *p.parents]:
        if (cand / "brand" / "brand.json").exists():
            return cand
    return p.parents[1]


def load_brand(series: Series) -> dict[str, Any]:
    """作者ブランド。STORYENGINE_BRAND_FILE があればそちらを読む（確定前の見本づくり用。保存物の名前は変えない）。"""
    override = os.environ.get("STORYENGINE_BRAND_FILE")
    path = Path(override) if override else repo_root(series) / "brand" / "brand.json"
    if not path.exists():
        raise PublishError(f"{path} がありません")
    b = read_json(path)
    b.setdefault("display_name", b.get("name_ja"))
    b.setdefault("display_name_en", b.get("name_en"))
    return b


def publish_config(series: Series) -> dict[str, Any]:
    p = series.root / "publish" / "config.json"
    return read_json(p) if p.exists() else {}


_TOKEN = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*(?:\|([^}]*))?\}\}")


def _lookup(ctx: dict[str, Any], path: str) -> Any:
    cur: Any = ctx
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def fill(text: str, ctx: dict[str, Any]) -> str:
    """{{a.b}} を値で置き換える。値がなければ {{a.b|代わり}} の代わりを使い、それもなければ誤り。"""
    missing = []

    def rep(m: re.Match) -> str:
        v = _lookup(ctx, m.group(1))
        if v in (None, "", []):
            if m.group(2) is not None:
                return m.group(2)
            missing.append(m.group(1))
            return m.group(0)
        if isinstance(v, list):
            return " ".join(str(x) for x in v)
        return str(v)
    out = _TOKEN.sub(rep, text)
    if missing:
        raise PublishError(f"文章の型に値がありません: {', '.join(sorted(set(missing)))}")
    return out


def context(series: Series, n: int | None = None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    brand = load_brand(series)
    links = brand.get("links") or {}
    brand = dict(brand, links_line_ja=_links_line(links, "ja"), links_line_en=_links_line(links, "en"))
    cfg = series.config
    ctx: dict[str, Any] = {"brand": brand, "series": {
        "id": series.id, "title": cfg.get("title"), "title_en": cfg.get("title_en"), "premise": cfg.get("premise"),
        "series_arc": cfg.get("series_arc"), "tone": cfg.get("tone"), "ai_disclosure": cfg.get("ai_disclosure"),
        "characters": cfg.get("characters"), "intro_en": (publish_config(series).get("intro_en") or {})},
        "publish": publish_config(series)}
    if n is not None:
        ep = episode(series, n)
        posts = json.loads(json.dumps(ep.get("posts") or {}))
        drop = set((extra or {}).get("package", {}).get("hashtags_remove") or [])
        for p in posts.values():
            if isinstance(p, dict) and isinstance(p.get("hashtags"), list):
                p["hashtags"] = [h for h in p["hashtags"] if h not in drop]
                p["hashtags_line"] = " ".join(p["hashtags"])
            if isinstance(p, dict) and isinstance(p.get("description"), str):
                # 説明の本文（AI 表示の行「※…」は型の側で入れ直すので除く）
                p["description_body"] = "\n".join(
                    ln for ln in p["description"].splitlines() if not ln.startswith("※")).strip()
            if isinstance(p, dict) and isinstance(p.get("tags"), list):
                p["tags_line"] = ", ".join(p["tags"])
        ctx["episode"] = {"number": n, "number_padded": f"{n:03d}", "title": ep["title"], "logline": ep.get("logline"),
                          "summary": ep.get("summary"), "featured_object": ep.get("featured_object"),
                          "ai_disclosure": ep.get("ai_disclosure") or cfg.get("ai_disclosure")}
        ctx["posts"] = posts
    if extra:
        ctx.update(extra)
    return ctx


def _links_line(links: dict[str, Any], lang: str) -> str:
    names = {"youtube": "YouTube", "instagram": "Instagram", "tiktok": "TikTok", "note": "note", "x": "X", "site": "Web"}
    pending = "準備中" if lang == "ja" else "coming soon"
    return " / ".join(f"{names.get(k, k)}: {v if v else pending}" for k, v in links.items())


def episode(series: Series, n: int) -> dict[str, Any]:
    for e in load_episodes(series):
        if e["episode_number"] == n:
            return e
    raise PublishError(f"第{n}話は登録されていません")


def render_template(path: Path, ctx: dict[str, Any]) -> str:
    return fill(path.read_text(encoding="utf-8"), ctx)


# ---------------- 作者ロゴ（文字）と投稿版の動画 ----------------
def logo_png(brand: dict[str, Any], out: Path, *, font: str | None = None) -> Path:
    """作者名の文字だけで小さなロゴ画像（透明背景）を作る。画像生成は使わない。"""
    lg = brand.get("logo") or {}
    text = fill(lg.get("text", "{{brand.name_en}}"), {"brand": brand})
    size = int(lg.get("font_size", 34))
    f = font or lg.get("font") or FONT_DEFAULT
    with tempfile.TemporaryDirectory() as d:
        tf = Path(d) / "t.txt"
        tf.write_text(text, encoding="utf-8")
        w = max(120, int(size * 0.75 * len(text)) + 40)
        h = int(size * 1.8)
        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                            "-i", f"color=black@0.0:s={w}x{h},format=rgba", "-frames:v", "1", "-vf",
                            f"drawtext=fontfile='{f}':textfile='{tf}':fontsize={size}:fontcolor={lg.get('color', 'white')}:"
                            f"x=(w-text_w)/2:y=(h-text_h)/2:shadowx=0:shadowy=2:shadowcolor=black@0.5", str(out)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise PublishError(f"ロゴを作れませんでした: {r.stderr[-300:]}")
    return out


def _latest(series: Series, n: int, slot: str) -> dict[str, Any]:
    vs = [v for v in load_records(series, n)["slots"].get(slot, {}).get("versions", []) if v.get("status") == "complete"]
    if not vs:
        raise PublishError(f"第{n}話の {slot} に完成品がありません")
    return vs[-1]


def post_video(series: Series, n: int) -> dict[str, Any]:
    """ロゴなしの原版（生成記録 final）に小さな作者ロゴを重ね、投稿版（post）として記録する。原版は残す。"""
    from . import finalize
    brand = load_brand(series)
    lg = dict(brand.get("logo") or {})
    pos = dict((publish_config(series).get("logo") or {}), **(lg.get("position") or {}))
    with exclusive_lock(series.lock_path):
        master = _latest(series, n, "final")
        mpath = series.resolve(master["file"])
        if inspect_media(mpath)["sha256"] != master["sha256"]:
            raise PublishError(f"{master['file']} が記録と一致しません（restore で取り戻してください）")
        cond = {"kind": "post", "version": PUBLISH_VERSION, "master_sha256": master["sha256"],
                "logo": {"text": fill(lg.get("text", "{{brand.name_en}}"), {"brand": brand}),
                         "font_size": lg.get("font_size", 34), "opacity": lg.get("opacity", 0.7), "position": pos},
                "brand_status": brand.get("status")}
        key = content_hash(cond)
        rec = load_records(series, n)
        slot = rec["slots"].setdefault("post", {"kind": "video", "versions": []})
        for v in slot["versions"]:
            if v.get("status") == "complete" and v["key"] == key and series.resolve(v["file"]).exists():
                return {"action": "reuse", "version": v}
        out_dir = mpath.parent
        final = out_dir / f"ep{n:03d}-post-{key.split(':', 1)[1][:16]}.mp4"
        work = Path(tempfile.mkdtemp(prefix="post-", dir=out_dir))
        try:
            logo = logo_png(brand, work / "logo.png")
            x = pos.get("x", "W-w-48")
            y = pos.get("y", "230")
            tmp = work / "out.mp4"
            o = finalize.DEFAULTS
            r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(mpath), "-i", str(logo),
                                "-filter_complex",
                                f"[1]format=rgba,colorchannelmixer=aa={cond['logo']['opacity']}[l];[0:v][l]overlay={x}:{y}",
                                "-map", "0:a:0", "-map_metadata", "-1", "-c:v", "libx264", "-preset", o["preset"],
                                "-crf", str(o["crf"]), "-profile:v", "high", "-pix_fmt", "yuv420p", "-g", "30",
                                "-keyint_min", "30", "-sc_threshold", "0", "-color_primaries", "bt709",
                                "-color_trc", "bt709", "-colorspace", "bt709", "-c:a", "copy",
                                "-movflags", "+faststart", str(tmp)], capture_output=True, text=True)
            if r.returncode != 0:
                raise PublishError(f"投稿版を作れませんでした: {r.stderr[-300:]}")
            checks = finalize.check(tmp, dict(series.format), series.config.get("publish_export"))
            info = inspect_media(tmp)
            os.replace(tmp, final)
            shutil.copy(logo, out_dir / f"logo-{key.split(':', 1)[1][:16]}.png")
        finally:
            shutil.rmtree(work, ignore_errors=True)
        version = {"key": key, "status": "complete", "kind": "post", "conditions": cond, "file": series.rel(final),
                   "format": "mp4", "width": info["width"], "height": info["height"],
                   "duration": round(info["duration"], 3), "bytes": info["bytes"], "sha256": info["sha256"],
                   "master_file": master["file"], "checks": checks, "cost_usd": 0.0,
                   "created_at": _now(), "storage": {"status": "local_only", "note": "persist で永続保存する"}}
        slot["versions"].append(version)
        save_records(series, n, rec)
        return {"action": "built", "version": version}


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


# ---------------- 投稿パッケージ（動画→表紙・静止画・投稿文） ----------------
def package_dir(series: Series, n: int) -> Path:
    return series.root / "publish" / f"ep{n:03d}"


def build_package(series: Series, n: int) -> dict[str, Any]:
    """投稿版の動画・表紙・静止画・各投稿先の文章・一覧（manifest.json）をそろえる。公開はしない。"""
    from . import cover
    from .roughcut import load_spec
    spec_path = series.root / "edits" / f"ep{n:03d}_publish.json"
    spec = read_json(spec_path) if spec_path.exists() else {}
    cfg = publish_config(series)
    post = post_video(series, n)["version"]
    master = _latest(series, n, "final")
    # 表紙（既存の型 edits/epNNN_covers.json を使う）と静止画（動画のコマ、文字なし）
    covers = []
    cov_spec_path = series.root / "edits" / f"ep{n:03d}_covers.json"
    if cov_spec_path.exists():
        covers = [r["version"] for r in cover.build(series, n, load_spec(series, str(cov_spec_path)))]
    stills_spec = {"source_slot": "final", "font": FONT_DEFAULT,
                   "covers": [{"name": f"still-{t:05.2f}s", "time": t} for t in spec.get("stills", [])]}
    stills = [r["version"] for r in cover.build(series, n, stills_spec)] if spec.get("stills") else []
    chosen = spec.get("cover")
    ctx = context(series, n, {"package": {
        "ai_disclosure": spec.get("ai_disclosure") or episode(series, n).get("ai_disclosure")
        or series.config.get("ai_disclosure"),
        "hook": spec.get("hook", ""), "cover": chosen, "hashtags_remove": spec.get("hashtags_remove") or [],
        "en": spec.get("en") or {}}})
    out = package_dir(series, n)
    texts = {}
    tdir = series.root / "publish" / "templates"
    for platform in cfg.get("platforms", []):
        t = tdir / f"{platform}.md"
        if t.exists():
            texts[platform] = render_template(t, ctx)
            atomic_write_text(out / f"{platform}.md", texts[platform])
    manifest = {"episode": n, "version": PUBLISH_VERSION, "created_at": _now(),
                "brand": {"status": ctx["brand"].get("status"), "name_ja": ctx["brand"].get("name_ja"),
                          "name_en": ctx["brand"].get("name_en")},
                "video": {"post": {"file": post["file"], "sha256": post["sha256"], "checks_ok": post["checks"]["ok"]},
                          "master": {"file": master["file"], "sha256": master["sha256"]}},
                "covers": [{"name": v["conditions"]["cover"]["name"], "file": v["file"], "sha256": v["sha256"]}
                           for v in covers],
                "cover_choice": chosen,
                "stills": [{"time": v["conditions"]["cover"]["time"], "file": v["file"], "sha256": v["sha256"]}
                           for v in stills],
                "texts": {k: f"publish/ep{n:03d}/{k}.md" for k in texts},
                "platforms": cfg.get("platforms", [])}
    manifest["package_sha256"] = content_hash({k: v for k, v in manifest.items() if k != "created_at"})
    atomic_write_json(out / "manifest.json", manifest)
    return manifest


# ---------------- 承認台帳と関門 ----------------
def ledger_path(series: Series) -> Path:
    return series.root / "publish" / "ledger.json"


def load_ledger(series: Series) -> dict[str, Any]:
    p = ledger_path(series)
    return read_json(p) if p.exists() else {"version": 1, "episodes": {}}


def save_ledger(series: Series, led: dict[str, Any]) -> None:
    atomic_write_json(ledger_path(series), led)


def _entry(led: dict[str, Any], n: int) -> dict[str, Any]:
    return led["episodes"].setdefault(str(n), {"status": "preparing", "history": []})


def _log(e: dict[str, Any], what: str, **kw: Any) -> None:
    e["history"].append(dict({"at": _now(), "event": what}, **kw))


def request_review(series: Series, n: int) -> dict[str, Any]:
    with exclusive_lock(series.root / "publish" / ".ledger.lock"):
        man = read_json(package_dir(series, n) / "manifest.json")
        led = load_ledger(series)
        e = _entry(led, n)
        if e["status"] in DONE_STATUSES:
            raise GateError(f"第{n}話はすでに {e['status']} です")
        e.update(status="awaiting_review", package_sha256=man["package_sha256"], post_sha256=man["video"]["post"]["sha256"])
        e.pop("approved", None)
        _log(e, "review_requested", package_sha256=man["package_sha256"])
        save_ledger(series, led)
        return e


def approve(series: Series, n: int, sha_prefix: str, *, note: str = "") -> dict[str, Any]:
    """利用者が確認した投稿版を承認する。sha256 の先頭（8文字以上）で、確認したファイルを指定する。"""
    if len(sha_prefix) < 8:
        raise GateError("承認には投稿版の sha256 の先頭8文字以上が必要です")
    with exclusive_lock(series.root / "publish" / ".ledger.lock"):
        man = read_json(package_dir(series, n) / "manifest.json")
        led = load_ledger(series)
        e = _entry(led, n)
        if e.get("status") != "awaiting_review" or e.get("package_sha256") != man["package_sha256"]:
            raise GateError("確認待ちのパッケージがありません（publish package → request-review の後に承認）")
        if man["brand"]["status"] != "final":
            raise GateError("作者ブランドが仮のままです。brand/brand.json の名前を確定してから承認してください")
        if not man["video"]["post"]["checks_ok"]:
            raise GateError("投稿版の自動確認に不合格の項目があります")
        post_sha = man["video"]["post"]["sha256"]
        if not post_sha.startswith(sha_prefix):
            raise GateError("指定した sha256 が、確認待ちの投稿版と一致しません")
        cur = inspect_media(series.resolve(man["video"]["post"]["file"]))["sha256"]
        if cur != post_sha:
            raise GateError("投稿版のファイルがパッケージ作成後に変わっています")
        e.update(status="approved", approved={"post_sha256": post_sha, "package_sha256": man["package_sha256"],
                                              "at": _now(), "note": note})
        _log(e, "approved", post_sha256=post_sha)
        save_ledger(series, led)
        return e


def assert_can_post(series: Series, n: int, platform: str) -> dict[str, Any]:
    """投稿の直前に必ず呼ぶ。承認済み・ファイル一致・未投稿のときだけ通す。"""
    led = load_ledger(series)
    e = led["episodes"].get(str(n))
    if not e or e.get("status") not in ("approved", "published"):
        raise GateError(f"第{n}話は承認されていません")
    if platform in (e.get("posts") or {}):
        raise GateError(f"第{n}話は {platform} に記録済みです（二重投稿の防止）")
    man = read_json(package_dir(series, n) / "manifest.json")
    if man["package_sha256"] != e["approved"]["package_sha256"]:
        raise GateError("承認後にパッケージが作り直されています。もう一度確認・承認が必要です")
    cur = inspect_media(series.resolve(man["video"]["post"]["file"]))["sha256"]
    if cur != e["approved"]["post_sha256"]:
        raise GateError("承認した投稿版とファイルが一致しません")
    if platform not in man.get("platforms", []):
        raise GateError(f"{platform} はこの話の投稿先に入っていません")
    return man


def record_post(series: Series, n: int, platform: str, *, url: str | None = None, post_id: str | None = None,
                done: bool = False) -> dict[str, Any]:
    """利用者が投稿したことを記録する（手動投稿・API 投稿のどちらも）。同じ投稿先への二重記録は拒否。"""
    with exclusive_lock(series.root / "publish" / ".ledger.lock"):
        man = assert_can_post(series, n, platform)
        led = load_ledger(series)
        e = led["episodes"][str(n)]
        e.setdefault("posts", {})[platform] = {"at": _now(), "url": url, "post_id": post_id,
                                               "post_sha256": man["video"]["post"]["sha256"]}
        if done or set(e["posts"]) >= set(man.get("platforms", [])):
            e["status"] = "published"
        _log(e, "posted", platform=platform)
        save_ledger(series, led)
        return e


def assert_can_generate(series: Series, n: int) -> None:
    """第n話の生成・登録の前に呼ぶ。それより前の話が公開（または取り下げ）まで済んでいなければ止める。"""
    led = load_ledger(series)
    pending = []
    for e in load_episodes(series):
        m = e["episode_number"]
        if m >= n:
            continue
        st = (led["episodes"].get(str(m)) or {}).get("status", "preparing")
        if st not in DONE_STATUSES:
            pending.append(f"第{m}話（{st}）")
    if pending:
        raise GateError("前の話の公開準備が終わっていないため、次の話は生成しません: " + "、".join(pending))


def status_lines(series: Series) -> list[str]:
    led = load_ledger(series)
    out = []
    for e in load_episodes(series):
        m = e["episode_number"]
        le = led["episodes"].get(str(m)) or {}
        posts = ", ".join(sorted((le.get("posts") or {}).keys())) or "なし"
        out.append(f"第{m}話「{e['title']}」: {le.get('status', 'preparing')} / 投稿済み: {posts}")
    return out
