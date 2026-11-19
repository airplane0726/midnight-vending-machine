"""エピソード JSON の検証。

検証ルールの数値・必須項目はすべて series.json の `format` から読む。
`state` には「この話より前までの履歴」を渡す（話数・謎・品物の整合を確認するため）。
"""
from __future__ import annotations

import re
from typing import Any

from .series import Series

_MISSING = object()
PLACEHOLDER_RE = re.compile(r"TODO|TBD|\{\{.*?\}\}")
EPS = 1e-6


def _get_paths(obj: Any, path: str) -> list[tuple[str, Any]]:
    """`a.b[].c` 形式のパスを辿り、(実際のパス, 値) の一覧を返す。"""
    results: list[tuple[str, Any]] = [("", obj)]
    for part in path.split("."):
        is_list = part.endswith("[]")
        key = part[:-2] if is_list else part
        nxt: list[tuple[str, Any]] = []
        for prefix, cur in results:
            here = f"{prefix}.{key}" if prefix else key
            val = cur.get(key, _MISSING) if isinstance(cur, dict) else _MISSING
            if is_list:
                if isinstance(val, list):
                    nxt.extend((f"{here}[{i}]", v) for i, v in enumerate(val))
                else:
                    nxt.append((here, _MISSING if val is _MISSING else val))
            else:
                nxt.append((here, val))
        results = nxt
    return results


def _is_empty(val: Any) -> bool:
    if val is _MISSING or val is None:
        return True
    if isinstance(val, str):
        return not val.strip()
    if isinstance(val, (list, dict)):
        return len(val) == 0
    return False


def _walk_strings(obj: Any, path: str = ""):
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk_strings(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_strings(v, f"{path}[{i}]")


def _num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def check_required(obj: Any, paths: list[str], errors: list[str]) -> None:
    for p in paths:
        for real, val in _get_paths(obj, p):
            if _is_empty(val):
                errors.append(f"必須項目が空です: {real}")


def validate_structure(series: Series, ep: dict[str, Any]) -> list[str]:
    """履歴に依存しない検証（カット数・尺・必須項目・投稿文の制限など）。"""
    fmt = series.format
    errors: list[str] = []

    if not isinstance(ep, dict):
        return ["エピソードは JSON オブジェクトである必要があります"]

    if ep.get("series_id") != series.id:
        errors.append(f"series_id が一致しません: {ep.get('series_id')!r} != {series.id!r}")

    n = ep.get("episode_number")
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        errors.append(f"episode_number は1以上の整数である必要があります: {n!r}")

    check_required(ep, fmt.get("required_episode_fields", []), errors)

    # ---- カット ----
    cuts = ep.get("cuts")
    durations = fmt["cut_durations"]
    if not isinstance(cuts, list):
        errors.append("cuts が配列ではありません")
        cuts = []
    elif len(cuts) != len(durations):
        errors.append(f"カット数が {len(cuts)} です（必要: {len(durations)}）")

    total = 0.0
    char_ids = set(series.characters)
    loc_ids = set(series.locations)
    forbidden = fmt.get("forbidden_cut_fields", []) if not fmt.get("spoken_text_allowed", True) else []
    for i, cut in enumerate(cuts):
        label = f"cuts[{i}]"
        if not isinstance(cut, dict):
            errors.append(f"{label} がオブジェクトではありません")
            continue
        if cut.get("cut_number") != i + 1:
            errors.append(f"{label}.cut_number は {i + 1} である必要があります: {cut.get('cut_number')!r}")
        d = cut.get("duration_sec")
        if not _num(d):
            errors.append(f"{label}.duration_sec が数値ではありません: {d!r}")
            d = 0
        elif i < len(durations) and abs(d - durations[i]) > EPS:
            errors.append(f"{label}.duration_sec が {d} 秒です（必要: {durations[i]} 秒）")
        total += d

        for p in fmt.get("required_cut_fields", []):
            for real, val in _get_paths(cut, p):
                if _is_empty(val):
                    errors.append(f"必須項目が空です: {label}.{real}")

        for fld in forbidden:
            if not _is_empty(cut.get(fld, _MISSING)):
                errors.append(f"{label}.{fld}: この連載ではせりふ・ナレーション・字幕を使いません")

        for cid in cut.get("characters_in_shot", []) or []:
            if cid not in char_ids:
                errors.append(f"{label}.characters_in_shot に未登録の人物: {cid!r}")
        start_chars = cut.get("characters_in_start_image")
        if start_chars is not None and not set(start_chars) <= set(cut.get("characters_in_shot") or []):
            errors.append(f"{label}.characters_in_start_image は characters_in_shot に含まれる人物だけにしてください")
        loc = cut.get("location")
        if loc_ids and loc is not None and loc not in loc_ids:
            errors.append(f"{label}.location が未登録です: {loc!r}")

        for j, beat in enumerate(cut.get("motion", []) or []):
            s, e = beat.get("start_sec"), beat.get("end_sec")
            if not (_num(s) and _num(e)) or s < 0 or e < s or e > d + EPS:
                errors.append(f"{label}.motion[{j}] の時刻が不正です（0〜{d}秒、start<=end）: {s!r}-{e!r}")
        sound = cut.get("sound") or {}
        for j, sfx in enumerate(sound.get("sfx", []) or []):
            t = sfx.get("at_sec")
            if not _num(t) or t < 0 or t > d + EPS:
                errors.append(f"{label}.sound.sfx[{j}].at_sec が 0〜{d} 秒の範囲外です: {t!r}")

    if cuts and abs(total - fmt["total_seconds"]) > EPS:
        errors.append(f"合計尺が {total:g} 秒です（必要: {fmt['total_seconds']} 秒）")

    # ---- 投稿文 ----
    limits = fmt.get("post_limits", {})
    posts = ep.get("posts") or {}
    ig = posts.get("instagram") or {}
    yt = posts.get("youtube") or {}
    tags = ig.get("hashtags") or []
    for t in tags:
        if not isinstance(t, str) or not re.fullmatch(r"#[^\s#]+", t):
            errors.append(f"posts.instagram.hashtags の形式が不正です: {t!r}")
    if "instagram_max_hashtags" in limits and len(tags) > limits["instagram_max_hashtags"]:
        errors.append(f"Instagram のハッシュタグが {len(tags)} 個です（上限 {limits['instagram_max_hashtags']}）")
    full_caption = compose_instagram_caption(ep)
    if "instagram_caption_max" in limits and len(full_caption) > limits["instagram_caption_max"]:
        errors.append(f"Instagram の投稿文が {len(full_caption)} 文字です（上限 {limits['instagram_caption_max']}）")
    title = yt.get("title") or ""
    if "youtube_title_max" in limits and len(title) > limits["youtube_title_max"]:
        errors.append(f"YouTube のタイトルが {len(title)} 文字です（上限 {limits['youtube_title_max']}）")
    if re.search(r"[<>]", title + (yt.get("description") or "")):
        errors.append("YouTube のタイトル・説明文に < > は使えません")
    if "youtube_description_max" in limits and len(yt.get("description") or "") > limits["youtube_description_max"]:
        errors.append(f"YouTube の説明文が上限 {limits['youtube_description_max']} 文字を超えています")
    yt_tags = yt.get("tags") or []
    if "youtube_tags_total_max" in limits and sum(len(t) for t in yt_tags) + max(len(yt_tags) - 1, 0) > limits["youtube_tags_total_max"]:
        errors.append(f"YouTube タグの合計が上限 {limits['youtube_tags_total_max']} 文字を超えています")

    # ---- 書きかけの検出 ----
    for p, s in _walk_strings(ep):
        if PLACEHOLDER_RE.search(s):
            errors.append(f"未記入のプレースホルダーが残っています: {p}")

    return errors


def validate_against_history(series: Series, state: dict[str, Any], ep: dict[str, Any]) -> list[str]:
    """履歴（この話より前の state）との整合を検証する。"""
    errors: list[str] = []
    n = ep.get("episode_number")
    if n != state["next_episode"]:
        if any(e["episode_number"] == n for e in state["episodes"]):
            errors.append(f"第{n}話はすでに登録されています（話数の重複）")
        else:
            errors.append(f"episode_number が {n!r} です。次に作るべき話数は {state['next_episode']} です")

    title = ep.get("title")
    for e in state["episodes"]:
        if e["title"] == title:
            errors.append(f"タイトル「{title}」は第{e['episode_number']}話と重複しています")

    # 品物（作品ごとの名称は format.featured_object.label）
    fo_cfg = series.format.get("featured_object")
    obj = ep.get("featured_object")
    if fo_cfg and fo_cfg.get("required") and not isinstance(obj, dict):
        errors.append(f"featured_object（{fo_cfg.get('label', '題材')}）がありません")
    if isinstance(obj, dict):
        registry = state["featured_objects"]
        oid = obj.get("id")
        if oid in registry:
            known = registry[oid]
            if obj.get("first_appearance", False):
                errors.append(f"featured_object.id {oid!r} は第{known['first_episode']}話で登場済みです（first_appearance を false に）")
            if obj.get("properties") != known["properties"]:
                errors.append(f"既出の {oid!r} の性質（properties）が変更されています。性質は途中で変えられません")
            if obj.get("name") != known["name"]:
                errors.append(f"既出の {oid!r} の名前が変更されています")
        else:
            if not obj.get("first_appearance", False):
                errors.append(f"featured_object.id {oid!r} は未登場です（first_appearance を true に）")
            for k, v in registry.items():
                if v["name"] == obj.get("name"):
                    errors.append(f"品物名「{obj.get('name')}」は {k!r} として登録済みです")

    cont = ep.get("continuity") or {}
    mysteries = state["mysteries"]
    for m in cont.get("mysteries_opened", []) or []:
        if m.get("id") in mysteries:
            errors.append(f"謎 {m.get('id')!r} はすでに存在します")
    for key in ("mysteries_progressed", "mysteries_resolved"):
        for m in cont.get(key, []) or []:
            mid = m.get("id")
            if mid not in mysteries:
                errors.append(f"continuity.{key}: 未登録の謎 {mid!r}")
            elif mysteries[mid]["status"] != "open":
                errors.append(f"continuity.{key}: 謎 {mid!r} はすでに解決済みです")

    ideas = state["future_ideas_non_canon"]
    for idea in cont.get("future_ideas", []) or []:
        if idea.get("id") in ideas:
            errors.append(f"構想 {idea.get('id')!r} はすでに存在します")
    for iid in cont.get("future_ideas_retired", []) or []:
        if iid not in ideas:
            errors.append(f"continuity.future_ideas_retired: 未登録の構想 {iid!r}")

    char_ids = set(series.characters)
    for c in cont.get("character_changes", []) or []:
        if c.get("character") not in char_ids:
            errors.append(f"continuity.character_changes に未登録の人物: {c.get('character')!r}")
    for r in cont.get("relationship_changes", []) or []:
        pair = r.get("between") or []
        if len(pair) != 2 or any(p not in char_ids for p in pair):
            errors.append(f"continuity.relationship_changes.between が不正です: {pair!r}")
    return errors


def compose_instagram_caption(ep: dict[str, Any]) -> str:
    ig = (ep.get("posts") or {}).get("instagram") or {}
    caption = (ig.get("caption") or "").rstrip()
    tags = " ".join(ig.get("hashtags") or [])
    return f"{caption}\n\n{tags}" if tags else caption
