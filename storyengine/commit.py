"""下書きの検証と、履歴への登録（コミット）。

安全性の方針:
- 書き込み前に、メモリ上で「既存の全話 + 下書き」を再生して検証を終える。
- エピソードは一時ディレクトリに書いてから rename で配置する（中途半端な ep ディレクトリを残さない）。
- state.json は最後に原子的に置き換える。失敗したらエピソードの配置を元に戻す。
- 同じ内容の再実行は何も変えずに成功する（冪等）。内容が違う同じ話数はエラー（--replace で最新話のみ差し替え可）。
- 連載ごとのロックで同時実行を防ぐ。
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .render import build_production, render_markdown
from .series import Series
from .state import apply_episode, load_episodes, replay
from .storage import atomic_write_json, atomic_write_text, content_hash, exclusive_lock, pretty_json, read_json
from .validate import validate_against_history, validate_structure


@dataclass
class Plan:
    action: str  # create / replace / unchanged / invalid
    episode_number: Any
    errors: list[str] = field(default_factory=list)
    state_after: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return not self.errors


def plan_commit(series: Series, draft: dict[str, Any], *, replace: bool = False) -> Plan:
    """ディスクを変更せずに、登録した場合の結果を計算する。"""
    errors = validate_structure(series, draft)
    n = draft.get("episode_number") if isinstance(draft, dict) else None
    if not isinstance(n, int) or isinstance(n, bool):
        return Plan("invalid", n, errors or ["episode_number がありません"])

    episodes = load_episodes(series)
    existing = {e["episode_number"]: e for e in episodes}

    if n in existing:
        if content_hash(existing[n]) == content_hash(draft):
            return Plan("unchanged", n, [], replay(series, episodes))
        if not replace:
            return Plan("invalid", n, errors + [
                f"第{n}話はすでに別の内容で登録されています（話数の重複）。"
                f"最新話を差し替える場合のみ --replace を使えます"])
        if n != max(existing):
            return Plan("invalid", n, errors + [f"差し替えできるのは最新話（第{max(existing)}話）だけです"])
        base = replay(series, [e for e in episodes if e["episode_number"] < n])
        action = "replace"
    else:
        base = replay(series, episodes)
        action = "create"

    errors += validate_against_history(series, base, draft)
    if errors:
        return Plan("invalid", n, errors)
    return Plan(action, n, [], apply_episode(base, draft))


MANAGED_FILES = ("episode.json", "episode.md", "production.json")


def _link_or_copy(src: str, dst: str) -> str:
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)
    return dst


def _carry_over_extras(old_dir: Path, new_dir: Path) -> None:
    """差し替え時、エピソードのディレクトリにある管理外のファイル（旧形式の assets/ など）を引き継ぐ。

    ハードリンク（できなければコピー）なので、差し替えが失敗して元に戻すときも元のファイルは残る。
    """
    for child in old_dir.iterdir():
        if child.name in MANAGED_FILES or child.name.startswith(".tmp-"):
            continue
        dst = new_dir / child.name
        if child.is_dir():
            shutil.copytree(child, dst, copy_function=_link_or_copy, symlinks=True)
        else:
            _link_or_copy(str(child), str(dst))


def _write_episode_files(series: Series, ep: dict[str, Any], target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target / "episode.json", pretty_json(ep))
    atomic_write_text(target / "episode.md", render_markdown(series, ep))
    atomic_write_json(target / "production.json", build_production(series, ep))


def recover(series: Series) -> list[str]:
    """前回の中断で残った一時ディレクトリを片付ける。"""
    notes = []
    d = series.episodes_dir
    if not d.is_dir():
        return notes
    for p in d.iterdir():
        if p.name.startswith(".staging-"):
            shutil.rmtree(p, ignore_errors=True)
            notes.append(f"中断された一時ディレクトリを削除: {p.name}")
        elif p.name.startswith(".backup-"):
            target = d / p.name[len(".backup-"):]
            if target.exists():
                shutil.rmtree(p, ignore_errors=True)
                notes.append(f"差し替え前のバックアップを削除: {p.name}")
            else:
                os.rename(p, target)
                notes.append(f"中断された差し替えを元に戻しました: {target.name}")
    return notes


def commit(series: Series, draft: dict[str, Any], *, replace: bool = False) -> tuple[Plan, list[str]]:
    with exclusive_lock(series.lock_path):
        notes = recover(series)
        plan = plan_commit(series, draft, replace=replace)
        if not plan.ok:
            return plan, notes
        n = plan.episode_number
        target = series.episode_dir(n)

        if plan.action == "unchanged":
            # 前回が state 書き込み前に止まっていた場合などを修復する
            current = read_json(series.state_path) if series.state_path.exists() else None
            if current != plan.state_after:
                atomic_write_json(series.state_path, plan.state_after)
                notes.append("state.json を保存済みのエピソードに合わせて修復しました")
            for name in ("episode.md", "production.json"):
                if not (target / name).exists():
                    _write_episode_files(series, read_json(target / "episode.json"), target)
                    notes.append("欠けていた生成ファイルを作り直しました")
                    break
            return plan, notes

        series.episodes_dir.mkdir(parents=True, exist_ok=True)
        staging = series.episodes_dir / f".staging-{target.name}-{os.getpid()}"
        backup = series.episodes_dir / f".backup-{target.name}"
        try:
            _write_episode_files(series, draft, staging)
            if target.exists():
                _carry_over_extras(target, staging)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        moved_old = False
        if target.exists():
            os.rename(target, backup)
            moved_old = True
        os.rename(staging, target)
        try:
            atomic_write_json(series.state_path, plan.state_after)
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            if moved_old:
                os.rename(backup, target)
            raise
        if moved_old:
            shutil.rmtree(backup, ignore_errors=True)
        return plan, notes


def rebuild_state(series: Series) -> dict[str, Any]:
    with exclusive_lock(series.lock_path):
        recover(series)
        state = replay(series, load_episodes(series))
        atomic_write_json(series.state_path, state)
        return state
