"""カット単位の動画の試作（技術確認）。

- episode.json のカットの `video_trial` に、目的・長さ・指示文・省いた動き・未解決の点を書く。
- 開始画像は、そのカットで採用中の開始画像（生成記録で再利用できるもの）。
- 1回の見積もりが上限を超えるものは送らない。自動で作り直さない。同じ条件で一度でも依頼したら、
  `again=True`（利用者の明示的な指示）がない限り二度目は送らない。
- 成功した動画は ffprobe で確認し、生成記録に登録して永続保存先へ送る（保存後にダウンロードして照合）。
"""
from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path
from typing import Any

from .integrations import AssetStore, VideoGenerator
from .media import _persist, inspect_media, load_records, plan_images, save_records
from .series import Series
from .state import load_episodes
from .storage import content_hash, exclusive_lock

TRIAL_VERSION = 1
EXPECTED_VIDEO_BYTES = 50 * 1024 * 1024


class TrialError(RuntimeError):
    pass


def plan(series: Series, n: int, cut_number: int, generator: VideoGenerator | None) -> dict[str, Any]:
    eps = {e["episode_number"]: e for e in load_episodes(series)}
    if n not in eps:
        raise TrialError(f"第{n}話は登録されていません")
    cut = next((c for c in eps[n]["cuts"] if c["cut_number"] == cut_number), None)
    if not cut or not cut.get("video_trial"):
        raise TrialError(f"カット{cut_number}に video_trial（試作の指示）がありません")
    trial = cut["video_trial"]
    p = {x.cut_number: x for x in plan_images(series, n)[0]}.get(cut_number)
    if not p or p.action != "reuse":
        raise TrialError(f"カット{cut_number}の採用済みの開始画像がありません")
    cfg = series.config.get("generation", {}).get("video") or {}
    spec = {"prompt": trial["prompt"], "duration_sec": int(trial["duration_sec"]),
            "aspect_ratio": series.format["aspect_ratio"]}
    cond = {"kind": "video_trial", "version": TRIAL_VERSION, "cut": cut_number, "spec": spec,
            "model": {"provider": cfg.get("provider"), "model": cfg.get("model"), "params": cfg.get("params") or {}},
            "init_image": p.current["file"], "init_image_sha256": p.current["sha256"]}
    slot = f"cut{cut_number:02d}_video_trial"
    versions = load_records(series, n)["slots"].get(slot, {}).get("versions", [])
    key = content_hash(cond)
    same = [v for v in versions if v["key"] == key]
    return {"slot": slot, "key": key, "conditions": cond, "spec": spec, "trial": trial,
            "init_image": p.current["file"], "previous_attempts": same,
            "estimate": generator.estimate(spec) if generator else None}


def _refetch(series: Series, n: int, pl: dict[str, Any], generator: VideoGenerator, store: AssetStore | None,
             task_id: str) -> dict[str, Any]:
    """記録済みの依頼（タスク）の結果を取り直す。新しい生成依頼は送らず、費用も加えない。"""
    known = [v for v in pl["previous_attempts"] if (v.get("task") or {}).get("id") == task_id]
    if not known:
        raise TrialError(f"タスク {task_id} はこの試作の記録にありません")
    out_dir = series.resolve(series.media_rel(n, "videos"))
    tmp = out_dir / f".tmp-{pl['slot']}-refetch-{os.getpid()}.mp4"
    try:
        generator.fetch(task_id, tmp)
        info = inspect_media(tmp)
    except Exception as e:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        raise TrialError(f"結果の取り直しに失敗しました（新しい依頼は送っていません）: {e}") from None
    final = out_dir / f"{pl['slot']}-{pl['key'].split(':', 1)[1][:16]}.mp4"
    os.replace(tmp, final)
    rec = load_records(series, n)
    entry = rec["slots"].setdefault(pl["slot"], {"kind": "video", "versions": []})
    version = {"key": pl["key"], "kind": "video_trial", "conditions": pl["conditions"], "status": "complete",
               "created_at": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat(),
               "task": {"id": task_id, "status": "SUCCEEDED"}, "cost_usd": 0.0,
               "cost_note": "同じタスクの結果を取り直したもの（費用は元の依頼の記録に計上済み）",
               "file": series.rel(final), "format": "mp4", "width": info["width"], "height": info["height"],
               "duration": round(info["duration"], 3), "bytes": info["bytes"], "sha256": info["sha256"]}
    version["storage"] = _persist(series, store, final, n)
    entry["versions"].append(version)
    save_records(series, n, rec)
    return {"action": "refetched", "version": version}


def run(series: Series, n: int, cut_number: int, generator: VideoGenerator, *,
        store: AssetStore | None, again: bool = False, resume_task: str | None = None) -> dict[str, Any]:
    with exclusive_lock(series.lock_path):
        pl = plan(series, n, cut_number, generator)
        done = [v for v in pl["previous_attempts"] if v.get("status") == "complete"]
        if done:
            return {"action": "reuse", "version": done[-1]}
        if resume_task:
            return _refetch(series, n, pl, generator, store, resume_task)
        if pl["previous_attempts"] and not again:
            raise TrialError("同じ条件の試作を一度依頼済みです。自動では作り直しません（明示の指示があるときだけ again を使う）")
        est = pl["estimate"]
        if not est or not est["within_cap"]:
            raise TrialError(f"見積もりが1回の上限を超えるため送りません: {est}")
        if store is not None:
            ok, msg = store.check_capacity(EXPECTED_VIDEO_BYTES)
            if not ok:
                raise TrialError(f"生成を中止しました（{msg}）")
        out_dir = series.resolve(series.media_rel(n, "videos"))
        out_dir.mkdir(parents=True, exist_ok=True)
        tmp = out_dir / f".tmp-{pl['slot']}-{os.getpid()}.mp4"
        rec = load_records(series, n)
        entry = rec["slots"].setdefault(pl["slot"], {"kind": "video", "versions": []})
        base = {"key": pl["key"], "kind": "video_trial", "conditions": pl["conditions"],
                "created_at": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat(),
                "cost_usd": est["usd"], "cost_note": "料金表からの見積もり。実際の請求は Runway の請求画面で確認する"}
        try:
            generator.generate(pl["spec"], series.resolve(pl["init_image"]), tmp)
            info = inspect_media(tmp)
        except Exception as e:  # noqa: BLE001 - 失敗は記録し、送り直さない
            tmp.unlink(missing_ok=True)
            entry["versions"].append(dict(base, status="failed", task=getattr(generator, "last_task", None),
                                          error=f"{type(e).__name__}: {e}"[:500]))
            save_records(series, n, rec)
            raise TrialError(f"試作に失敗しました（自動では作り直しません）: {e}") from None
        final = out_dir / f"{pl['slot']}-{pl['key'].split(':', 1)[1][:16]}.mp4"
        os.replace(tmp, final)
        version = dict(base, status="complete", task=getattr(generator, "last_task", None), file=series.rel(final),
                       format="mp4", width=info["width"], height=info["height"], duration=round(info["duration"], 3),
                       bytes=info["bytes"], sha256=info["sha256"])
        version["storage"] = _persist(series, store, final, n)
        entry["versions"].append(version)
        save_records(series, n, rec)
        return {"action": "generated", "version": version}
