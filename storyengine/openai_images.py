"""OpenAI Images API（GPT Image モデル）の画像生成器。

確認した公式資料（2026-11-19 時点、developers.openai.com）:
- モデル: gpt-image-2.6-sunburst / gpt-image-2.6-flare（どちらも v1/images/generations と v1/images/edits に対応）
- 参照画像: v1/images/edits の images に最大16枚（image_url は URL または base64 の data URL、最大 20,971,520 文字）
- サイズ: 推奨 1024x1024 / 1536x1024 / 1024x1536。Sunburst / Flare は WIDTHxHEIGHT の任意サイズ
  （各辺16の倍数、縦横比 1:3〜3:1、長辺3840px以下、総画素 655,360〜8,294,400）
- 品質: low / medium / high / xhigh / max / auto
- シード値: パラメーターなし（supports_seed = False）
- 除外指示（negative prompt）のパラメーターはないため、指示文の末尾に「含めないもの」として付ける
- 料金（画像トークン 100万あたり）: 入力 $8、出力 $30。テキスト入力 $5
- 出力トークン数は公式の計算機（GptImageTokenCalculator）と同じ式で見積もる。参照画像の入力トークン数は
  GPT-Image-2.6 について公表されていないため、見積もりでは「未確定」とし、実際の値は応答の usage で記録する。

認証: クラウド環境では API キーを環境のネットワーク用シークレットに保存し、通信経路で付与される。
そのため OPENAI_API_KEY が未設定でも、置き換え用の仮の値を送る（実際のキーはコードにもログにも出ない）。
"""
from __future__ import annotations

import base64
import json
import math
import mimetypes
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .integrations import IMAGE_GENERATORS, ImageGenerator

API_BASE = "https://api.openai.com/v1"
PLACEHOLDER_KEY = "injected-by-environment-proxy"
TIMEOUT_SEC = 300  # 公式資料: 複雑な指示は最大2分程度かかることがある

# 公式の出力トークン計算機と同じ値（developers.openai.com の GptImageTokenCalculator）
_SUNBURST_FLARE = {"low": 16, "medium": 24, "high": 48, "xhigh": 64, "max": 96}
_BASE = {"gpt-image-2": {"low": 16, "medium": 48, "high": 96},
         "gpt-image-2.5": _SUNBURST_FLARE, "gpt-image-2.6": _SUNBURST_FLARE}
_V26_FACTOR = {"low": 4.2, "medium": 3.3, "high": 1, "xhigh": 1, "max": 1}

# 料金（USD / 100万トークン）。pricing ページの gpt-image-2.6-sunburst / -flare の行（標準処理）
PRICES_PER_1M = {
    "gpt-image-2.6-sunburst": {"image_input": 8.0, "text_input": 5.0, "image_output": 30.0},
    "gpt-image-2.6-flare": {"image_input": 8.0, "text_input": 5.0, "image_output": 30.0},
}


def model_family(model: str) -> str | None:
    for fam in ("gpt-image-2.6", "gpt-image-2.5", "gpt-image-2"):
        if model == fam or model.startswith(fam + "-"):
            return fam
    return None


def check_size(size: str) -> list[str]:
    """Sunburst / Flare の任意サイズの条件（公式資料）を満たすか。"""
    if size in ("auto", "1024x1024", "1536x1024", "1024x1536"):
        return []
    try:
        w, h = (int(x) for x in size.lower().split("x"))
    except ValueError:
        return [f"サイズの形式が不正です: {size!r}"]
    errs = []
    if w % 16 or h % 16:
        errs.append("縦横とも16の倍数である必要があります")
    if not 655_360 <= w * h <= 8_294_400:
        errs.append("総画素数は 655,360〜8,294,400 である必要があります")
    if max(w, h) > 3840:
        errs.append("長辺は3840px以下である必要があります")
    if max(w, h) / min(w, h) > 3:
        errs.append("縦横比は 1:3〜3:1 である必要があります")
    return errs


def estimate_output_tokens(model: str, size: str, quality: str) -> int | None:
    """公式の出力トークン計算機と同じ計算。対応外（auto など）は None。"""
    fam = model_family(model)
    if fam is None or quality not in _BASE[fam] or "x" not in size:
        return None
    w, h = (int(x) for x in size.lower().split("x"))
    big, small = max(w, h), min(w, h)
    o = _BASE[fam][quality]
    s = o / (big / small)
    c = math.floor(s)
    d = c + c % 2 if s - c == 0.5 else math.floor(s + 0.5)
    f = (o if w >= h else d) * (d if w >= h else o)
    p = math.ceil(f * (2e6 + w * h) / 4e6)
    return round(p * _V26_FACTOR[quality]) if fam == "gpt-image-2.6" else p


def _data_url(path: str) -> str:
    mime = mimetypes.guess_type(path)[0] or "image/png"
    return f"data:{mime};base64," + base64.b64encode(Path(path).read_bytes()).decode("ascii")


class OpenAIImageError(RuntimeError):
    pass


class OpenAIImageGenerator(ImageGenerator):
    provider = "openai"
    supports_seed = False  # 公式の API リファレンスにシード値のパラメーターはない
    placeholder = False
    paid = True

    def __init__(self, config: dict[str, Any] | None = None, *, opener=None) -> None:
        super().__init__(config)
        self.model = self.config.get("model")
        if not self.model:
            raise OpenAIImageError("series.json の generation.image.model が未設定です")
        self.params = dict(self.config.get("params") or {})
        self._opener = opener or urllib.request.urlopen
        self.last_usage: dict[str, Any] | None = None
        self.last_request: dict[str, Any] | None = None

    # ---- 見積もり ----
    def estimate(self, spec: dict[str, Any]) -> dict[str, Any]:
        size = self.params.get("size", "auto")
        quality = self.params.get("quality", "auto")
        out_tokens = estimate_output_tokens(self.model, size, quality)
        price = PRICES_PER_1M.get(self.model)
        return {
            "model": self.model, "size": size, "quality": quality,
            "output_tokens": out_tokens,
            "output_usd": (out_tokens * price["image_output"] / 1e6) if (out_tokens and price) else None,
            "reference_images": len(spec.get("references", [])),
            "input_usd": None,  # 参照画像の入力トークン数は GPT-Image-2.6 について未公表
            "note": "入力（参照画像・指示文）の料金は未確定。実際の値は生成後の usage で記録する。",
        }

    def cost_from_usage(self, usage: dict[str, Any] | None) -> float | None:
        price = PRICES_PER_1M.get(self.model)
        if not usage or not price:
            return None
        det = usage.get("input_tokens_details") or {}
        img_in = det.get("image_tokens", 0)
        txt_in = det.get("text_tokens", max(usage.get("input_tokens", 0) - img_in, 0))
        out = usage.get("output_tokens", 0)
        return round((img_in * price["image_input"] + txt_in * price["text_input"]
                      + out * price["image_output"]) / 1e6, 6)

    # ---- 生成 ----
    def build_request(self, spec: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        size = self.params.get("size", "auto")
        errs = check_size(size)
        if errs:
            raise OpenAIImageError(f"サイズ {size} は使えません: " + "、".join(errs))
        prompt = spec["prompt"]
        if spec.get("negative_prompt"):
            prompt += "\n\nDo not include: " + spec["negative_prompt"]
        body: dict[str, Any] = {"model": self.model, "prompt": prompt, "n": 1, "size": size,
                                "quality": self.params.get("quality", "auto"),
                                "output_format": self.params.get("output_format", "png")}
        for k in ("complexity", "moderation", "background"):
            if self.params.get(k) is not None:
                body[k] = self.params[k]
        refs = spec.get("references") or []
        if refs:
            if len(refs) > 16:
                raise OpenAIImageError("参照画像は最大16枚です")
            body["images"] = [{"image_url": _data_url(r["abs_path"])} for r in refs]
            return f"{API_BASE}/images/edits", body
        return f"{API_BASE}/images/generations", body

    def generate(self, spec: dict[str, Any], out_path: Path) -> Path:
        url, body = self.build_request(spec)
        self.last_usage = None
        self.last_request = {"endpoint": url.rsplit("/v1", 1)[1], "model": body["model"], "size": body["size"],
                             "quality": body["quality"], "reference_images": len(body.get("images", []))}
        key = os.environ.get("OPENAI_API_KEY") or PLACEHOLDER_KEY
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {key}"})
        try:
            with self._opener(req, timeout=TIMEOUT_SEC) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise OpenAIImageError(_describe_http_error(e)) from None
        except urllib.error.URLError as e:
            raise OpenAIImageError(f"OpenAI API に接続できません: {e.reason}") from None
        self.last_usage = payload.get("usage")
        data = payload.get("data") or []
        if not data or not data[0].get("b64_json"):
            raise OpenAIImageError("応答に画像データがありません")
        Path(out_path).write_bytes(base64.b64decode(data[0]["b64_json"]))
        return Path(out_path)


def _describe_http_error(e: urllib.error.HTTPError) -> str:
    detail = ""
    try:
        err = json.loads(e.read().decode("utf-8")).get("error") or {}
        detail = f" {err.get('type', '')}/{err.get('code', '')}: {err.get('message', '')}"
    except Exception:  # noqa: BLE001
        pass
    hints = {401: "（API キーが未登録か無効です）",
             403: "（組織の本人確認が必要な場合があります）",
             429: "（利用上限・残高・レート制限を確認してください）"}
    msg = f"OpenAI API エラー HTTP {e.code}{hints.get(e.code, '')}{detail}"
    return re.sub(r"sk-[A-Za-z0-9_\-*]+", "sk-***", msg)[:600]


IMAGE_GENERATORS["openai"] = OpenAIImageGenerator
