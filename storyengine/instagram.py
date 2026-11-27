"""Instagram（リール）への投稿。承認済みの投稿版だけを、関門を通してから送る。標準ライブラリのみ。

公式資料（2026-11-27 時点の検索で確認。接続時に再確認すること）:
- Instagram ログインの API（graph.instagram.com）。権限 instagram_business_basic・instagram_business_content_publish。
  自分のプロアカウントだけなら Standard Access（アプリ審査なし）。
- 流れ: POST /{ig-user-id}/media（media_type=REELS、video_url＝公開 URL）→ コンテナの status_code を
  1分ごと・最大5分ほど確認（FINISHED／IN_PROGRESS／ERROR／EXPIRED）→ POST /{ig-user-id}/media_publish（creation_id）。
  コンテナは24時間で期限切れ。24時間あたりの投稿数に上限がある（content_publishing_limit で確認）。
- 動画を直接送る再開可能アップロード（rupload.facebook.com）は、Facebook ログイン（Facebook ページの連携）だけ。
  そのため既定では、Google ドライブの投稿版に一時的な公開リンクを付けて video_url に渡し、取り込み後に外す。
- 長期トークンは60日有効。発行から24時間以上たてば refresh_access_token で延長できる。

方針:
- 承認台帳（publish/ledger.json）で、承認済み・ファイルの sha256 一致・未投稿を確かめてから送る（publish.assert_can_post）。
- 失敗しても自動で作り直さない。コンテナが ERROR／EXPIRED なら止めて報告する。
- 段階（mode）: check＝確認だけ（送信なし）／container＝コンテナを作って取り込みまで（公開しない）／publish＝公開まで。
- 結果は "INSTAGRAM_RESULT <依頼ID> <JSON>" の1行で出す（トークンは出さない）。記録（publish record）は利用者側で行う。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from . import publish
from .media import load_records
from .series import Series

GRAPH = "https://graph.instagram.com"
API_VERSION = "v25.0"          # 接続時に公式資料の最新版を確認する
CAPTION_MAX = 2200
HASHTAGS_MAX = 30
MODES = ("check", "container", "publish")
RESULT_PREFIX = "INSTAGRAM_RESULT"


class InstagramError(RuntimeError):
    pass


class InstagramClient:
    def __init__(self, token: str, user_id: str = "me", *, opener=None, version: str = API_VERSION,
                 base: str = GRAPH) -> None:
        if not token:
            raise InstagramError("Instagram のアクセストークンがありません")
        self._token, self.user_id = token, user_id
        self._opener = opener or urllib.request.urlopen
        self.base = f"{base}/{version}"
        self.calls: list[tuple[str, str]] = []   # 送った依頼（方法・パス）の記録（トークンは含めない）

    def _req(self, method: str, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        self.calls.append((method, path))
        url = f"{self.base}/{path}"
        data = None
        headers = {"Authorization": f"Bearer {self._token}"}
        if method == "GET":
            if params:
                url += "?" + urllib.parse.urlencode(params)
        else:
            data = urllib.parse.urlencode(params).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            with self._opener(urllib.request.Request(url, data=data, method=method, headers=headers), timeout=120) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                err = json.loads(e.read().decode()).get("error", {})
                detail = f" {err.get('type', '')} code={err.get('code')} sub={err.get('error_subcode')}: {err.get('message', '')}"
            except Exception:  # noqa: BLE001
                pass
            hint = {190: "（トークンが無効・期限切れ）"}.get(e.code, "")
            raise InstagramError(f"Instagram API エラー HTTP {e.code}{hint}{detail}"[:500]) from None
        except urllib.error.URLError as e:
            raise InstagramError(f"Instagram API に接続できません: {e.reason}") from None

    def me(self) -> dict[str, Any]:
        return self._req("GET", "me", {"fields": "user_id,username,account_type"})

    def publishing_limit(self) -> dict[str, Any]:
        d = self._req("GET", f"{self.user_id}/content_publishing_limit", {"fields": "quota_usage,config"})
        return (d.get("data") or [{}])[0]

    def create_reel(self, video_url: str, caption: str, *, cover_url: str | None = None,
                    thumb_offset_ms: int | None = None, share_to_feed: bool = True) -> str:
        d = self._req("POST", f"{self.user_id}/media", {
            "media_type": "REELS", "video_url": video_url, "caption": caption, "cover_url": cover_url,
            "thumb_offset": thumb_offset_ms, "share_to_feed": "true" if share_to_feed else "false"})
        if not d.get("id"):
            raise InstagramError("コンテナ ID が返りませんでした")
        return d["id"]

    def container_status(self, container_id: str) -> dict[str, Any]:
        return self._req("GET", container_id, {"fields": "status_code,status"})

    def publish(self, container_id: str) -> str:
        d = self._req("POST", f"{self.user_id}/media_publish", {"creation_id": container_id})
        if not d.get("id"):
            raise InstagramError("公開した投稿の ID が返りませんでした")
        return d["id"]

    def permalink(self, media_id: str) -> str | None:
        return self._req("GET", media_id, {"fields": "permalink"}).get("permalink")


def wait_finished(client: InstagramClient, container_id: str, *, interval: float = 60, max_wait: float = 600,
                  sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """取り込みが終わるのを待つ（公式の目安: 1分ごと）。ERROR／EXPIRED は作り直さずに止める。"""
    waited = 0.0
    while True:
        st = client.container_status(container_id)
        code = st.get("status_code")
        if code in ("FINISHED", "PUBLISHED"):
            return st
        if code in ("ERROR", "EXPIRED"):
            raise InstagramError(f"コンテナが {code} になりました（作り直していません）: {str(st.get('status', ''))[:200]}")
        if waited >= max_wait:
            raise InstagramError(f"{int(waited)} 秒待っても取り込みが終わりません（コンテナ {container_id}。作り直していません）")
        sleep(interval)
        waited += interval


def caption_for(series: Series, n: int) -> str:
    text = (publish.package_dir(series, n) / "instagram.md").read_text(encoding="utf-8").strip()
    if len(text) > CAPTION_MAX:
        raise InstagramError(f"キャプションが {len(text)} 文字で、上限 {CAPTION_MAX} を超えています")
    tags = [w for w in text.split() if w.startswith("#")]
    if len(tags) > HASHTAGS_MAX:
        raise InstagramError(f"ハッシュタグが {len(tags)} 個で、上限 {HASHTAGS_MAX} を超えています")
    return text


def _storage_uri(series: Series, n: int, rel: str) -> str:
    for slot in load_records(series, n)["slots"].values():
        for v in slot["versions"]:
            if v.get("file") == rel and (v.get("storage") or {}).get("status") == "persisted":
                return v["storage"]["uri"]
    raise InstagramError(f"{rel} は Google ドライブに保存されていません（persist が先）")


def plan(series: Series, n: int, expected_sha: str) -> dict[str, Any]:
    """送る前の確認（通信なし）: 承認・ファイル・未投稿・キャプション・保存先。"""
    man = publish.assert_can_post(series, n, "instagram")
    post = man["video"]["post"]
    if not expected_sha or not post["sha256"].startswith(expected_sha) or len(expected_sha) < 16:
        raise InstagramError("指定された sha256（先頭16文字以上）が承認済みの投稿版と一致しません")
    cover = next((c for c in man.get("covers", []) if c["name"] == man.get("cover_choice")), None)
    return {"episode": n, "video_file": post["file"], "video_sha256": post["sha256"],
            "video_uri": _storage_uri(series, n, post["file"]),
            "cover_file": cover["file"] if cover else None,
            "cover_uri": _storage_uri(series, n, cover["file"]) if cover else None,
            "caption": caption_for(series, n), "package_sha256": man["package_sha256"]}


def run(series: Series, n: int, *, mode: str, expected_sha: str, client: InstagramClient, store,
        request_id: str = "", log: Callable[[str], None] = print, sleep: Callable[[float], None] = time.sleep,
        poll_interval: float = 60, max_wait: float = 600) -> dict[str, Any]:
    if mode not in MODES:
        raise InstagramError(f"mode は {', '.join(MODES)} のどれか")
    # 保存先（Google ドライブ）の投稿版を取り寄せ、承認した中身と同じかを確かめる。手元に無ければ（Actions）置く
    man = publish.read_json(publish.package_dir(series, n) / "manifest.json")
    rel = man["video"]["post"]["file"]
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d) / "post.mp4"
        store.download(_storage_uri(series, n, rel), tmp)
        got = publish.file_sha256(tmp)
        if got != man["video"]["post"]["sha256"]:
            raise InstagramError("保存先の動画が承認した投稿版と一致しません（送っていません）")
        local = series.resolve(rel)
        if not local.exists():
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(tmp.read_bytes())
    pl = plan(series, n, expected_sha)
    log(f"確認: 第{n}話 投稿版 {pl['video_sha256'][:12]} / キャプション {len(pl['caption'])} 文字 / 表紙 {pl['cover_file'] or 'なし'}")
    me = client.me()
    limit = client.publishing_limit()
    log(f"アカウント: @{me.get('username')}（{me.get('account_type')}） / 24時間の投稿数: {limit.get('quota_usage')} / {(limit.get('config') or {}).get('quota_total')}")
    result: dict[str, Any] = {"episode": n, "mode": mode, "video_sha256": pl["video_sha256"],
                              "username": me.get("username")}
    if mode == "check":
        result["status"] = "checked"
        _emit(request_id, result, log)
        return result
    shared: list[tuple[str, str]] = []
    try:
        perm, video_url = store.share_public(pl["video_uri"])
        shared.append((pl["video_uri"], perm))
        cover_url = None
        if pl["cover_uri"]:
            cperm, cover_url = store.share_public(pl["cover_uri"])
            shared.append((pl["cover_uri"], cperm))
        cid = client.create_reel(video_url, pl["caption"], cover_url=cover_url)
        result["container_id"] = cid
        log(f"コンテナを作りました: {cid}（取り込みを待ちます）")
        wait_finished(client, cid, interval=poll_interval, max_wait=max_wait, sleep=sleep)
        if mode == "container":
            result["status"] = "container_ready"
            log("取り込み完了（公開はしていません。コンテナは24時間で期限切れ）")
            _emit(request_id, result, log)
            return result
        media_id = client.publish(cid)
        result.update(status="published", media_id=media_id, permalink=client.permalink(media_id))
        log(f"公開しました: {result['permalink']}")
        _emit(request_id, result, log)
        return result
    finally:
        for uri, perm in shared:   # 公開リンクは必ず外す
            try:
                store.unshare(uri, perm)
            except Exception as e:  # noqa: BLE001
                log(f"[注意] 公開リンクを外せませんでした（手動で外してください）: {uri} {e}")


def _emit(request_id: str, result: dict[str, Any], log: Callable[[str], None]) -> None:
    log(f"{RESULT_PREFIX} {request_id or '-'} {json.dumps(result, ensure_ascii=False, sort_keys=True)}")


def parse_result(log_text: str, request_id: str) -> dict[str, Any]:
    for line in log_text.splitlines():
        i = line.find(RESULT_PREFIX + " ")
        if i == -1:
            continue
        _, rid, payload = line[i:].split(" ", 2)
        if rid == request_id:
            return json.loads(payload)
    raise InstagramError(f"依頼 {request_id} の結果がログにありません")


def record_from_log(series: Series, n: int, log_path: Path, request_id: str) -> dict[str, Any]:
    """Actions の結果行から、公開した投稿を承認台帳に記録する（publish だけ。二重記録は拒否）。"""
    res = parse_result(Path(log_path).read_text(encoding="utf-8", errors="replace"), request_id)
    if res.get("episode") != n or res.get("status") != "published":
        raise InstagramError(f"記録できる公開結果ではありません: {res.get('status')}")
    man = publish.assert_can_post(series, n, "instagram")
    if res.get("video_sha256") != man["video"]["post"]["sha256"]:
        raise InstagramError("公開した動画と承認済みの投稿版が一致しません")
    return publish.record_post(series, n, "instagram", url=res.get("permalink"), post_id=res.get("media_id"))


def from_env(opener=None) -> InstagramClient:
    return InstagramClient(os.environ.get("INSTAGRAM_ACCESS_TOKEN", ""), os.environ.get("INSTAGRAM_USER_ID") or "me",
                           opener=opener)
