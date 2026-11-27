"""プロフィール用の画像（アイコン・ヘッダー）の見本を作る。生成AIは使わない（字体と既存の画像だけ）。

使い方: python3 brand/tools/make_profile_assets.py <出力フォルダ>
必要: Pillow（storyengine 本体は標準ライブラリだけ。この道具だけが Pillow を使う）

- アイコン: アイボリー地に墨色の小文字「m」。字体違いで3案。丸く切り抜かれる前提で中心に置く。
- ヘッダー: 既存の夜の自販機の基準画像（references/locations/vending-corner）を使い、YouTube と X の表示範囲に合わせる。
  文字は brand.json の display_name と tagline_en。
表示範囲の数値は作成時点の各サービスの案内に基づく目安。アップロード前に各サービスの画面のプレビューで確認する。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[2]
BRAND = json.loads((ROOT / "brand" / "brand.json").read_text(encoding="utf-8"))
REF = ROOT / "series" / "midnight-vending-machine" / "references" / "locations" / "vending-corner" / "vending_corner_reference.png"

IVORY = (244, 238, 223)
INK = (38, 36, 34)          # 墨色（真っ黒より少し柔らかい）
NIGHT = (16, 22, 44)        # 夜の紺
WARM = (255, 214, 150)

FONTS = {
    "A_serif": ("/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf", "端正なセリフ体（落ち着き・本の表紙のよう）"),
    "B_italic": ("/usr/share/fonts/truetype/freefont/FreeSerifItalic.ttf", "筆記風のイタリック（手書きの署名に近い・美術的）"),
    "C_slab": ("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf", "太めのセリフ体（小さな丸でも読みやすい）"),
}
TITLE_FONT = "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf"
TAG_FONT = "/usr/share/fonts/truetype/liberation/LiberationSerif-Italic.ttf"


def glyph_box(font: ImageFont.FreeTypeFont, ch: str) -> tuple[int, int, int, int]:
    img = Image.new("L", (font.size * 3, font.size * 3), 0)
    ImageDraw.Draw(img).text((font.size, font.size), ch, font=font, fill=255)
    return img.getbbox()


def icon(font_path: str, size: int = 1080) -> Image.Image:
    """文字の実際の形（字の外枠）で中心を合わせる。丸く切り抜かれても余白が均等になるように。"""
    img = Image.new("RGB", (size, size), IVORY)
    target_w = size * 0.56
    fs = 200
    while True:
        f = ImageFont.truetype(font_path, fs)
        x0, y0, x1, y1 = glyph_box(f, "m")
        if x1 - x0 >= target_w:
            break
        fs += 8
    canvas = Image.new("L", (fs * 3, fs * 3), 0)
    ImageDraw.Draw(canvas).text((fs, fs), "m", font=f, fill=255)
    g = canvas.crop(canvas.getbbox())
    ox = (size - g.width) // 2
    oy = (size - g.height) // 2 + int(size * 0.01)  # 目の錯覚で少し上に見えるのを補う
    ink = Image.new("RGB", g.size, INK)
    img.paste(ink, (ox, oy), g)
    return img


def circle_preview(icons: dict[str, Image.Image], out: Path) -> None:
    """丸い表示（1080・180・110・48・32px）を、明るい背景と暗い背景で並べた確認用の見本。"""
    sizes = [360, 180, 110, 48, 32]
    pad = 24
    row_h = 360 + pad * 2
    W = pad + sum(s + pad for s in sizes) + 220
    H = row_h * len(icons) * 2
    sheet = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(sheet)
    lab = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 22)
    y = 0
    for bg in ((250, 250, 250), (18, 18, 18)):
        for name, im in icons.items():
            d.rectangle([0, y, W, y + row_h], fill=bg)
            d.text((pad, y + pad), name, font=lab, fill=(120, 120, 120))
            x = 220
            for s in sizes:
                t = im.resize((s, s), Image.LANCZOS)
                m = Image.new("L", (s * 4, s * 4), 0)
                ImageDraw.Draw(m).ellipse([0, 0, s * 4 - 1, s * 4 - 1], fill=255)
                m = m.resize((s, s), Image.LANCZOS)
                sheet.paste(t, (x, y + pad + (360 - s) // 2), m)
                x += s + pad
            y += row_h
    sheet.save(out)


def night_background(w: int, h: int) -> Image.Image:
    """基準画像を大きくぼかして暗くした夜の背景（端は紺に溶かす）。"""
    ref = Image.open(REF).convert("RGB")
    scale = max(w / ref.width, h / ref.height)
    bg = ref.resize((int(ref.width * scale) + 1, int(ref.height * scale) + 1), Image.LANCZOS)
    top = int(bg.height * 0.18)
    bg = bg.crop((0, top, w, top + h)) if bg.height - top >= h else bg.crop((0, 0, w, h))
    bg = bg.filter(ImageFilter.GaussianBlur(max(w, h) / 60))
    dark = Image.new("RGB", (w, h), NIGHT)
    return Image.blend(bg, dark, 0.62)


def feathered(img: Image.Image, feather: int, left_only: bool = False) -> Image.Image:
    """画像の縁（または左端だけ）を透明に溶かす。"""
    m = Image.new("L", img.size, 255)
    d = ImageDraw.Draw(m)
    for i in range(feather):
        a = int(255 * i / feather)
        if left_only:
            d.line([(i, 0), (i, img.height)], fill=a)
        else:
            d.rectangle([i, i, img.width - 1 - i, img.height - 1 - i], outline=a)
    out = img.convert("RGBA")
    out.putalpha(m)
    return out


def scene_crop(height: int, box: tuple[float, float, float, float]) -> Image.Image:
    """基準画像の一部（相対座標）を、指定の高さにそろえて切り出す。"""
    ref = Image.open(REF).convert("RGB")
    x0, y0, x1, y1 = (int(box[0] * ref.width), int(box[1] * ref.height), int(box[2] * ref.width), int(box[3] * ref.height))
    c = ref.crop((x0, y0, x1, y1))
    return c.resize((int(c.width * height / c.height), height), Image.LANCZOS)


def draw_text_block(img: Image.Image, x: int, y: int, title: str, tag: str, title_px: int, tag_px: int) -> None:
    d = ImageDraw.Draw(img)
    tf = ImageFont.truetype(TITLE_FONT, title_px)
    gf = ImageFont.truetype(TAG_FONT, tag_px)
    d.text((x, y), title, font=tf, fill=IVORY)
    tb = d.textbbox((x, y), title, font=tf)
    ly = tb[3] + int(tag_px * 0.55)
    d.line([(x + 2, ly), (x + int(title_px * 1.4), ly)], fill=WARM, width=max(2, tag_px // 14))
    d.text((x, ly + int(tag_px * 0.45)), tag, font=gf, fill=(230, 222, 204))


def youtube_header(title: str, tag: str) -> Image.Image:
    # YouTube: 2560×1440。どの画面でも見える範囲は中央の 1546×423（文字はこの中に入れる）。テレビでは全体が見える
    W, H = 2560, 1440
    img = night_background(W, H)
    sx0, sy0, sw, sh = (W - 1546) // 2, (H - 423) // 2, 1546, 423
    ref = Image.open(REF).convert("RGB")
    h = 2200
    scene = ref.resize((int(ref.width * h / ref.height), h), Image.LANCZOS)
    off = min(0, sy0 - int(0.22 * h))            # 見える帯に、時計・光る陳列窓が入る高さ（上端は必ず埋める）
    scene = scene.crop((0, -off, scene.width, -off + H))
    sc = feathered(scene, 360, left_only=True)
    img.paste(sc, (W - scene.width, 0), sc)
    draw_text_block(img, sx0 + 40, sy0 + 105, title, tag, 112, 46)
    return img


def x_header(title: str, tag: str) -> Image.Image:
    # X: 1500×500。左下はアイコンが重なり、スマホでは上下が少し切れるので、文字は中央の高さ・左寄り（アイコンより上）に置く
    W, H = 1500, 500
    img = night_background(W, H)
    scene = scene_crop(500, (0.0, 0.10, 1.0, 0.50))
    sc = feathered(scene, 220, left_only=True)
    img.paste(sc, (W - scene.width, 0), sc)
    draw_text_block(img, 90, 120, title, tag, 84, 36)
    return img


def safe_overlay(img: Image.Image, boxes: list[tuple[tuple[int, int, int, int], str]]) -> Image.Image:
    out = img.copy()
    d = ImageDraw.Draw(out)
    f = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", max(18, img.width // 80))
    for (x0, y0, x1, y1), label in boxes:
        d.rectangle([x0, y0, x1, y1], outline=(80, 220, 255), width=max(3, img.width // 600))
        d.text((x0 + 8, y0 + 6), label, font=f, fill=(80, 220, 255))
    return out


def main(out_dir: str) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    title = BRAND["display_name"]
    tag = BRAND.get("tagline_header_en", "Small wonders. Quiet stories.")
    icons = {}
    for key, (path, _) in FONTS.items():
        im = icon(path)
        im.save(out / f"icon_{key}.png")
        icons[key] = im
    circle_preview(icons, out / "icon_preview_circles.png")
    yt = youtube_header(title, tag)
    yt.save(out / "header_youtube_2560x1440.jpg", quality=92)
    safe_overlay(yt, [(((2560 - 1546) // 2, (1440 - 423) // 2, (2560 + 1546) // 2, (1440 + 423) // 2), "all devices 1546x423"),
                      ((0, (1440 - 423) // 2, 2560, (1440 + 423) // 2), "desktop 2560x423")]
                 ).resize((1280, 720)).save(out / "header_youtube_safe_area_preview.jpg", quality=88)
    xh = x_header(title, tag)
    xh.save(out / "header_x_1500x500.jpg", quality=92)
    pv = safe_overlay(xh, [((0, 60, 1500, 440), "mobile crop (approx.)")])
    d = ImageDraw.Draw(pv)
    d.ellipse([24, 330, 224, 530], outline=(255, 120, 120), width=4)  # アイコンが重なる目安
    pv.save(out / "header_x_safe_area_preview.jpg", quality=88)
    print("\n".join(sorted(p.name for p in out.iterdir())))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "brand/out/profile")
