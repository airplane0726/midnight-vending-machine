"""Google ドライブへの生成素材の永続保存（Drive API v3、標準ライブラリのみ）。

認証の仕組み（認証情報をコード・Git・チャットに置かない）:
- 方式A（クラウド環境の既定）: OAuth Playground で発行したアクセストークン（約1時間有効）を、
  www.googleapis.com 宛てのネットワーク用シークレット（Bearer）として保存する。通信経路が Authorization を付与する。
  リフレッシュトークンはクラウド環境に置かない（本文でしか送れず、安全に保存する場所がないため）。
  有効期限があるので、保存確認などの手動作業向け。毎日の自動運用には使えない。
- 方式B（リフレッシュトークンを安全に置ける実行環境向け）:
- OAuth クライアントの ID とシークレット: クラウド環境のネットワーク用シークレット（oauth2.googleapis.com 宛て、
  Basic 認証）として保存し、通信経路で Authorization ヘッダーに付与される。コードは置き換え用の仮の値を送る。
- リフレッシュトークン: Google のトークン発行 API は本文でしか受け取らないため、環境変数
  GOOGLE_DRIVE_REFRESH_TOKEN（クラウド環境の設定画面で利用者が入力）から読む。
- スコープは drive.file（Google の分類で非機密）。このアプリが作ったファイルとフォルダーだけを扱える。
- アクセストークン（約1時間有効）はメモリ上にだけ持ち、ファイルにもログにも出さない。

保存の確認:
- 保存前に Drive の空き容量（about.storageQuota）を確認し、不足していれば保存しない。
- アップロード後に同じファイルをダウンロードし、SHA-256 が一致したときだけ「保存済み」とする。
"""
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .integrations import ASSET_STORES, AssetStore

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/drive/v3"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
FOLDER_MIME = "application/vnd.google-apps.folder"
REFRESH_ENV = "GOOGLE_DRIVE_REFRESH_TOKEN"
PLACEHOLDER_BEARER = "injected-by-environment-proxy"
PLACEHOLDER_BASIC = base64.b64encode(b"injected-by-environment-proxy:x").decode()
TIMEOUT_SEC = 300


class DriveError(RuntimeError):
    pass


class GoogleDriveStore(AssetStore):
    provider = "google-drive"

    def __init__(self, config: dict[str, Any] | None = None, *, opener=None, clock=time.monotonic) -> None:
        super().__init__(config)
        params = self.config.get("params") or {}
        self.folder_name = params.get("folder_name") or "midnight-vending-machine-media"
        self.min_free_bytes = int(float(params.get("min_free_mb", 500)) * 1024 * 1024)
        self._opener = opener or urllib.request.urlopen
        self._clock = clock
        self._token: str | None = None
        self._token_until = 0.0
        self._folder_id: str | None = None

    # ---------------------------------------------------------------- 通信
    def _send(self, req: urllib.request.Request):
        try:
            return self._opener(req, timeout=TIMEOUT_SEC)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                body = json.loads(e.read().decode("utf-8"))
                err = body.get("error")
                detail = f" {err.get('message', '')}" if isinstance(err, dict) else f" {err}: {body.get('error_description', '')}"
            except Exception:  # noqa: BLE001
                pass
            hint = {400: "（リフレッシュトークンが無効・期限切れの可能性）",
                    401: "（アクセストークンが未登録・期限切れ（約1時間）の可能性。Playground で取り直して登録してください）",
                    403: "（権限・容量・API の有効化を確認してください）", 404: "（ファイルが見つかりません）"}.get(e.code, "")
            raise DriveError(f"Google ドライブ エラー HTTP {e.code}{hint}{detail}"[:600]) from None
        except urllib.error.URLError as e:
            raise DriveError(f"Google に接続できません: {e.reason}") from None

    def _access_token(self) -> str:
        if self._token and self._clock() < self._token_until:
            return self._token
        refresh = os.environ.get(REFRESH_ENV)
        if not refresh:
            # クラウド環境: アクセストークン（約1時間有効）を www.googleapis.com 宛てのネットワーク用シークレットに
            # 保存しておくと、通信経路が Authorization ヘッダーを置き換える。ここでは仮の値を送るだけ。
            return PLACEHOLDER_BEARER
        form = {"grant_type": "refresh_token", "refresh_token": refresh}
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        cid, csec = os.environ.get("GOOGLE_OAUTH_CLIENT_ID"), os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
        if cid and csec:  # 手元のパソコンで使う場合
            form.update(client_id=cid, client_secret=csec)
        else:  # クラウド環境: 通信経路が Basic 認証に置き換える
            headers["Authorization"] = f"Basic {PLACEHOLDER_BASIC}"
        req = urllib.request.Request(TOKEN_URL, data=urllib.parse.urlencode(form).encode(), method="POST", headers=headers)
        with self._send(req) as resp:
            tok = json.loads(resp.read().decode("utf-8"))
        if not tok.get("access_token"):
            raise DriveError("アクセストークンを取得できませんでした")
        self._token = tok["access_token"]
        self._token_until = self._clock() + max(int(tok.get("expires_in", 3600)) - 120, 60)
        return self._token

    def _call(self, method: str, url: str, *, data: bytes | None = None, headers: dict[str, str] | None = None):
        h = {"Authorization": f"Bearer {self._access_token()}", **(headers or {})}
        return self._send(urllib.request.Request(url, data=data, method=method, headers=h))

    def _json(self, method: str, url: str, payload: Any = None) -> dict[str, Any]:
        data = json.dumps(payload).encode() if payload is not None else None
        with self._call(method, url, data=data, headers={"Content-Type": "application/json"} if data else None) as r:
            return json.loads(r.read().decode("utf-8") or "{}")

    # ---------------------------------------------------------------- 容量
    def quota(self) -> dict[str, int | None]:
        q = self._json("GET", f"{API}/about?fields=storageQuota")["storageQuota"]
        limit = int(q["limit"]) if q.get("limit") else None
        usage = int(q.get("usage", 0))
        return {"limit": limit, "usage": usage, "free": (limit - usage) if limit is not None else None}

    def check_capacity(self, required_bytes: int) -> tuple[bool, str]:
        q = self.quota()
        if q["free"] is None:
            return True, "容量の上限なし"
        need = required_bytes + self.min_free_bytes
        gb = q["free"] / 1024 ** 3
        if q["free"] < need:
            return False, (f"Google ドライブの空き容量が不足しています（空き {gb:.2f} GB、"
                           f"必要 {need / 1024 ** 2:.0f} MB＝保存分＋予備 {self.min_free_bytes / 1024 ** 2:.0f} MB）")
        return True, f"空き {gb:.2f} GB"

    # ---------------------------------------------------------------- フォルダーとファイル
    def _q(self, q: str, fields: str = "files(id,name,appProperties)") -> list[dict[str, Any]]:
        url = f"{API}/files?" + urllib.parse.urlencode({"q": q, "fields": fields, "spaces": "drive", "pageSize": 100})
        return self._json("GET", url).get("files", [])

    @staticmethod
    def _lit(s: str) -> str:
        return s.replace("\\", "\\\\").replace("'", "\\'")

    def folder_id(self) -> str:
        if self._folder_id:
            return self._folder_id
        found = self._q(f"name = '{self._lit(self.folder_name)}' and mimeType = '{FOLDER_MIME}' and trashed = false",
                        "files(id)")
        if found:
            self._folder_id = found[0]["id"]
        else:
            self._folder_id = self._json("POST", f"{API}/files?fields=id",
                                         {"name": self.folder_name, "mimeType": FOLDER_MIME})["id"]
        return self._folder_id

    def _fetch(self, file_id: str) -> bytes:
        with self._call("GET", f"{API}/files/{urllib.parse.quote(file_id)}?alt=media") as r:
            return r.read()

    def upload(self, local_path: Path, object_key: str) -> str:
        data = Path(local_path).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        ok, msg = self.check_capacity(len(data))
        if not ok:
            raise DriveError(msg)
        name = object_key.replace("/", "__")
        folder = self.folder_id()
        # 前回の途中終了などで同じ内容が既にあれば、それを確認して使う（重複させない）
        file_id = None
        for f in self._q(f"'{folder}' in parents and name = '{self._lit(name)}' and trashed = false"):
            if (f.get("appProperties") or {}).get("sha256") == digest:
                file_id = f["id"]
                break
        if file_id is None:
            ctype = mimetypes.guess_type(str(local_path))[0] or "application/octet-stream"
            meta = {"name": name, "parents": [folder], "appProperties": {"sha256": digest}}
            with self._call("POST", f"{UPLOAD_API}/files?uploadType=resumable&fields=id",
                            data=json.dumps(meta).encode(),
                            headers={"Content-Type": "application/json; charset=UTF-8",
                                     "X-Upload-Content-Type": ctype,
                                     "X-Upload-Content-Length": str(len(data))}) as r:
                session = r.headers.get("Location")
            if not session or not session.startswith(UPLOAD_API):
                raise DriveError("アップロード先の URL を取得できませんでした")
            with self._call("PUT", session, data=data, headers={"Content-Type": ctype}) as r:
                file_id = json.loads(r.read().decode("utf-8"))["id"]
        # 復元の確認: ダウンロードして中身を照合
        if hashlib.sha256(self._fetch(file_id)).hexdigest() != digest:
            raise DriveError("保存後にダウンロードした内容が元のファイルと一致しません")
        return f"gdrive://{file_id}"

    def download(self, uri: str, dest: Path) -> Path:
        if not uri.startswith("gdrive://"):
            raise DriveError(f"Google ドライブの URI ではありません: {uri}")
        Path(dest).write_bytes(self._fetch(uri[len("gdrive://"):]))
        return Path(dest)


ASSET_STORES["google-drive"] = GoogleDriveStore
