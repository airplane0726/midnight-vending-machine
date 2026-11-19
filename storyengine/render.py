"""エピソード JSON から、読みやすい Markdown と制作用パッケージ（production.json）を作る。"""
from __future__ import annotations

from typing import Any

from .series import Series
from .validate import compose_instagram_caption


def _fmt_sec(x: float) -> str:
    return f"{x:.1f}秒"


PRODUCTION_SCHEMA_VERSION = 2


def reference_images(series: Series, character_ids: list[str], location_id: str | None) -> list[dict[str, Any]]:
    """カットに映る人物・場所の参照画像（series.json の reference_images）。未登録なら空。"""
    out: list[dict[str, Any]] = []
    for cid in character_ids:
        for r in series.characters.get(cid, {}).get("reference_images", []) or []:
            out.append({"subject": cid, "kind": "character", "path": r["path"], "view": r.get("view", "")})
    for r in (series.locations.get(location_id) or {}).get("reference_images", []) or []:
        out.append({"subject": location_id, "kind": "location", "path": r["path"], "view": r.get("view", "")})
    return out


def _join(parts: list[str]) -> str:
    """文として連結する（末尾に句点がなければ補う）。"""
    out = []
    for p in parts:
        p = (p or "").strip()
        if p:
            out.append(p if p[-1] in ".!?。" else p + ".")
    return " ".join(out)


def build_production(series: Series, ep: dict[str, Any]) -> dict[str, Any]:
    """画像・動画・音・投稿の各工程にそのまま渡せる形にまとめる。

    人物・場所の外見プロンプトを series.json から毎回合成するので、話をまたいで外見がぶれにくい。
    """
    style = series.config.get("style", {})
    fmt = series.format
    cuts_out = []
    timeline = []
    offset = 0.0
    for cut in ep["cuts"]:
        in_shot = cut.get("characters_in_shot", [])
        at_start = cut.get("characters_in_start_image", in_shot)
        entering = [c for c in in_shot if c not in at_start]
        loc = series.locations.get(cut.get("location"), {})
        parts = [style.get("image_prompt_prefix", ""), cut["image_prompt"]]
        if loc.get("appearance_prompt"):
            parts.append("Setting: " + loc["appearance_prompt"])
        for cid in at_start:
            c = series.characters[cid]
            parts.append(f"{c['name_en']}: {c['appearance_prompt']}")
        parts.append(style.get("image_prompt_suffix", ""))
        image_prompt = _join(parts)

        video_parts = [cut["video_prompt"]]
        for cid in entering:
            c = series.characters[cid]
            video_parts.append(f"{c['name_en']} (appears during the shot): {c['appearance_prompt']}")
        if cut.get("camera_en"):
            video_parts.append(f"Camera: {cut['camera_en']}")
        video_parts += [f"Duration: {cut['duration_sec']} seconds.", style.get("video_prompt_suffix", "")]
        video_prompt = _join(video_parts)

        for sfx in cut["sound"].get("sfx", []):
            timeline.append({"time_sec": round(offset + sfx["at_sec"], 2), "cut": cut["cut_number"],
                             "sound": sfx["sound"], "description": sfx.get("description", "")})
        n = ep["episode_number"]
        slot = f"cut{cut['cut_number']:02d}"
        cuts_out.append({
            "cut_number": cut["cut_number"],
            "start_sec": offset,
            "duration_sec": cut["duration_sec"],
            "image": {"slot": f"{slot}_start",
                      "prompt": image_prompt, "negative_prompt": style.get("negative_prompt", ""),
                      "aspect_ratio": fmt["aspect_ratio"], "resolution": fmt.get("resolution"),
                      "references": reference_images(series, at_start, cut.get("location")),
                      "output_dir": series.media_rel(n, "images")},
            "video": {"slot": slot,
                      "prompt": video_prompt, "negative_prompt": style.get("negative_prompt", ""),
                      "init_image_slot": f"{slot}_start",
                      "references": reference_images(series, in_shot, cut.get("location")),
                      "duration_sec": cut["duration_sec"], "aspect_ratio": fmt["aspect_ratio"],
                      "output_dir": series.media_rel(n, "videos")},
            "sound": cut["sound"],
        })
        offset += cut["duration_sec"]

    posts = ep["posts"]
    return {
        "_note": "自動生成ファイル。episode.json を編集して python -m storyengine render で作り直してください。",
        "schema_version": PRODUCTION_SCHEMA_VERSION,
        "paths_relative_to": "series_dir",
        "series_id": series.id,
        "episode_number": ep["episode_number"],
        "title": ep["title"],
        "format": {"aspect_ratio": fmt["aspect_ratio"], "resolution": fmt.get("resolution"),
                   "total_seconds": fmt["total_seconds"], "cut_durations": fmt["cut_durations"]},
        "cuts": cuts_out,
        "audio": {"music": ep.get("music", {}), "sfx_timeline": sorted(timeline, key=lambda x: x["time_sec"])},
        "publish": {
            "instagram": {"caption": compose_instagram_caption(ep)},
            "youtube": {"title": posts["youtube"]["title"], "description": posts["youtube"]["description"],
                        "tags": posts["youtube"].get("tags", []),
                        "made_for_kids": posts["youtube"].get("made_for_kids", False)},
            "ai_disclosure": series.config.get("ai_disclosure", ""),
        },
    }


def render_markdown(series: Series, ep: dict[str, Any]) -> str:
    chars = series.characters
    L: list[str] = []
    a = L.append
    a(f"# {series.config['title']} 第{ep['episode_number']}話「{ep['title']}」\n")
    a(f"> {ep.get('logline', '')}\n")
    fmt = series.format
    a(f"- 形式: 縦型 {fmt['aspect_ratio']} / {fmt['total_seconds']}秒 / {len(fmt['cut_durations'])}カット"
      f"（{'・'.join(str(d) + '秒' for d in fmt['cut_durations'])}）")
    a("- ナレーション・せりふ・字幕なし（表情・動作・効果音・音楽で伝える）\n")
    a("## あらすじ\n")
    a(ep.get("summary", "") + "\n")

    obj = ep.get("featured_object")
    if obj:
        label = fmt.get("featured_object", {}).get("label", "題材")
        a(f"## {label}：{obj['name']}\n")
        a(obj.get("description", "") + "\n")
        a("**性質（以後変更しない）**\n")
        for p in obj.get("properties", []):
            a(f"- {p}")
        a("")

    music = ep.get("music") or {}
    if music:
        a("## 音楽（全体）\n")
        for k, v in music.items():
            a(f"- **{k}**: {v}")
        a("")

    offset = 0.0
    for cut in ep["cuts"]:
        d = cut["duration_sec"]
        a(f"## カット{cut['cut_number']}「{cut.get('title', '')}」 {d}秒（{offset:g}〜{offset + d:g}秒）\n")
        names = "、".join(chars[c]["name"] for c in cut.get("characters_in_shot", []))
        a(f"- 登場: {names}")
        a(f"- カメラ: {cut.get('camera', '')}")
        a(f"- 前からのつながり: {cut['continuity']['carry_in']}")
        a(f"- 足の接地: {cut['continuity']['footing']}\n")
        a("**開始画像**\n")
        a(cut["start_image"] + "\n")
        a("**動き**\n")
        for b in cut.get("motion", []):
            a(f"- {_fmt_sec(b['start_sec'])}〜{_fmt_sec(b['end_sec'])}: {b['action']}")
        a(f"\n**終了状態**: {cut['continuity']['carry_out']}\n")
        a("**音**\n")
        snd = cut["sound"]
        if snd.get("ambience"):
            a(f"- 環境音: {snd['ambience']}")
        for s in snd.get("sfx", []):
            a(f"- {_fmt_sec(s['at_sec'])}: {s['sound']} — {s.get('description', '')}")
        a(f"- 音楽: {snd.get('music', '')}\n")
        a("<details><summary>画像生成指示 / 動画生成指示</summary>\n")
        a(f"**image_prompt**\n\n```\n{cut['image_prompt']}\n```\n")
        a(f"**video_prompt**\n\n```\n{cut['video_prompt']}\n```\n")
        a("</details>\n")
        offset += d

    posts = ep["posts"]
    a("## 投稿文\n")
    a("### Instagram\n")
    a("```\n" + compose_instagram_caption(ep) + "\n```\n")
    a("### YouTube\n")
    a(f"**タイトル**: {posts['youtube']['title']}\n")
    a("**説明文**\n")
    a("```\n" + posts["youtube"]["description"] + "\n```\n")
    if posts["youtube"].get("tags"):
        a("**タグ**: " + ", ".join(posts["youtube"]["tags"]) + "\n")

    cont = ep.get("continuity") or {}
    a("## 連続性メモ（履歴に反映される内容）\n")
    a(f"**この話の終わりの状況**: {cont.get('end_state', '')}\n")
    sections = [
        ("確定した出来事", [f"- {t}" for t in cont.get("new_facts", [])]),
        ("人物の変化", [f"- {chars[c['character']]['name']}: {c['change']}" for c in cont.get("character_changes", [])]),
        ("関係の変化", [f"- {'×'.join(chars[x]['name'] for x in r['between'])}: {r['status']}"
                    for r in cont.get("relationship_changes", [])]),
        ("新しい謎", [f"- `{m['id']}` {m['text']}" for m in cont.get("mysteries_opened", [])]),
        ("進展した謎", [f"- `{m['id']}` {m['note']}" for m in cont.get("mysteries_progressed", [])]),
        ("解決した謎", [f"- `{m['id']}` {m['resolution']}" for m in cont.get("mysteries_resolved", [])]),
        ("今後の構想（未確定・正史ではない）", [f"- `{i['id']}` {i['text']}" for i in cont.get("future_ideas", [])]),
    ]
    for head, items in sections:
        if items:
            a(f"### {head}\n")
            L.extend(items)
            a("")
    return "\n".join(L).rstrip() + "\n"
