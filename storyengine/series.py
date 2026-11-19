"""連載ディレクトリの読み込み。

連載ごとの設定はすべて `series/<id>/series.json` に置き、エンジン側には作品固有の値を持たない。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .storage import read_json


@dataclass(frozen=True)
class Series:
    root: Path
    config: dict[str, Any]

    @property
    def id(self) -> str:
        return self.config["series_id"]

    @property
    def format(self) -> dict[str, Any]:
        return self.config["format"]

    @property
    def characters(self) -> dict[str, Any]:
        return self.config.get("characters", {})

    @property
    def locations(self) -> dict[str, Any]:
        return self.config.get("locations", {})

    @property
    def episodes_dir(self) -> Path:
        return self.root / "episodes"

    @property
    def drafts_dir(self) -> Path:
        return self.root / "drafts"

    @property
    def state_path(self) -> Path:
        return self.root / "state.json"

    @property
    def lock_path(self) -> Path:
        return self.root / ".lock"

    def episode_dir(self, number: int) -> Path:
        return self.episodes_dir / f"ep{number:03d}"

    def draft_path(self, number: int) -> Path:
        return self.drafts_dir / f"ep{number:03d}.json"

    # ---- 素材の保存場所 ----
    # production.json・生成記録・series.json に書く素材のパスは、すべて
    # 「連載ディレクトリ（series/<id>/）からの相対パス」で統一する。

    @property
    def references_dir(self) -> Path:
        """人物・場所の基準画像（小さなファイル。Git 管理する）。"""
        return self.root / "references"

    @property
    def media_dir(self) -> Path:
        """生成素材と生成記録。files は Git 管理外、records.json は Git 管理。"""
        return self.root / "media"

    def media_rel(self, number: int, kind: str) -> str:
        return f"media/ep{number:03d}/{kind}"

    def media_records_path(self, number: int) -> Path:
        return self.media_dir / f"ep{number:03d}" / "records.json"

    def resolve(self, rel: str) -> Path:
        """連載ディレクトリからの相対パスを絶対パスにする（外へ出るパスは拒否）。"""
        return resolve_inside(self.root, rel)

    def rel(self, path: Path) -> str:
        return Path(path).resolve().relative_to(self.root).as_posix()


def resolve_inside(root: Path, rel: str) -> Path:
    if not isinstance(rel, str) or not rel or Path(rel).is_absolute():
        raise ValueError(f"連載ディレクトリからの相対パスを指定してください: {rel!r}")
    p = (root / rel).resolve()
    if not p.is_relative_to(root):
        raise ValueError(f"連載ディレクトリの外を指すパスは使えません: {rel!r}")
    return p


REQUIRED_CONFIG_KEYS = ("series_id", "title", "format", "characters", "initial_state")


def load_series(path: str | Path) -> Series:
    root = Path(path).resolve()
    cfg_path = root / "series.json"
    if not cfg_path.is_file():
        raise FileNotFoundError(f"series.json が見つかりません: {cfg_path}")
    config = read_json(cfg_path)
    missing = [k for k in REQUIRED_CONFIG_KEYS if k not in config]
    if missing:
        raise ValueError(f"series.json に必須キーがありません: {', '.join(missing)}")
    fmt = config["format"]
    if sum(fmt["cut_durations"]) != fmt["total_seconds"]:
        raise ValueError("series.json の cut_durations の合計が total_seconds と一致しません")
    for group in ("characters", "locations"):
        for sid, item in (config.get(group) or {}).items():
            for r in item.get("reference_images", []) or []:
                path = r.get("path") if isinstance(r, dict) else None
                resolve_inside(root, path)
                if not path.startswith("references/"):
                    raise ValueError(f"{group}.{sid}.reference_images の path は references/ の下にしてください: {path!r}")
    return Series(root=root, config=config)
