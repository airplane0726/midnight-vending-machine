"""作品サイトのローカル下書きを作る（静的 HTML。公開はしない）。

- 最新話、全話一覧、キャラクター紹介、各投稿先へのリンク、作者・作品紹介を1ページにまとめ、話ごとのページも作る。
- 名前・リンクは brand/brand.json と各作品の series.json・publish/templates から読む。未取得の URL は
  リンクにせず「準備中」と書く（実在するように見せない）。
- 既定では公開済みでない話にも「確認中」などの印を付けて載せる（下書き）。public_only=True で公開済みの話だけ。
- 出力先（既定 site_draft/）は Git 管理外。動画・画像はそこへ写す。
"""
from __future__ import annotations

import html
import shutil
from pathlib import Path
from typing import Any

from . import publish
from .media import load_records
from .series import Series
from .state import load_episodes

STATUS_JA = {"preparing": "準備中", "awaiting_review": "確認待ち", "approved": "承認済み・未投稿",
             "published": "公開済み", "withdrawn": "取り下げ"}
LINK_NAMES = {"youtube": "YouTube", "instagram": "Instagram", "tiktok": "TikTok", "note": "note", "x": "X", "site": "Web"}

CSS = """
:root{--bg:#0f1830;--panel:#17223f;--ink:#f3ead8;--sub:#c9bfae;--accent:#ffcf7a;--line:#2b3a63}
@media (prefers-color-scheme: light){:root:not([data-theme="dark"]){--bg:#f6f1e7;--panel:#fffaf0;--ink:#1d2338;--sub:#55607a;--accent:#b9741c;--line:#e3d9c6}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:system-ui,"Hiragino Sans","Noto Sans JP",sans-serif;line-height:1.7}
.draft{background:#b33;color:#fff;text-align:center;padding:6px 16px;font-size:14px}
header,main,footer{max-width:960px;margin:0 auto;padding:16px}
header h1{margin:8px 0 0;font-size:28px}header p{margin:4px 0;color:var(--sub)}
section{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;margin:16px 0}
h2{margin:0 0 12px;font-size:20px;color:var(--accent)}
.latest{display:grid;grid-template-columns:minmax(0,320px) 1fr;gap:16px}
@media (max-width:640px){.latest{grid-template-columns:1fr}}
video,img{max-width:100%;border-radius:10px;display:block}
.eps{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px;list-style:none;padding:0;margin:0}
.eps a{color:inherit;text-decoration:none}.eps small{color:var(--sub)}
.badge{display:inline-block;font-size:12px;border:1px solid var(--accent);color:var(--accent);border-radius:999px;padding:0 8px;margin-left:6px}
.chars{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}
.links{list-style:none;padding:0;margin:0;display:flex;flex-wrap:wrap;gap:8px}
.links li{border:1px solid var(--line);border-radius:8px;padding:4px 10px}
.links a{color:var(--accent)} .pending{color:var(--sub)}
footer{color:var(--sub);font-size:13px}
"""


def _e(s: Any) -> str:
    return html.escape(str(s or ""))


def _media(series: Series, n: int, slot: str) -> dict[str, Any] | None:
    vs = [v for v in load_records(series, n)["slots"].get(slot, {}).get("versions", []) if v.get("status") == "complete"]
    return vs[-1] if vs else None


def _copy(series: Series, rel: str, out: Path) -> str:
    src = series.resolve(rel)
    dst = out / "media" / Path(rel).name
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.exists() and (not dst.exists() or dst.stat().st_size != src.stat().st_size):
        shutil.copy2(src, dst)
    return f"media/{dst.name}"


def _links_html(links: dict[str, Any]) -> str:
    items = []
    for k, v in links.items():
        name = LINK_NAMES.get(k, k)
        if v:
            items.append(f'<li><a href="{_e(v)}" rel="noopener">{_e(name)}</a></li>')
        else:
            items.append(f'<li><span class="pending">{_e(name)}（準備中）</span></li>')
    return f'<ul class="links">{"".join(items)}</ul>'


def _page(title: str, body: str, draft: bool, brand: dict[str, Any]) -> str:
    banner = '<div class="draft">下書き（非公開・ローカル確認用）。名前・リンクは仮のものを含みます</div>' if draft else ""
    return (f'<!doctype html><html lang="ja"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta name="robots" content="noindex,nofollow"><title>{_e(title)}</title><style>{CSS}</style></head>'
            f'<body>{banner}{body}<footer><p>© {_e(brand.get("display_name"))}</p></footer>'
            f'</body></html>')


def build(series: Series, out: Path, *, public_only: bool = False) -> list[Path]:
    brand = publish.load_brand(series)
    led = publish.load_ledger(series)
    cfg = series.config
    ctx = publish.context(series)
    tdir = series.root / "publish" / "templates"
    intro_ja = publish.render_template(tdir / "intro_ja.md", ctx) if (tdir / "intro_ja.md").exists() else cfg.get("premise", "")
    eps = []
    for ep in load_episodes(series):
        n = ep["episode_number"]
        st = (led["episodes"].get(str(n)) or {}).get("status", "preparing")
        if public_only and st != "published":
            continue
        eps.append((ep, st))
    out.mkdir(parents=True, exist_ok=True)
    written = []
    cards, latest = [], ""
    for i, (ep, st) in enumerate(reversed(eps)):
        n = ep["episode_number"]
        post = _media(series, n, "post") or _media(series, n, "final")
        cov = publish.read_json(publish.package_dir(series, n) / "manifest.json") \
            if (publish.package_dir(series, n) / "manifest.json").exists() else None
        cover_rel = None
        if cov and cov.get("covers"):
            pick = next((c for c in cov["covers"] if c["name"] == cov.get("cover_choice")), cov["covers"][0])
            cover_rel = pick["file"]
        poster = _copy(series, cover_rel, out) if cover_rel else ""
        video = _copy(series, post["file"], out) if post else ""
        badge = "" if st == "published" else f'<span class="badge">{_e(STATUS_JA.get(st, st))}</span>'
        page = f"ep{n:03d}.html"
        body = (f'<header><p><a href="index.html">← {_e(cfg.get("title"))}</a></p>'
                f'<h1>第{n}話「{_e(ep["title"])}」{badge}</h1><p>{_e(ep.get("logline"))}</p></header><main><section>'
                + (f'<video controls playsinline preload="metadata" poster="{poster}" src="{video}"></video>' if video else "")
                + f'<p>{_e(ep.get("summary"))}</p><p><small>{_e(ep.get("ai_disclosure") or cfg.get("ai_disclosure"))}</small></p></section></main>')
        (out / page).write_text(_page(f'第{n}話「{ep["title"]}」｜{cfg.get("title")}', body, not public_only, brand),
                                encoding="utf-8")
        written.append(out / page)
        thumb = f'<img src="{poster}" alt="第{n}話の表紙" loading="lazy">' if poster else ""
        cards.append(f'<li><a href="{page}">{thumb}<div>第{n}話「{_e(ep["title"])}」{badge}</div></a></li>')
        if i == 0:
            latest = (f'<section><h2>最新話</h2><div class="latest"><div>'
                      + (f'<video controls playsinline preload="metadata" poster="{poster}" src="{video}"></video>'
                         if video else "")
                      + f'</div><div><h3>第{n}話「{_e(ep["title"])}」{badge}</h3><p>{_e(ep.get("logline"))}</p>'
                      f'<p><a href="{page}">この話のページへ</a></p></div></div></section>')
    chars = "".join(
        f'<div><h3>{_e(c.get("name"))} <small>{_e(c.get("name_en"))}</small></h3><p>{_e(c.get("role"))}</p>'
        f'<p>{_e(c.get("personality"))}</p></div>' for c in (cfg.get("characters") or {}).values())
    body = (f'<header><p>{_e(brand.get("display_name"))}</p>'
            f'<h1>{_e(cfg.get("title"))}</h1><p>{_e(cfg.get("title_en"))}</p></header><main>'
            f'{latest or "<section><h2>最新話</h2><p>まだありません。</p></section>"}'
            f'<section><h2>全話一覧</h2><ul class="eps">{"".join(cards) or "<li>まだありません</li>"}</ul></section>'
            f'<section><h2>作品紹介</h2><p>{_e(intro_ja).replace(chr(10), "<br>")}</p></section>'
            f'<section><h2>キャラクター</h2><div class="chars">{chars}</div></section>'
            f'<section><h2>リンク</h2>{_links_html(brand.get("links") or {})}</section>'
            f'<section><h2>作者</h2><p>{_e(brand.get("tagline_ja"))}</p></section></main>')
    (out / "index.html").write_text(_page(cfg.get("title", series.id), body, not public_only, brand), encoding="utf-8")
    written.append(out / "index.html")
    return written
