"""GitHub Actions（.github/workflows/drive-token.yml）から、暗号化した Google ドライブ用トークンを受け取る。

流れ（Claude Code のセッションが GitHub 連携でワークフローを起動・ログ取得する）:
  1. begin  : その場限りの RSA 鍵（3072 bit）と依頼 ID を作る。秘密鍵は非公開の一時ディレクトリにだけ置く。
              ワークフローへ渡す入力（依頼 ID・公開鍵・mode）を JSON で表示する。
  2. （セッションが workflow_dispatch で起動し、実行名「drive-token <依頼ID>」の実行のログを取得してファイルに保存）
  3. finish : ログから依頼 ID の行を探し、改ざん検出（HMAC-SHA256）→ 鍵の復号（RSA-OAEP）→ 本文の復号（AES-256-CBC）。
              トークンはメモリ上にだけ置き、続けて指定した storyengine のコマンドを同じプロセスで実行する。
              成功・失敗にかかわらず、秘密鍵とログファイルを削除する。平文のトークンはファイルにもログにも書かない。

暗号文はワークフローのログに残るが、秘密鍵はこのセッションの一時ディレクトリにしかなく、すぐ消すので復号できない。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

PREFIX = "DRIVE_TOKEN_ENVELOPE"
WORKFLOW = "drive-token.yml"
KEY_TTL_SEC = 30 * 60
MAX_TOKEN_AGE_SEC = 15 * 60  # 発行から受け取りまでの許容時間


class TokenError(RuntimeError):
    pass


def _base_dir() -> Path:
    d = Path(os.environ.get("STORYENGINE_TOKEN_TMP") or tempfile.gettempdir()) / "storyengine-drive-token"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def _req_dir(request_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", request_id or ""):
        raise TokenError("依頼 ID の形式が不正です")
    return _base_dir() / request_id


def _openssl(args: list[str], data: bytes | None = None) -> bytes:
    try:
        return subprocess.run(["openssl", *args], input=data, capture_output=True, check=True).stdout
    except subprocess.CalledProcessError:
        raise TokenError("openssl の処理に失敗しました") from None


def sweep_stale(now: float | None = None) -> int:
    """期限切れの依頼（秘密鍵）を削除する。"""
    now = now or time.time()
    removed = 0
    for d in _base_dir().iterdir():
        meta = d / "meta.json"
        try:
            created = json.loads(meta.read_text())["created"]
        except Exception:  # noqa: BLE001
            created = 0
        if now - created > KEY_TTL_SEC:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    return removed


def cleanup_all() -> int:
    n = 0
    for d in _base_dir().iterdir():
        shutil.rmtree(d, ignore_errors=True)
        n += 1
    return n


def begin(mode: str) -> dict[str, str]:
    if mode not in ("dummy", "google"):
        raise TokenError("mode は dummy か google です")
    sweep_stale()
    request_id = secrets.token_hex(16)
    d = _req_dir(request_id)
    d.mkdir(mode=0o700)
    try:
        key = d / "key.pem"
        fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        _openssl(["genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:3072", "-out", str(key)])
        os.chmod(key, 0o600)
        pub = _openssl(["pkey", "-in", str(key), "-pubout"])
        (d / "meta.json").write_text(json.dumps({"mode": mode, "created": time.time()}))
    except BaseException:
        shutil.rmtree(d, ignore_errors=True)
        raise
    return {"workflow": WORKFLOW, "ref": "main", "request_id": request_id, "mode": mode,
            "public_key": base64.b64encode(pub).decode("ascii"),
            "run_name": f"drive-token {request_id}"}


def find_envelope(log_text: str, request_id: str) -> str:
    """ログからこの依頼 ID の暗号文の行を1つだけ取り出す（タイムスタンプ付きの行にも対応）。"""
    found = [m.group(1) for m in re.finditer(rf"{PREFIX} {re.escape(request_id)} ([A-Za-z0-9+/=]+)", log_text)]
    if len(found) != 1:
        errors = re.findall(r"DRIVE_TOKEN_ERROR (.+)", log_text)
        detail = f"（ワークフローのエラー: {errors[-1].strip()}）" if errors else ""
        raise TokenError(f"依頼 {request_id} の結果がログに {len(found)} 件あります（1件である必要があります）{detail}")
    return found[0]


def open_envelope(envelope_b64: str, request_id: str, key_pem: Path, *, now: float | None = None
                  ) -> tuple[str, dict[str, Any]]:
    """改ざんを確かめてから復号する。戻り値は (トークン, ヘッダー)。"""
    try:
        env = json.loads(base64.b64decode(envelope_b64, validate=True))
        header_b, wrapped, iv, ct, mac = (base64.b64decode(env[k], validate=True) for k in ("h", "k", "iv", "ct", "mac"))
    except Exception:  # noqa: BLE001
        raise TokenError("暗号文の形式が不正です") from None
    key = _openssl(["pkeyutl", "-decrypt", "-inkey", str(key_pem), "-pkeyopt", "rsa_padding_mode:oaep",
                    "-pkeyopt", "rsa_oaep_md:sha256", "-pkeyopt", "rsa_mgf1_md:sha256"], wrapped)
    if len(key) != 64:
        raise TokenError("鍵の長さが不正です")
    enc_key, mac_key = key[:32], key[32:]
    expect = hmac.new(mac_key, b"|".join([header_b, wrapped, iv, ct]), hashlib.sha256).digest()
    if not hmac.compare_digest(expect, mac):
        raise TokenError("暗号文が改ざんされています（改ざん検出の値が一致しません）")
    header = json.loads(header_b)
    if header.get("v") != 1 or header.get("request_id") != request_id:
        raise TokenError("別の依頼の結果です（依頼 ID が一致しません）")
    age = (now or time.time()) - int(header.get("issued_at", 0))
    if not -120 <= age <= MAX_TOKEN_AGE_SEC:
        raise TokenError("発行から時間が経ちすぎています。もう一度依頼してください")
    token = _openssl(["enc", "-d", "-aes-256-cbc", "-K", enc_key.hex(), "-iv", iv.hex()], ct).decode("utf-8")
    return token, header


def finish(request_id: str, log_path: Path) -> tuple[str, dict[str, Any]]:
    """ログを読んで復号する。成功・失敗にかかわらず、秘密鍵とログファイルを削除する。"""
    d = _req_dir(request_id)
    try:
        if not (d / "key.pem").is_file():
            raise TokenError("この依頼の秘密鍵がありません（期限切れ・別のセッション・削除済み）")
        meta = json.loads((d / "meta.json").read_text())
        envelope = find_envelope(Path(log_path).read_text(encoding="utf-8", errors="replace"), request_id)
        token, header = open_envelope(envelope, request_id, d / "key.pem")
        if header.get("mode") != meta.get("mode"):
            raise TokenError("依頼したモードと結果のモードが一致しません")
        return token, header
    finally:
        shutil.rmtree(d, ignore_errors=True)
        try:
            Path(log_path).unlink()
        except OSError:
            pass
