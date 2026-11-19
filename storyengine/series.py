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
    return Series(root=root, config=config)
