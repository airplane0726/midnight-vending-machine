"""Runway API（Gen-4 Turbo）で、開始画像から短い動画を作る。

確認した公式資料（docs.dev.runwayml.com, 2026-11-24 時点）:
- POST /v1/image_to_video（model=gen4_turbo、promptImage は https / data URI、ratio 720:1280、duration は 2〜10 の整数）
- 結果は GET /v1/tasks/{id} を待って取得（SUCCEEDED / FAILED など）。ヘッダー X-Runway-Version が必要
- 料金: 1秒 5クレジット（$0.05）、1回の最低 10クレジット。1クレジット $0.01

方針:
- 1回の見積もりが上限（max_cost_usd、既定 $0.25）を超える依頼は送らない。
- 失敗・中断しても自動で作り直さない（同じ依頼を送り直さない）。タスク ID を記録して終える。
- API キーはクラウド環境のネットワーク用シークレット（api.dev.runwayml.com）で通信経路から付与する。
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .integrations import VIDEO_GENERATORS, VideoGenerator

API_BASE = "https://api.dev.runwayml.com"
API_VERSION = "2024-11-06"
PLACEHOLDER_KEY = "injected-by-environment-proxy"
CREDITS_PER_SEC = {"gen4_turbo": 5}
MIN_CREDITS = {"gen4_turbo": 10}
USD_PER_CREDIT = 0.01
RATIOS = {"9:16": "720:1280"}


class RunwayError(RuntimeError):
    pass


def estimate_usd(model: str, seconds: int) -> float:
    if model not in CREDITS_PER_SEC:
        raise RunwayError(f"料金が確認できていないモデルです: {model}")
    return max(CREDITS_PER_SEC[model] * seconds, MIN_CREDITS[model]) * USD_PER_CREDIT


class RunwayVideoGenerator(VideoGenerator):
    provider = "runway"
    paid = True

    def __init__(self, config: dict[str, Any] | None = None, *, opener=None, sleep=time.sleep,
                 poll_interval: float = 10.0, max_wait_sec: float = 900.0) -> None:
        super().__init__(config)
        self.model = self.config.get("model") or "gen4_turbo"
        params = self.config.get("params") or {}
        self.max_cost_usd = float(params.get("max_cost_usd", 0.25))
        self._opener = opener or urllib.request.urlopen
        self._sleep, self.poll_interval, self.max_wait_sec = sleep, poll_interval, max_wait_sec
        self.last_task: dict[str, Any] | None = None

    def estimate(self, spec: dict[str, Any]) -> dict[str, Any]:
        usd = estimate_usd(self.model, int(spec["duration_sec"]))
        return {"model": self.model, "duration_sec": int(spec["duration_sec"]), "usd": usd,
                "within_cap": usd <= self.max_cost_usd + 1e-9, "max_cost_usd": self.max_cost_usd}

    # ---- 通信 ----
    def _req(self, method: str, url: str, body: dict | None = None) -> dict[str, Any]:
        key = os.environ.get("RUNWAYML_API_SECRET") or PLACEHOLDER_KEY
        headers = {"Authorization": f"Bearer {key}", "X-Runway-Version": API_VERSION}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        try:
            with self._opener(urllib.request.Request(url, data=data, method=method, headers=headers), timeout=120) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = " " + json.loads(e.read().decode()).get("error", "")[:300]
            except Exception:  # noqa: BLE001
                pass
            hint = {401: "（API キー未登録・無効）", 402: "（残高不足）", 429: "（混雑・上限）"}.get(e.code, "")
            raise RunwayError(f"Runway API エラー HTTP {e.code}{hint}{detail}") from None
        except urllib.error.URLError as e:
            raise RunwayError(f"Runway API に接続できません: {e.reason}") from None

    @staticmethod
    def _image_data_uri(path: Path) -> str:
        """送る画像を JPEG にして小さくする（data URI の大きさの上限が未確認のため）。"""
        with tempfile.TemporaryDirectory() as d:
            jpg = Path(d) / "first.jpg"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-q:v", "2", str(jpg)], check=True)
            return "data:image/jpeg;base64," + base64.b64encode(jpg.read_bytes()).decode()

    def build_request(self, spec: dict[str, Any], init_image: Path) -> dict[str, Any]:
        est = self.estimate(spec)
        if not est["within_cap"]:
            raise RunwayError(f"見積もり ${est['usd']:.2f} が1回の上限 ${self.max_cost_usd:.2f} を超えるため送りません")
        dur = int(spec["duration_sec"])
        if not 2 <= dur <= 10:
            raise RunwayError("Gen-4 Turbo の長さは 2〜10 秒です")
        return {"model": self.model, "promptText": spec["prompt"][:1000],
                "promptImage": [{"uri": self._image_data_uri(init_image), "position": "first"}],
                "ratio": RATIOS.get(spec.get("aspect_ratio", "9:16"), "720:1280"), "duration": dur}

    def generate(self, spec: dict[str, Any], init_image: Path, out_path: Path) -> Path:
        body = self.build_request(spec, init_image)
        task = self._req("POST", f"{API_BASE}/v1/image_to_video", body)
        task_id = task.get("id")
        if not task_id:
            raise RunwayError("タスク ID が返りませんでした")
        self.last_task = {"id": task_id, "status": "SUBMITTED"}
        return self._wait_and_download(task_id, out_path)

    def fetch(self, task_id: str, out_path: Path) -> Path:
        """既存のタスクの結果を取り直す（新しい生成依頼は送らない・追加費用なし）。"""
        self.last_task = {"id": task_id, "status": "REFETCH"}
        return self._wait_and_download(task_id, out_path)

    def _wait_and_download(self, task_id: str, out_path: Path) -> Path:
        waited = 0.0
        while True:  # 結果を待つだけ。失敗しても送り直さない
            info = self._req("GET", f"{API_BASE}/v1/tasks/{task_id}")
            status = info.get("status")
            self.last_task = {"id": task_id, "status": status}
            if status == "SUCCEEDED":
                urls = info.get("output") or []
                if not urls:
                    raise RunwayError("完成したのに動画の URL がありません")
                with self._opener(urllib.request.Request(urls[0]), timeout=300) as r:
                    Path(out_path).write_bytes(r.read())
                return Path(out_path)
            if status in ("FAILED", "CANCELLED"):
                raise RunwayError(f"Runway のタスクが {status} で終わりました: {str(info.get('failure') or '')[:300]}")
            if waited >= self.max_wait_sec:
                raise RunwayError(f"{int(waited)} 秒待っても完成しません（タスク {task_id} は記録済み。送り直していません）")
            self._sleep(self.poll_interval)
            waited += self.poll_interval


VIDEO_GENERATORS["runway"] = RunwayVideoGenerator
