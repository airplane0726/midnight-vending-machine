"""画像・動画・音声の生成と、SNS 投稿・素材の永続保存の接続口。

登録済みの実装: 画像生成 openai（storyengine/openai_images.py）。永続保存 google-drive（storyengine/google_drive.py）。各サービスと契約・費用を確認したあと、
このクラスを継承して実装し、IMAGE_GENERATORS / ASSET_STORES に登録する。
使うサービスは series.json の generation.image.provider / asset_storage.provider で選ぶ。

- 入力は production.json と同じ内容（storyengine.media が episode.json と series.json から組み立てる）。
- 素材のパスはすべて「連載ディレクトリ（series/<id>/）からの相対パス」。
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
    """カットの開始画像を生成する。

    実装するときの約束:
    - generate() は spec に従って画像を 1 枚作り、out_path に書き込む（PNG / JPEG / WebP）。
      拡張子は呼び出し側が判定するので、out_path の名前は変えない。
    - spec は production.json の cuts[].image に次を加えたもの:
        references[].abs_path … 参照画像の絶対パス（未登録なら references は空）
        seed               … supports_seed が True のときだけ入る（生成条件から決まる固定値）
    - 失敗したら例外を投げる。不完全なファイルが残っても完成品としては登録されない。
    """

    provider: str = ""
    supports_seed: bool = False
    #: True の生成器（ダミー・プレースホルダー）の出力は完成素材として登録しない
    placeholder: bool = False
    #: True の生成器は費用がかかる。CLI では --allow-paid を付けたときだけ実行する
    paid: bool = False
    #: 直近の生成で使ったトークン数など（サービスが返す場合）。生成記録に残す
    last_usage: dict[str, Any] | None = None
    last_request: dict[str, Any] | None = None

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}

    @abstractmethod
    def generate(self, spec: dict[str, Any], out_path: Path) -> Path: ...

    def estimate(self, spec: dict[str, Any]) -> dict[str, Any] | None:
        """生成前の費用見積もり（わかる範囲）。わからなければ None。"""
        return None

    def cost_from_usage(self, usage: dict[str, Any] | None) -> float | None:
        """応答の使用量から実際の費用（USD）を計算する。わからなければ None。"""
        return None


class VideoGenerator(ABC):
    """開始画像と動きの指示から、カットの動画を生成する。cuts[].video を受け取る。

    init_image_slot の画像は、生成記録（media/epNNN/records.json）で完成済みのものを使う。
    """

    provider: str = ""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}

    #: True の生成器は費用がかかる。CLI では --allow-paid を付けたときだけ実行する
    paid: bool = False

    @abstractmethod
    def generate(self, spec: dict[str, Any], init_image: Path, out_path: Path) -> Path: ...

    def estimate(self, spec: dict[str, Any]) -> dict[str, Any] | None:
        return None


class AudioProducer(ABC):
    """音楽・効果音を用意し、20秒の音声トラックにまとめる。audio を受け取る。"""

    @abstractmethod
    def produce(self, audio: dict[str, Any], out_path: Path) -> Path: ...


class Publisher(ABC):
    """完成動画を投稿する。publish.instagram / publish.youtube を受け取る。"""

    @abstractmethod
    def publish(self, video_path: Path, post: dict[str, Any], *, dry_run: bool = True) -> dict[str, Any]: ...


class AssetStore(ABC):
    """生成素材の永続保存先（クラウドのストレージなど）。

    クラウド環境は終了するとローカルのファイルが消えるため、生成素材はここへ保存して初めて「保存完了」とする。
    """

    provider: str = ""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}

    @abstractmethod
    def upload(self, local_path: Path, object_key: str) -> str:
        """ファイルを保存し、取り出し用の URI を返す。"""

    @abstractmethod
    def download(self, uri: str, dest: Path) -> Path:
        """upload() が返した URI のファイルを dest に書き出す。"""

    def check_capacity(self, required_bytes: int) -> tuple[bool, str]:
        """保存先の空き容量が足りるか。(足りるか, 説明)。確認できない保存先は常に True。"""
        return True, "容量確認なし"


# 実サービスの実装を登録する（例: IMAGE_GENERATORS["some-service"] = SomeServiceImageGenerator）
IMAGE_GENERATORS: dict[str, type[ImageGenerator]] = {}
ASSET_STORES: dict[str, type[AssetStore]] = {}
VIDEO_GENERATORS: dict[str, type[VideoGenerator]] = {}


def _load_builtin_providers() -> None:
    from . import google_drive, openai_images, runway_video  # noqa: F401  （openai / google-drive / runway を登録）


def create_image_generator(cfg: dict[str, Any] | None) -> ImageGenerator | None:
    """series.json の generation.image から生成器を作る。provider 未設定なら None（未接続）。"""
    provider = (cfg or {}).get("provider")
    if not provider:
        return None
    _load_builtin_providers()
    if provider not in IMAGE_GENERATORS:
        raise NotConfiguredError(f"画像生成サービス {provider!r} の接続が実装されていません")
    return IMAGE_GENERATORS[provider](cfg)


def create_video_generator(cfg: dict[str, Any] | None) -> VideoGenerator | None:
    """series.json の generation.video から動画生成器を作る。provider 未設定なら None（未接続）。"""
    provider = (cfg or {}).get("provider")
    if not provider:
        return None
    _load_builtin_providers()
    if provider not in VIDEO_GENERATORS:
        raise NotConfiguredError(f"動画生成サービス {provider!r} の接続が実装されていません")
    return VIDEO_GENERATORS[provider](cfg)


def create_asset_store(cfg: dict[str, Any] | None) -> AssetStore | None:
    """series.json の asset_storage から保存先を作る。provider 未設定なら None（永続保存なし）。"""
    provider = (cfg or {}).get("provider")
    if not provider:
        return None
    _load_builtin_providers()
    if provider not in ASSET_STORES:
        raise NotConfiguredError(f"永続保存先 {provider!r} の接続が実装されていません")
    return ASSET_STORES[provider](cfg)
