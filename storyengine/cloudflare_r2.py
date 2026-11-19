"""Cloudflare R2 への生成素材の永続保存（Cloudflare REST API を使用）。

確認した公式資料（developers.cloudflare.com, 2026-11-19 時点）:
- アップロード: PUT /accounts/{account_id}/r2/buckets/{bucket_name}/objects/{object_key}（本文がオブジェクト、最大 300 MB）
- ダウンロード: GET 同じパス
- 認証: API トークン（Authorization: Bearer）。権限は「Workers R2 Storage」の Write（アカウント単位）
- オブジェクトキーのスラッシュはそのまま送る（%2F にしない）

S3 互換 API ではなく REST API を使うのは、署名に秘密鍵が必要な S3 方式と違い、トークンを
クラウド環境のネットワーク用シークレットとして通信経路で付与できるため（コードやファイルに秘密を置かない）。
アップロード本文の形式（そのままのバイト列か multipart か）は資料の例が食い違っているため、
アップロード後にダウンロードして SHA-256 を照合し、一致したときだけ「保存済み」とする。
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .integrations import ASSET_STORES, AssetStore

API_BASE = "https://api.cloudflare.com/client/v4"
PLACEHOLDER_TOKEN = "injected-by-environment-proxy"
TIMEOUT_SEC = 300


class R2Error(RuntimeError):
    pass


class CloudflareR2Store(AssetStore):
    provider = "cloudflare-r2"

    def __init__(self, config: dict[str, Any] | None = None, *, opener=None) -> None:
        super().__init__(config)
        params = {**(self.config.get("params") or {}), **{k: v for k, v in self.config.items() if k in ("account_id", "bucket")}}
        self.account_id = params.get("account_id")
        self.bucket = params.get("bucket")
        if not self.account_id or not self.bucket:
            raise R2Error("series.json の asset_storage に account_id と bucket を設定してください")
        self._opener = opener or urllib.request.urlopen

    def _url(self, key: str) -> str:
        return (f"{API_BASE}/accounts/{urllib.parse.quote(self.account_id, safe='')}/r2/buckets/"
                f"{urllib.parse.quote(self.bucket, safe='')}/objects/{urllib.parse.quote(key, safe='/')}")

    def _request(self, method: str, url: str, data: bytes | None = None, ctype: str | None = None):
        token = os.environ.get("CLOUDFLARE_API_TOKEN") or PLACEHOLDER_TOKEN
        headers = {"Authorization": f"Bearer {token}"}
        if ctype:
            headers["Content-Type"] = ctype
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            return self._opener(req, timeout=TIMEOUT_SEC)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                errs = json.loads(e.read().decode("utf-8")).get("errors") or []
                detail = " " + "; ".join(f"{x.get('code')}: {x.get('message')}" for x in errs)
            except Exception:  # noqa: BLE001
                pass
            hint = {401: "（トークンが未登録か無効です）", 403: "（トークンの権限を確認してください）",
                    404: "（アカウントID・バケット名を確認してください）"}.get(e.code, "")
            raise R2Error(f"Cloudflare R2 エラー HTTP {e.code}{hint}{detail}"[:600]) from None
        except urllib.error.URLError as e:
            raise R2Error(f"Cloudflare API に接続できません: {e.reason}") from None

    def upload(self, local_path: Path, object_key: str) -> str:
        data = Path(local_path).read_bytes()
        ctype = mimetypes.guess_type(str(local_path))[0] or "application/octet-stream"
        with self._request("PUT", self._url(object_key), data, ctype) as resp:
            body = resp.read()
        try:
            if body and json.loads(body.decode("utf-8")).get("success") is False:
                raise R2Error(f"アップロードに失敗しました: {body[:300]!r}")
        except ValueError:
            pass
        # 保存された中身が元と同じか、取り出して確かめる
        with self._request("GET", self._url(object_key)) as resp:
            stored = resp.read()
        if hashlib.sha256(stored).digest() != hashlib.sha256(data).digest():
            raise R2Error("アップロード後に取り出した内容が元のファイルと一致しません")
        return f"r2://{self.bucket}/{object_key}"

    def download(self, uri: str, dest: Path) -> Path:
        prefix = f"r2://{self.bucket}/"
        if not uri.startswith(prefix):
            raise R2Error(f"このバケットの URI ではありません: {uri}")
        with self._request("GET", self._url(uri[len(prefix):])) as resp:
            Path(dest).write_bytes(resp.read())
        return Path(dest)


ASSET_STORES["cloudflare-r2"] = CloudflareR2Store
