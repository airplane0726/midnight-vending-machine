"""画像・動画・音声の生成と、SNS 投稿の接続口（インターフェースのみ）。

今回の段階では外部サービスに接続しない。各サービスと契約・費用を確認したあと、
このクラスを継承して実装を追加する。

- 入力は episodes/epNNN/production.json（カットごとの合成済みプロンプト、効果音タイムライン、投稿文）。
- 認証情報は必ず環境変数から読む（コードやリポジトリに書かない）。必要な変数名は .env.example を参照。
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class NotConfiguredError(RuntimeError):
    pass


def require_env(*names: str) -> dict[str, str]:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise NotConfiguredError("環境変数が設定されていません: " + ", ".join(missing))
    return {n: os.environ[n] for n in names}


class ImageGenerator(ABC):
    """カットの開始画像を生成する。production.json の cuts[].image を受け取る。"""

    @abstractmethod
    def generate(self, spec: dict[str, Any], out_path: Path) -> Path: ...


class VideoGenerator(ABC):
    """開始画像と動きの指示から、カットの動画を生成する。cuts[].video を受け取る。"""

    @abstractmethod
    def generate(self, spec: dict[str, Any], init_image: Path, out_path: Path) -> Path: ...


class AudioProducer(ABC):
    """音楽・効果音を用意し、20秒の音声トラックにまとめる。audio を受け取る。"""

    @abstractmethod
    def produce(self, audio: dict[str, Any], out_path: Path) -> Path: ...


class Publisher(ABC):
    """完成動画を投稿する。publish.instagram / publish.youtube を受け取る。"""

    @abstractmethod
    def publish(self, video_path: Path, post: dict[str, Any], *, dry_run: bool = True) -> dict[str, Any]: ...
