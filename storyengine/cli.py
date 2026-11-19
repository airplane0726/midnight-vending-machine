"""コマンドライン。

    python -m storyengine <command> [--series series/<id>] ...

commands:
  brief     次の話を書くための設定・履歴の要約を表示
  scaffold  次の話の下書きひな形を drafts/epNNN.json に作成
  validate  下書きを検証（ディスクは変更しない）
  commit    検証に通った下書きを登録し、履歴（state.json）を更新
  render    保存済みエピソードの episode.md / production.json を作り直す
  rebuild   保存済みエピソードから state.json を作り直す
  status    次の話数と履歴の整合を表示
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .commit import commit, plan_commit, rebuild_state
from .render import build_production, render_markdown
from .series import Series, load_series
from .state import HistoryError, load_episodes, replay
from .storage import LockBusyError, atomic_write_json, atomic_write_text, read_json

DEFAULT_SERIES = "series/midnight-vending-machine"


def _load_draft(path: str) -> dict[str, Any]:
    try:
        return read_json(Path(path))
    except json.JSONDecodeError as e:
        raise SystemExit(f"JSON の構文エラー: {path}: {e}")


def _print_errors(errors: list[str]) -> None:
    print(f"検証失敗（{len(errors)}件）:")
    for e in errors:
        print(f"  - {e}")


def cmd_brief(series: Series, args) -> int:
    cfg = series.config
    state = replay(series, load_episodes(series))
    fmt = series.format
    out: list[str] = []
    a = out.append
    a(f"# 制作ブリーフ: {cfg['title']}（次は第{state['next_episode']}話）\n")
    a("## 作品の前提\n" + cfg.get("premise", "") + "\n")
    if cfg.get("series_arc"):
        a("## 連載全体の方向性\n" + cfg["series_arc"] + "\n")
    a("## 形式")
    a(f"- {fmt['aspect_ratio']} / {fmt['total_seconds']}秒 / カット尺 {fmt['cut_durations']}")
    a(f"- せりふ・ナレーション・字幕: {'可' if fmt.get('spoken_text_allowed') else '使わない'}\n")
    a("## 制作ルール")
    for r in cfg.get("writing_rules", []):
        a(f"- {r}")
    a("\n## 人物（外見は固定）")
    for cid, c in series.characters.items():
        st = state["characters"][cid]["status"]
        a(f"- `{cid}` {c['name']}（{c.get('role', '')}）現在: {st}")
        for x in c.get("appearance_ja", []):
            a(f"    - {x}")
    a("\n## 場所")
    for lid, l in series.locations.items():
        a(f"- `{lid}` {l['name']}: " + " / ".join(l.get("appearance_ja", [])))
    a("\n## 関係")
    for r in state["relationships"].values():
        a(f"- {'×'.join(r['between'])}: {r['status']}（第{r['since_episode']}話〜）")
    a("\n## 登場済みの品物（性質は変更禁止。同じ品物・同名は新規に使えない）")
    for oid, o in state["featured_objects"].items():
        a(f"- `{oid}` {o['name']}（第{o['first_episode']}話）持ち主: {o['owner']}")
        for p in o["properties"]:
            a(f"    - {p}")
    a("\n## 確定した出来事（正史）")
    for f in state["canon_facts"]:
        a(f"- [{'初期設定' if f['episode'] == 0 else '第' + str(f['episode']) + '話'}] {f['text']}")
    a("\n## 未回収の謎")
    for mid, m in state["mysteries"].items():
        if m["status"] == "open":
            notes = " / ".join(f"第{x['episode']}話: {x['note']}" for x in m["notes"])
            a(f"- `{mid}` {m['text']}" + (f"（進展: {notes}）" if notes else ""))
    a("\n## 今後の構想（未確定。採用しても、しなくてもよい）")
    for iid, i in state["future_ideas_non_canon"].items():
        a(f"- `{iid}` {i['text']}（第{i['proposed_in']}話で提案）")
    a("\n## 直近の話")
    for e in state["episodes"][-args.recent:]:
        a(f"- 第{e['episode_number']}話「{e['title']}」: {e['summary']}")
    a(f"\n## 前話の終わりの状況（次話はここから続ける）\n{state['last_end_state']}\n")
    a(f"## 次の作業\n- 下書き: {os.path.relpath(series.draft_path(state['next_episode']))}")
    print("\n".join(out))
    return 0


def cmd_scaffold(series: Series, args) -> int:
    state = replay(series, load_episodes(series))
    n = state["next_episode"]
    path = series.draft_path(n)
    if path.exists() and not args.force:
        print(f"下書きがすでにあります: {path}（上書きするには --force）")
        return 1
    tmpl = read_json(Path(__file__).parent / "templates" / "episode_template.json")
    tmpl["series_id"] = series.id
    tmpl["episode_number"] = n
    base_cut = tmpl.pop("cuts")[0]
    tmpl["cuts"] = []
    for i, d in enumerate(series.format["cut_durations"], start=1):
        c = json.loads(json.dumps(base_cut))
        c["cut_number"] = i
        c["duration_sec"] = d
        tmpl["cuts"].append(c)
    atomic_write_json(path, tmpl)
    print(f"ひな形を作成しました: {path}")
    return 0


def cmd_validate(series: Series, args) -> int:
    draft = _load_draft(args.draft)
    plan = plan_commit(series, draft, replace=args.replace)
    if not plan.ok:
        _print_errors(plan.errors)
        return 1
    if plan.action == "unchanged":
        print(f"検証成功: 第{plan.episode_number}話は同じ内容で登録済みです（再登録しても変化なし）")
    else:
        total = sum(c["duration_sec"] for c in draft["cuts"])
        print(f"検証成功: 第{plan.episode_number}話「{draft['title']}」"
              f" {len(draft['cuts'])}カット / 合計{total:g}秒 / 登録時の動作: {plan.action}")
    return 0


def cmd_commit(series: Series, args) -> int:
    draft = _load_draft(args.draft)
    plan, notes = commit(series, draft, replace=args.replace)
    for n in notes:
        print(f"[復旧] {n}")
    if not plan.ok:
        _print_errors(plan.errors)
        print("履歴は変更していません。")
        return 1
    msg = {"create": "登録しました", "replace": "差し替えました",
           "unchanged": "同じ内容で登録済みのため、変更はありません"}[plan.action]
    print(f"第{plan.episode_number}話: {msg}")
    print(f"  {series.episode_dir(plan.episode_number)}")
    print(f"  次の話数: {plan.state_after['next_episode']}")
    return 0


def cmd_render(series: Series, args) -> int:
    eps = load_episodes(series)
    targets = eps if args.episode is None else [e for e in eps if e["episode_number"] == args.episode]
    if not targets:
        print("対象のエピソードがありません")
        return 1
    for ep in targets:
        d = series.episode_dir(ep["episode_number"])
        atomic_write_text(d / "episode.md", render_markdown(series, ep))
        atomic_write_json(d / "production.json", build_production(series, ep))
        print(f"作り直しました: {d}")
    return 0


def cmd_rebuild(series: Series, args) -> int:
    state = rebuild_state(series)
    print(f"state.json を作り直しました（登録済み {len(state['episodes'])}話 / 次は第{state['next_episode']}話）")
    return 0


def cmd_status(series: Series, args) -> int:
    eps = load_episodes(series)
    state = replay(series, eps)
    print(f"連載: {series.config['title']} ({series.id})")
    print(f"登録済み: {len(eps)}話 / 次の話数: {state['next_episode']}")
    if not series.state_path.exists():
        print("state.json: 未作成（rebuild で作成できます）")
        return 0
    if read_json(series.state_path) == state:
        print("state.json: 保存済みエピソードと一致")
        return 0
    print("state.json: 保存済みエピソードと不一致（python -m storyengine rebuild で修復できます）")
    return 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="storyengine", description="連載ショート動画の制作管理")
    p.add_argument("--series", default=DEFAULT_SERIES, help=f"連載ディレクトリ（既定: {DEFAULT_SERIES}）")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("brief"); b.add_argument("--recent", type=int, default=5)
    s = sub.add_parser("scaffold"); s.add_argument("--force", action="store_true")
    for name in ("validate", "commit"):
        x = sub.add_parser(name)
        x.add_argument("draft")
        x.add_argument("--replace", action="store_true", help="内容の違う最新話を差し替える")
    r = sub.add_parser("render"); r.add_argument("--episode", type=int)
    sub.add_parser("rebuild")
    sub.add_parser("status")
    args = p.parse_args(argv)

    handlers = {"brief": cmd_brief, "scaffold": cmd_scaffold, "validate": cmd_validate,
                "commit": cmd_commit, "render": cmd_render, "rebuild": cmd_rebuild, "status": cmd_status}
    try:
        series = load_series(args.series)
        return handlers[args.cmd](series, args)
    except LockBusyError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 2
    except (HistoryError, FileNotFoundError, ValueError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 2
