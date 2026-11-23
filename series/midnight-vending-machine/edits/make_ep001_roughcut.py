"""第1話の仮編集の指示（edits/ep001_roughcut.json）を作る。座標は採用画像（1152x2048）の画素。
python3 edits/make_ep001_roughcut.py で書き出す。"""
import json
from pathlib import Path

IMG = "media/ep001/images/"
K1, K2, K3, K4, K5 = (IMG + f for f in (
    "cut01_start-696ceaab71273c70.png", "cut02_start-39312c13f6bfba88.png", "cut03_start-3e02072f3995d5c0.png",
    "cut04_start-483422b202a91c66.png", "cut05_start-913aa60bc5ae165e.png"))
MOCHI = "references/characters/mochi/mochi_reference.png"
SIZE = [1152, 2048]

# カット5の画像のポコ（浮いている）の形
POCO = [{"ellipse": [352, 1081, 172, 298]}, {"ellipse": [233, 1141, 40, 64]}, {"ellipse": [527, 1128, 40, 64]},
        {"rect": [387, 1215, 142, 155]}, {"rect": [283, 995, 222, 198]},
        {"rect": [273, 1340, 62, 105]}, {"rect": [387, 1340, 62, 105]},
        {"ellipse": [305, 1452, 56, 26]}, {"ellipse": [416, 1452, 56, 26]}]
SHADOW = {"ellipse": [353, 1556, 150, 26]}
CUBE5 = [283, 995, 221, 197]  # 立方体（x, y, 幅, 高さ）

# 浮き上がりの上下（+80 で接地、負で上）。ショット5の先頭からの秒
# 0 が接地（元の画像の足の高さを地面とみなし、足元に接地の影を置く）。負で上へ
LIFT = [[0, 0], [0.4, 0], [2.6, -200], [3.0, -200], [3.3, -190], [5.0, 0], [5.12, 8], [5.3, 0]]
FOOT_Y = 1478  # 接地したときの足の裏


def plus(kfs, d):
    return [[t, v + d] for t, v in kfs]


assets = {
    "glow_btn": {"op": "glow", "radius": 46, "color": [255, 186, 90]},
    "rain_tex3": {"op": "rain", "size": [276, 264], "density": 1.2, "seed": 3},
    "rain_tex4": {"op": "rain", "size": [190, 190], "density": 1.2, "seed": 4},
    "rain_tex5": {"op": "rain", "size": [190, 166], "density": 1.2, "seed": 5},
    "blank3": {"op": "blank", "size": [324, 312]},
    "blank4": {"op": "blank", "size": [214, 214]},
    "blank5": {"op": "blank", "size": CUBE5[2:]},
    "mask_plate": {"op": "mask", "size": SIZE, "shapes": POCO + [SHADOW], "grow": 12},
    "mask_poco": {"op": "mask", "size": SIZE, "shapes": POCO, "grow": 3, "feather": 2},
    "plate5": {"op": "fill", "src": K5, "mask": "@mask_plate", "sigma": 45,
               "patches": [{"from": [590, 1470, 380, 230], "to": [180, 1420], "feather": 30}]},
    "poco5": {"op": "cutout", "src": K5, "mask": "@mask_poco"},
    "cube5_flip": {"op": "crop", "src": K5, "rect": CUBE5, "vflip": True},
    "shadow": {"op": "glow", "radius": 140, "ry": 24, "color": [16, 10, 6], "alpha": 0.85},
    "rainbow": {"op": "rainbow", "radius": 66, "thickness": 26},
    "mask_mochi": {"op": "mask", "size": [1254, 1254], "shapes": [{"ellipse": [203, 640, 170, 166]},
                   {"rect": [80, 518, 246, 246]}], "grow": -4, "feather": 3},
    "mochi_cut": {"op": "cutout", "src": MOCHI, "mask": "@mask_mochi"},
    "mochi": {"op": "crop", "src": "@mochi_cut", "rect": [20, 450, 370, 380], "height": 226,
              "tint": [1.0, 0.84, 0.62]},
}

cube_rain_up = {"asset": "rain_tex5", "speed": 380, "dir": "up", "opacity": 0.7, "inset": 15}
cube_rain_dn = {"asset": "rain_tex5", "speed": 420, "dir": "down", "opacity": 0.55, "inset": 15}

shots = [
    {  # 0.0–1.8 光るボタン
        "name": "光るボタン", "duration": 1.8, "base": K1,
        "camera": [[0, 1.0, 0.5, 0.5], [1.8, 1.07, 0.6, 0.55]],
        "layers": [{"asset": "glow_btn", "anchor": "center", "x": 913, "y": 1078, "fade_in": [0.5, 0.3]}],
        "transition": {"type": "fade", "duration": 0.3}},
    {  # 1.8–4.4 押す→ゴトン
        "name": "押す", "duration": 2.6, "base": K2,
        "camera": [[0, 1.07, 0.6, 0.55], [2.6, 1.16, 0.66, 0.58]],
        "shake": [{"at": 1.6, "dur": 0.35, "amp": 7}],
        "layers": [{"asset": "glow_btn", "anchor": "center", "x": 913, "y": 1076, "opacity": 0.8,
                    "fade_out": [2.1, 0.3]}],
        "transition": {"type": "fade", "duration": 0.35}},
    {  # 4.4–7.4 立方体の雨
        "name": "雨の立方体", "duration": 3.0, "base": K3,
        "camera": [[0, 1.0, 0.5, 0.56], [3.0, 1.12, 0.5, 0.58]],
        "layers": [{"asset": "blank3", "x": 415, "y": 1034,
                    "rain": {"asset": "rain_tex3", "speed": 430, "dir": "down", "opacity": 0.5, "inset": 24}}],
        "transition": {"type": "fade", "duration": 0.4}},
    {  # 7.4–10.8 傾くと雨も傾く
        "name": "傾く雨", "duration": 3.4, "base": K4,
        "camera": [[0, 1.0, 0.45, 0.6], [3.4, 1.1, 0.42, 0.6]],
        "layers": [{"asset": "blank4", "anchor": "center", "x": 505, "y": 1244, "angle": 30,
                    "clip": [378, 1124, 258, 242],
                    "rain": {"asset": "rain_tex4", "speed": 400, "dir": "down", "opacity": 0.5, "inset": 12}}],
        "transition": {"type": "fadewhite", "duration": 0.35}},
    {  # 10.8–17.0 逆さ→浮く→戻す→着地→虹
        "name": "浮いて着地", "duration": 6.2, "base": "@plate5",
        "camera": [[0, 1.05, 0.4, 0.6], [2.6, 1.05, 0.4, 0.53], [3.3, 1.05, 0.4, 0.53], [5.0, 1.05, 0.4, 0.6],
                   [6.2, 1.06, 0.4, 0.61]],
        "shake": [{"at": 3.0, "dur": 0.25, "amp": 4}, {"at": 5.0, "dur": 0.3, "amp": 3}],
        "layers": [
            {"asset": "shadow", "x": 362 - 140, "y": FOOT_Y - 24},
            {"asset": "poco5", "x": 0, "y": LIFT},
            {"asset": "cube5_flip", "x": CUBE5[0], "y": plus(LIFT, CUBE5[1]), "fade_out": [3.0, 0.3]},
            {"asset": "blank5", "x": CUBE5[0], "y": plus(LIFT, CUBE5[1]), "fade_out": [3.0, 0.3],
             "rain": cube_rain_up},
            {"asset": "blank5", "x": CUBE5[0], "y": plus(LIFT, CUBE5[1]), "fade_in": [3.0, 0.3],
             "rain": cube_rain_dn},
            {"asset": "rainbow", "x": 361 - 66, "y": FOOT_Y + 34 - 66, "fade_in": [5.3, 0.9]}],
        "transition": {"type": "cut"}},
    {  # 17.0–20.0 モチがのぞく
        "name": "のぞく目", "duration": 3.0, "base": "@plate5",
        "camera": [[0, 1.06, 0.4, 0.61], [3.0, 1.13, 0.52, 0.62]],
        "layers": [
            {"asset": "shadow", "x": 362 - 140, "y": FOOT_Y - 24},
            {"asset": "poco5", "x": 0, "y": 0},
            {"asset": "blank5", "x": CUBE5[0], "y": CUBE5[1], "rain": cube_rain_dn},
            {"asset": "rainbow", "x": 361 - 66, "y": FOOT_Y + 34 - 66},
            {"asset": "mochi", "x": 690, "y": [[0, 1325], [0.4, 1325], [1.2, 1212]], "opacity": 0.88,
             "clip": [549, 1143, 422, 178]}]},
]

F3, C3 = "F3", "C3"
pizz = [[4.6 + 0.714 * i, "pizz", (F3, C3, "A2", C3)[i % 4], 0.5, {"gain_db": 2}] for i in range(9)]
sound = {
    "seed": 7,
    "ambience": [
        {"type": "hum", "freq": 50, "gain_db": -33, "start": 0, "end": 20, "fade": 0.6},
        {"type": "wind", "gain_db": -36, "start": 0, "end": 20, "fade": 1.0},
        {"type": "rain", "gain_db": -30, "start": 4.5, "end": 10.75, "fade": 0.3, "pan": [[7.6, 0], [9.0, 0.5]]},
        {"type": "rain_rise", "gain_db": -27, "start": 10.9, "end": 14.1, "fade": 0.2},
        {"type": "rain", "gain_db": -30, "start": 14.1, "end": 18.4, "fade": 0.5},
    ],
    "events": [
        {"t": 0.5, "type": "bell", "note": "A5", "gain_db": -14},
        {"t": 1.0, "type": "knock", "freq": 420, "gain_db": -20, "pan": -0.2},
        {"t": 1.45, "type": "knock", "freq": 400, "gain_db": -20, "pan": -0.1},
        {"t": 2.4, "type": "click", "gain_db": -14, "pan": 0.3},
        {"t": 3.4, "type": "thump", "freq": 70, "gain_db": -9},
        {"t": 3.5, "type": "rattle", "count": 4, "gain_db": -22},
        {"t": 3.95, "type": "squeak", "freq": 900, "to": 1250, "dur": 0.35, "gain_db": -24},
        {"t": 4.45, "type": "knock", "freq": 260, "dur": 0.12, "gain_db": -17},
        {"t": 5.6, "type": "beep", "freq": 1500, "to": 1900, "dur": 0.12, "gain_db": -18},
        {"t": 7.5, "type": "squeak", "freq": 2400, "to": 2900, "dur": 0.12, "gain_db": -26},
        {"t": 10.0, "type": "beep", "freq": 2100, "dur": 0.08, "gain_db": -18},
        {"t": 10.55, "type": "squeak", "freq": 2200, "to": 3100, "dur": 0.18, "gain_db": -22},
        {"t": 10.6, "type": "whoosh", "dur": 0.45, "gain_db": -20},
        {"t": 11.3, "type": "beep", "freq": 700, "to": 1400, "dur": 0.6, "gain_db": -20},
        {"t": 13.55, "type": "beep", "freq": 2300, "dur": 0.07, "gain_db": -17},
        {"t": 13.68, "type": "beep", "freq": 2300, "dur": 0.07, "gain_db": -17},
        {"t": 13.8, "type": "squeak", "freq": 2600, "to": 2000, "dur": 0.1, "gain_db": -22},
        {"t": 13.95, "type": "squeak", "freq": 2600, "to": 2000, "dur": 0.1, "gain_db": -22},
        {"t": 13.82, "type": "whoosh", "dur": 0.35, "up": False, "gain_db": -22},
        {"t": 15.8, "type": "thump", "freq": 120, "dur": 0.25, "gain_db": -16},
        {"t": 17.45, "type": "squeak", "freq": 700, "to": 820, "dur": 0.5, "gain_db": -28, "pan": 0.4},
        {"t": 19.0, "type": "pop", "freq": 1100, "gain_db": -22, "pan": 0.4},
    ],
    "music": {"gain_db": -15, "notes": [
        [0.6, "musicbox", "A5", 1.4], [1.1, "musicbox", "C6", 1.4], [1.6, "musicbox", "F5", 1.8],
        [3.0, "musicbox", "F5", 1.0], [4.6, "musicbox", "A5", 1.2], [5.3, "musicbox", "C6", 1.2],
        [6.0, "musicbox", "Bb5", 1.0], [6.7, "musicbox", "A5", 1.2],
        [8.0, "musicbox", "C6", 0.8], [8.3, "musicbox", "D6", 0.8], [8.6, "musicbox", "F6", 1.4],
        [9.2, "musicbox", "E6", 0.8], [9.5, "musicbox", "C6", 1.2], [10.0, "celesta", "A6", 0.8],
        *pizz,
        [11.0, "glass", "F5", 1.5, {"gain_db": -3}], [11.6, "glass", "G5", 1.5, {"gain_db": -3}],
        [12.2, "glass", "A5", 1.5, {"gain_db": -3}], [12.8, "glass", "B5", 1.5, {"gain_db": -3}],
        [13.4, "glass", "C#6", 1.2, {"gain_db": -3}],
        [14.3, "musicbox", "C6", 1.0], [14.9, "musicbox", "A5", 1.0], [15.5, "musicbox", "F5", 1.2],
        [16.15, "musicbox", "F5", 2.0], [16.25, "musicbox", "A5", 2.0], [16.35, "musicbox", "C6", 2.0],
        [16.45, "celesta", "F6", 1.5], [16.1, "glass", "F4", 2.4, {"gain_db": -4}],
        [16.1, "glass", "C5", 2.4, {"gain_db": -6}],
        [17.7, "musicbox", "C6", 1.4], [18.35, "musicbox", "A5", 1.4], [19.0, "musicbox", "F5", 1.0],
    ]},
}

spec = {"episode": 1, "purpose": "仮編集（完成形を判断するため。既存素材だけ・追加課金なし）",
        "placeholders": ["浮く・着地：カット5の画像からポコを切り抜いて上下に動かす。元の足の高さを地面とし、接地の影を足元に置く（回す動作は白い切り替えで代用）",
                         "逆さの立方体：立方体部分を上下反転して代用",
                         "虹：図形（6色の小さなアーチ）。両足がくっついていて間に置けないため、足のすぐ手前の地面に置いた",
                         "モチ：基準画像を形で切り抜き、暖色に寄せて取り出し口に重ねる（光や質感は合っていない）",
                         "音：すべて合成音（仮）"],
        "output": {"width": 1080, "height": 1920, "fps": 30}, "assets": assets, "shots": shots, "sound": sound}
Path(__file__).with_name("ep001_roughcut.json").write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
print(sum(s["duration"] for s in shots), "秒")
