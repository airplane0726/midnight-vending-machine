"""履歴（state）の構築。

保存済みのエピソード（episodes/epNNN/episode.json）を正本とし、
series.json の initial_state に第1話から順に適用して state を作る。
そのため state.json は常に作り直せる（`python -m storyengine rebuild`）。
"""
from __future__ import annotations

import copy
from typing import Any

from .series import Series
from .storage import content_hash, read_json
from .validate import validate_against_history, validate_structure


class HistoryError(RuntimeError):
    """保存済みの履歴に矛盾がある。"""


def rel_key(a: str, b: str) -> str:
    return "|".join(sorted((a, b)))


def initial_state(series: Series) -> dict[str, Any]:
    init = series.config["initial_state"]
    state: dict[str, Any] = {
        "series_id": series.id,
        "_note": "このファイルは自動生成です。直接編集せず、python -m storyengine rebuild で作り直してください。",
        "next_episode": 1,
        "episodes": [],
        "canon_facts": [{"episode": 0, "text": t} for t in init.get("canon_facts", [])],
        "characters": {cid: {"status": c.get("initial_status", ""), "history": []}
                       for cid, c in series.characters.items()},
        "relationships": {},
        "featured_objects": {},
        "mysteries": {},
        "future_ideas_non_canon": {},
        "last_end_state": init.get("opening_situation", ""),
    }
    for r in init.get("relationships", []):
        a, b = r["between"]
        state["relationships"][rel_key(a, b)] = {
            "between": sorted((a, b)), "status": r["status"], "since_episode": 0, "history": []}
    for m in init.get("mysteries", []):
        state["mysteries"][m["id"]] = {"text": m["text"], "status": "open", "opened_in": 0,
                                       "notes": [], "resolved_in": None, "resolution": None}
    for idea in init.get("future_ideas", []):
        state["future_ideas_non_canon"][idea["id"]] = {"text": idea["text"], "proposed_in": 0}
    return state


def apply_episode(state: dict[str, Any], ep: dict[str, Any]) -> dict[str, Any]:
    """検証済みのエピソードを state に適用した新しい state を返す（元の state は変更しない）。"""
    s = copy.deepcopy(state)
    n = ep["episode_number"]
    cont = ep.get("continuity") or {}
    obj = ep.get("featured_object")

    s["episodes"].append({
        "episode_number": n,
        "title": ep["title"],
        "logline": ep.get("logline", ""),
        "summary": ep.get("summary", ""),
        "featured_object_id": obj.get("id") if isinstance(obj, dict) else None,
        "end_state": cont.get("end_state", ""),
        "content_hash": content_hash(ep),
    })
    s["next_episode"] = n + 1
    s["last_end_state"] = cont.get("end_state", "")

    for t in cont.get("new_facts", []) or []:
        s["canon_facts"].append({"episode": n, "text": t})

    if isinstance(obj, dict):
        reg = s["featured_objects"]
        if obj["id"] not in reg:
            reg[obj["id"]] = {"name": obj["name"], "description": obj.get("description", ""),
                              "properties": obj.get("properties", []), "first_episode": n,
                              "appearances": [n], "owner": obj.get("owner", "不明")}
        else:
            reg[obj["id"]]["appearances"].append(n)

    for c in cont.get("character_changes", []) or []:
        ch = s["characters"][c["character"]]
        ch["history"].append({"episode": n, "change": c["change"]})
        if c.get("status"):
            ch["status"] = c["status"]

    for r in cont.get("relationship_changes", []) or []:
        a, b = r["between"]
        key = rel_key(a, b)
        entry = s["relationships"].setdefault(
            key, {"between": sorted((a, b)), "status": "", "since_episode": n, "history": []})
        if entry["status"] != r["status"]:
            entry["history"].append({"episode": n, "from": entry["status"], "to": r["status"]})
            entry["status"] = r["status"]
            entry["since_episode"] = n

    for m in cont.get("mysteries_opened", []) or []:
        s["mysteries"][m["id"]] = {"text": m["text"], "status": "open", "opened_in": n,
                                   "notes": [], "resolved_in": None, "resolution": None}
    for m in cont.get("mysteries_progressed", []) or []:
        s["mysteries"][m["id"]]["notes"].append({"episode": n, "note": m["note"]})
    for m in cont.get("mysteries_resolved", []) or []:
        mm = s["mysteries"][m["id"]]
        mm.update(status="resolved", resolved_in=n, resolution=m["resolution"])

    for idea in cont.get("future_ideas", []) or []:
        s["future_ideas_non_canon"][idea["id"]] = {"text": idea["text"], "proposed_in": n}
    for iid in cont.get("future_ideas_retired", []) or []:
        s["future_ideas_non_canon"].pop(iid, None)
    return s


def load_episodes(series: Series) -> list[dict[str, Any]]:
    """保存済みエピソードを話数順に読み込む。欠番があれば HistoryError。"""
    if not series.episodes_dir.is_dir():
        return []
    eps = []
    for d in sorted(series.episodes_dir.iterdir()):
        if d.is_dir() and d.name.startswith("ep") and d.name[2:].isdigit():
            f = d / "episode.json"
            if not f.is_file():
                raise HistoryError(f"{d} に episode.json がありません")
            ep = read_json(f)
            if ep.get("episode_number") != int(d.name[2:]):
                raise HistoryError(f"{f} の episode_number がディレクトリ名と一致しません")
            eps.append(ep)
    for i, ep in enumerate(eps, start=1):
        if ep["episode_number"] != i:
            raise HistoryError(f"話数に欠番があります（第{i}話が見つかりません）")
    return eps


def replay(series: Series, episodes: list[dict[str, Any]]) -> dict[str, Any]:
    """initial_state に各話を順に適用し、各話の整合も再検証する。"""
    state = initial_state(series)
    for ep in episodes:
        errs = validate_structure(series, ep) + validate_against_history(series, state, ep)
        if errs:
            raise HistoryError(f"保存済みの第{ep.get('episode_number')}話に矛盾があります:\n- " + "\n- ".join(errs))
        state = apply_episode(state, ep)
    return state
