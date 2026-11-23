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
  persist   生成済み画像を永続保存先へ送る（以前の版も含む）
  restore   永続保存した画像を取り戻す（生成はしない）
  import-zip 記録と一致する画像だけを ZIP から戻す
  images    カットの開始画像を生成（--dry-run で確認のみ。生成サービス未接続なら生成しない）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from .commit import commit, plan_commit, rebuild_state
from .integrations import NotConfiguredError
from .media import GenerationError
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
        if l.get("fixed_layout"):
            a(f"    - 位置関係: {l['fixed_layout']}")
        props = {k: v for k, v in (l.get("proportions") or {}).items() if not k.startswith("_")}
        if props:
            a("    - 比率: " + ", ".join(f"{k}={v}" for k, v in props.items()))
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


def _latest_episode(series: Series) -> int:
    eps = load_episodes(series)
    if not eps:
        raise HistoryError("登録済みのエピソードがありません")
    return eps[-1]["episode_number"]


IMAGE_INPUT_MARGIN_USD = 0.05  # 参照画像・指示文の入力分（実績は約$0.04）を安全側に見込む


def _image_budget_guard(series: Series, n: int, generator, args):
    from . import budget

    def guard(plan):
        est = generator.estimate(plan.spec) or {}
        usd = (est.get("output_usd") or 0.0) + IMAGE_INPUT_MARGIN_USD
        budget.check(usd, series=series.id, episode=n, allow_over_episode=args.allow_over_episode_cap)
    return guard


def cmd_budget(series: Series, args) -> int:
    from . import budget

    try:
        s = budget.summary(month=args.month, series=series.id, episode=args.episode)
        rows = budget.spends()
    except budget.BudgetError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 2
    print(f"{s['month']} の制作費（1ドル={s['rate']}円で換算）")
    print(f"  消費: {s['month_spent_jpy']:.0f}円（${s['month_spent_usd']:.4f}）/ 月の上限 {s['monthly_cap_jpy']}円"
          f" / 残り {s['month_remaining_jpy']:.0f}円")
    per_ep: dict = {}
    for r in rows:
        if r["month"] == s["month"]:
            k = (r.get("series"), r.get("episode"))
            per_ep[k] = per_ep.get(k, 0.0) + r["usd"]
    for (sid, ep), usd in sorted(per_ep.items(), key=lambda x: (str(x[0][0]), x[0][1] or 0)):
        print(f"  {sid} 第{ep}話: {usd * s['rate']:.0f}円（${usd:.4f}）/ 1本の上限 {s['per_episode_cap_jpy']}円（原則）")
    print("  前払いの入金（消費とは別）:")
    for d in s["deposits"]:
        if "face_value" in d:
            fv, paid = d["face_value"], d.get("paid") or {}
            shown = paid.get("shown_on_purchase_screen") or {}
            receipt = paid.get("receipt")
            paid_txt = (f"領収書 {receipt}" if receipt else
                        f"購入画面 {shown.get('jpy')}円（${shown.get('usd')}）・{paid.get('status', '')}")
            print(f"    {d.get('month', '')} {d['provider']} 額面 {fv.get('credits')}クレジット（${fv.get('usd')}）"
                  f" / 支払額 {paid_txt} / 自動入金:{'ON' if d.get('auto_topup') else 'OFF'}")
        else:
            print(f"    {d.get('month', d.get('date', ''))} {d['provider']} {d['amount']} {d['currency']} 自動入金:{'ON' if d.get('auto_topup') else 'OFF'}")
    for d in s["planned_deposits"]:
        print(f"    予定 {d['provider']} {d['amount']} {d['currency']}（{d['status']}）")
    return 0


def cmd_images(series: Series, args) -> int:
    from . import media
    from .integrations import NotConfiguredError, create_asset_store, create_image_generator

    n = args.episode or _latest_episode(series)
    cuts = [int(x) for x in args.cuts.split(",")] if args.cuts else None
    gen_cfg = series.config.get("generation", {}).get("image")
    store_cfg = series.config.get("asset_storage")
    try:
        generator = create_image_generator(gen_cfg)
        store = create_asset_store(store_cfg)
    except NotConfiguredError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 2
    model = media.model_descriptor(gen_cfg)
    print(f"第{n}話 画像生成{'（dry-run：確認のみ）' if args.dry_run else ''}")
    print(f"  画像生成サービス: {model['provider'] or '未接続'} / モデル: {model['model'] or '未設定'}"
          f" / シード: {'記録する' if generator and generator.supports_seed else '使わない（未対応または未接続）'}")
    if store is None:
        print("  永続保存先: 未設定（生成しても保存完了になりません）")
    else:
        try:
            _, cap = store.check_capacity(0)
        except Exception as e:  # noqa: BLE001
            cap = f"接続・容量を確認できません（{e}）"
        print(f"  永続保存先: {store.provider} / {cap}")
    print(f"  パスの基準: {os.path.relpath(series.root)}/（連載ディレクトリ）")

    if args.dry_run:
        plans, notes = media.plan_images(series, n, cuts=cuts, store=store)
    elif generator is not None and generator.paid and not args.allow_paid:
        plans, notes = media.plan_images(series, n, cuts=cuts, store=store)
        todo = [p for p in plans if p.action not in ("reuse", "restore", "blocked")]
        if todo:
            print(f"  有料の生成が {len(todo)} カット分必要です。見積もりを確認してから --allow-paid を付けて実行してください。")
            print("  （--dry-run で見積もりを表示できます。--cuts 1 で1カットだけに絞れます）")
            return 2
        result = media.run_images(series, n, generator, store=store, cuts=cuts,
                                  before_paid=_image_budget_guard(series, n, generator, args))
        notes = result.notes
    else:
        result = media.run_images(series, n, generator, store=store, cuts=cuts)
        notes = result.notes
        plans, _ = media.plan_images(series, n, cuts=cuts, store=store)
    for note in notes:
        print(f"  [注意] {note}")
    for p in plans:
        print(f"\n  カット{p.cut_number}（{p.slot}）: {media.ACTION_LABEL[p.action]}")
        if p.reason:
            print(f"    理由: {p.reason}")
        if p.references:
            for r in p.references:
                if r.get("kind") == "generated":
                    print(f"    参照画像: {r.get('path') or '（未完成）'}（生成済み {r['slot']}、{'あり' if r.get('path') else 'なし'}）")
                    continue
                mark = "なし" if r["path"] in p.missing_references else "あり"
                print(f"    参照画像: {r['path']}（{r['subject']}、{mark}）")
        else:
            print("    参照画像: 未登録（series.json の reference_images に追加すると使われます）")
        print(f"    保存先: {p.planned_file}")
        if generator is not None and p.action not in ("reuse", "restore", "blocked"):
            est = generator.estimate(p.spec)
            if est:
                out = f"約${est['output_usd']:.5f}（{est['output_tokens']} トークン）" if est.get("output_usd") is not None else "未確定"
                print(f"    見積もり: {est['model']} {est['size']} quality={est['quality']} / 出力 {out}"
                      f" / 入力（参照画像{est['reference_images']}枚＋指示文）未確定")
        if p.current:
            print(f"    永続保存: {(p.current.get('storage') or {}).get('status', 'local_only')}")
        for old in p.previous:
            print(f"    以前の画像（保持）: {old}")
        print(f"    条件キー: {p.key}")
    print()
    if not args.dry_run:
        for c in result.done:
            print(f"  カット{c}: 完了")
        for c, err in result.failed:
            print(f"  カット{c}: 失敗 — {err}")
    print(media.storage_summary(plans, store))
    if not args.dry_run and result.failed:
        print("成功したカットは記録済みです。原因を直して同じコマンドを再実行すると、続きから再開します。")
        return 1
    return 0


def cmd_persist(series: Series, args) -> int:
    from . import media
    from .integrations import NotConfiguredError, create_asset_store

    try:
        store = create_asset_store(series.config.get("asset_storage"))
    except (NotConfiguredError, RuntimeError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 2
    if store is None:
        print("永続保存先が未設定です（series.json の asset_storage.provider）。保存完了ではありません。")
        return 2
    eps = [args.episode] if args.episode else [e["episode_number"] for e in load_episodes(series)]
    results = media.persist_all(series, store, eps)
    for f, status, detail in results:
        print(f"  {f}: {status} {detail}")
    if not results:
        print("  未保存の完成画像はありません。")
    return 1 if any(st != "persisted" for _, st, _ in results) else 0


def cmd_restore(series: Series, args) -> int:
    from . import media
    from .integrations import NotConfiguredError, create_asset_store

    try:
        store = create_asset_store(series.config.get("asset_storage"))
    except (NotConfiguredError, RuntimeError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 2
    if store is None:
        print("永続保存先が未設定です。")
        return 2
    eps = [args.episode] if args.episode else [e["episode_number"] for e in load_episodes(series)]
    results = media.restore_all(series, store, eps, check_all=args.check)
    for f, status, detail in results:
        print(f"  {f}: {status} {detail}")
    if not results:
        print("  永続保存済みの画像がありません。")
    return 1 if any(st == "failed" for _, st, _ in results) or not results else 0


def cmd_import_zip(series: Series, args) -> int:
    from . import media

    eps = [args.episode] if args.episode else [e["episode_number"] for e in load_episodes(series)]
    results = media.import_zip(series, Path(args.zip), eps)
    for f, status in results:
        print(f"  {f}: {status}")
    return 0


def cmd_drive_token(series: Series, args) -> int:
    from . import drive_token as dt
    from . import google_drive

    if args.action == "begin":
        print(json.dumps(dt.begin(args.mode), ensure_ascii=False, indent=2))
        return 0
    if args.action == "cleanup":
        print(f"削除した依頼: {dt.cleanup_all()} 件")
        return 0
    # finish
    if not args.request_id or not args.log:
        print("エラー: --request-id と --log が必要です", file=sys.stderr)
        return 2
    try:
        token, header = dt.finish(args.request_id, Path(args.log))
    except dt.TokenError as e:
        print(f"エラー: {e}（秘密鍵とログファイルは削除しました）", file=sys.stderr)
        return 1
    try:
        digest = hashlib.sha256(token.encode()).hexdigest()[:12]
        print(f"復号成功: mode={header['mode']} / 依頼ID一致 / 改ざんなし / トークン長 {len(token)} 文字（SHA-256 先頭 {digest}）")
        if header.get("quota"):
            q = header["quota"]
            if q.get("limit"):
                free = (int(q["limit"]) - int(q.get("usage") or 0)) / 1024 ** 3
                print(f"  Actions 側でのドライブ確認: 空き {free:.2f} GB")
        then = list(args.then or [])
        if not then:
            return 0
        if header["mode"] != "google":
            print("dummy のトークンでは後続のコマンドを実行しません")
            return 0
        google_drive.set_session_token(token, header["issued_at"] + int(header["expires_in"]) - 120)
        # 「+」で区切って複数のコマンドを順に実行する（失敗したらそこで止める）
        commands, cur = [], []
        for a in then:
            if a == "+":
                commands.append(cur)
                cur = []
            else:
                cur.append(a)
        commands.append(cur)
        code = 0
        for c in (c for c in commands if c):
            print(f"--- {' '.join(c)}")
            code = main(["--series", str(series.root), *c])
            if code != 0:
                break
        return code
    finally:
        google_drive.clear_session_token()
        del token


def cmd_animatic(series: Series, args) -> int:
    from . import animatic

    n = args.episode or _latest_episode(series)
    try:
        res = animatic.build(series, n)
    except animatic.AnimaticError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    v = res["version"]
    print(f"第{n}話の構成確認用動画（{'再利用' if res['action'] == 'reuse' else '作成'}）: {v['file']}")
    print(f"  {v['width']}x{v['height']} / {v['duration']:.2f}秒 / {v['bytes'] / 1024 ** 2:.1f}MB / 費用 $0（追加の生成なし）")
    for c in v["conditions"]["cuts"]:
        print(f"  カット{c['cut_number']}: {c['duration_sec']}秒 zoom {c['move']['zoom']} focus {c['move']['focus']}")
    print(f"  永続保存: {v['storage']['status']}（persist で Google ドライブへ）")
    return 0


def cmd_video_trial(series: Series, args) -> int:
    from . import video_trial as vt
    from .integrations import NotConfiguredError, create_asset_store, create_video_generator

    n = args.episode or _latest_episode(series)
    try:
        gen = create_video_generator(series.config.get("generation", {}).get("video"))
        store = create_asset_store(series.config.get("asset_storage"))
        pl = vt.plan(series, n, args.cut, gen)
    except (vt.TrialError, NotConfiguredError, RuntimeError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 2
    t = pl["trial"]
    print(f"第{n}話 カット{args.cut} 動画の試作（{t.get('purpose', '')}）")
    print(f"  開始画像: {pl['init_image']}")
    print(f"  長さ: {pl['spec']['duration_sec']}秒 / 縦横: {pl['spec']['aspect_ratio']}")
    est = pl["estimate"]
    if est:
        print(f"  見積もり: {est['model']} ${est['usd']:.2f}（1回の上限 ${est['max_cost_usd']:.2f}、"
              f"{'範囲内' if est['within_cap'] else '上限超過のため送らない'}）")
    else:
        print("  動画生成サービス: 未接続")
    for o in t.get("omitted", []):
        print(f"  省く動き: {o}")
    if t.get("unresolved"):
        print(f"  未解決: {t['unresolved']}")
    if pl["previous_attempts"]:
        print(f"  同じ条件の依頼: {len(pl['previous_attempts'])} 回（自動では作り直さない）")
    if args.dry_run:
        print("  dry-run のため送っていません。")
        return 0
    if gen is None:
        print("エラー: 動画生成サービスが未接続です", file=sys.stderr)
        return 2
    if gen.paid and not args.allow_paid:
        print("  有料です。見積もりを確認してから --allow-paid を付けて実行してください。")
        return 2
    from . import budget
    try:
        budget.check(est["usd"], series=series.id, episode=n, allow_over_episode=args.allow_over_episode_cap)
    except budget.BudgetError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    try:
        res = vt.run(series, n, args.cut, gen, store=store, again=args.again)
    except vt.TrialError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    v = res["version"]
    print(f"  {'再利用' if res['action'] == 'reuse' else '完成'}: {v['file']}（{v['duration']}秒、"
          f"永続保存: {(v.get('storage') or {}).get('status')}）")
    return 0


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
    ps = sub.add_parser("persist", help="完成済みでまだ永続保存されていない生成画像を保存先へ送る")
    ps.add_argument("--episode", type=int)
    rs = sub.add_parser("restore", help="永続保存した画像を取り戻す（生成はしない）。--check で全件を照合")
    rs.add_argument("--episode", type=int)
    rs.add_argument("--check", action="store_true", help="手元にある画像も保存先からダウンロードして照合する")
    iz = sub.add_parser("import-zip", help="記録と一致する画像だけを ZIP から手元に戻す（生成・通信なし）")
    iz.add_argument("zip")
    iz.add_argument("--episode", type=int)
    dtp = sub.add_parser("drive-token", help="GitHub Actions から暗号化した Google ドライブ用トークンを受け取る")
    dtp.add_argument("action", choices=["begin", "finish", "cleanup"])
    dtp.add_argument("--mode", choices=["dummy", "google"], default="dummy")
    dtp.add_argument("--request-id")
    dtp.add_argument("--log", help="ワークフローのログを保存したファイル（処理後に削除）")
    dtp.add_argument("--then", nargs=argparse.REMAINDER, default=[],
                     help="最後に置く。トークンを使って続けて実行する storyengine のコマンド。複数は + で区切る（例: --then images --dry-run + restore --check）")
    an = sub.add_parser("animatic", help="採用済みの開始画像だけで構成確認用の動画を作る（無料）")
    an.add_argument("--episode", type=int)
    vtp = sub.add_parser("video-trial", help="カット単位の動画の試作（技術確認）。有料は --allow-paid が必要、自動で作り直さない")
    vtp.add_argument("--episode", type=int)
    vtp.add_argument("--cut", type=int, required=True)
    vtp.add_argument("--dry-run", action="store_true")
    vtp.add_argument("--allow-paid", action="store_true")
    vtp.add_argument("--again", action="store_true", help="同じ条件で依頼済みでも、利用者の明示の指示でもう一度だけ送る")
    vtp.add_argument("--allow-over-episode-cap", action="store_true")
    bp = sub.add_parser("budget", help="今月の制作費（消費）と上限、前払いの入金を表示")
    bp.add_argument("--month", help="YYYY-MM（既定: 今月）")
    bp.add_argument("--episode", type=int)
    im = sub.add_parser("images", help="カットの開始画像を生成（--dry-run で確認のみ）")
    im.add_argument("--episode", type=int, help="話数（既定: 最新話）")
    im.add_argument("--cuts", help="対象カット（例: 1,3）。既定: 全カット")
    im.add_argument("--dry-run", action="store_true", help="生成せず、対象・参照画像・保存先・再利用可否・見積もりを表示")
    im.add_argument("--allow-paid", action="store_true", help="費用のかかる生成サービスで実際に生成する（見積もり確認後に指定）")
    im.add_argument("--allow-over-episode-cap", action="store_true", help="1本の上限（原則）を超えることを利用者が明示的に認めた場合だけ")
    sub.add_parser("status")
    args = p.parse_args(argv)

    handlers = {"brief": cmd_brief, "scaffold": cmd_scaffold, "validate": cmd_validate,
                "commit": cmd_commit, "render": cmd_render, "rebuild": cmd_rebuild, "status": cmd_status, "images": cmd_images, "persist": cmd_persist,
                "restore": cmd_restore, "import-zip": cmd_import_zip,
                "drive-token": cmd_drive_token, "animatic": cmd_animatic,
                "video-trial": cmd_video_trial, "budget": cmd_budget}
    try:
        series = load_series(args.series)
        return handlers[args.cmd](series, args)
    except LockBusyError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 2
    except (HistoryError, FileNotFoundError, ValueError, GenerationError, NotConfiguredError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 2
