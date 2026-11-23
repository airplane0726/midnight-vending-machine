"""OpenAI 画像生成器のテスト。通信はすべて差し替えるので、API には接続せず費用もかからない。"""
from __future__ import annotations

import base64
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from storyengine import media
from storyengine.cli import main
from storyengine.integrations import create_image_generator
from storyengine.openai_images import (OpenAIImageGenerator, check_size, estimate_output_tokens)
from storyengine.series import load_series
from storyengine.storage import read_json
from tests.test_media import SERIES_SRC, png_bytes

USAGE = {"input_tokens": 5000, "input_tokens_details": {"image_tokens": 4600, "text_tokens": 400},
         "output_tokens": 1413, "output_tokens_details": {"image_tokens": 1413, "text_tokens": 0}}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    def __init__(self, payload=None, error=None):
        self.requests = []
        self.payload = payload if payload is not None else {
            "data": [{"b64_json": base64.b64encode(png_bytes(9, 16)).decode()}], "usage": USAGE}
        self.error = error

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        if self.error:
            raise self.error
        return FakeResponse(json.dumps(self.payload).encode())


CFG = {"provider": "openai", "model": "gpt-image-2.6-sunburst",
       "params": {"size": "1152x2048", "quality": "high", "output_format": "png"}}


class TestSpecs(unittest.TestCase):
    def test_output_token_formula_matches_published_gpt_image_2_table(self):
        # 公式資料の旧モデル表（gpt-image-2, 1024x1536）: low $0.005 / medium $0.041 / high $0.165
        for q, usd in (("low", 0.005), ("medium", 0.041), ("high", 0.165)):
            t = estimate_output_tokens("gpt-image-2", "1024x1536", q)
            self.assertAlmostEqual(t * 30 / 1e6, usd, delta=0.0006)

    def test_gpt_image_26_estimate(self):
        self.assertEqual(estimate_output_tokens("gpt-image-2.6-sunburst", "1152x2048", "high"), 1413)
        self.assertEqual(estimate_output_tokens("gpt-image-2.6-flare", "1152x2048", "high"), 1413)
        self.assertIsNone(estimate_output_tokens("gpt-image-2.6-sunburst", "auto", "high"))

    def test_size_rules(self):
        self.assertEqual(check_size("1152x2048"), [])
        self.assertTrue(check_size("1080x1920"))  # 1080 は16の倍数ではない
        self.assertTrue(check_size("640x640"))    # 画素数不足
        self.assertTrue(check_size("1024x3840"))  # 3:1 を超える


class TestGenerator(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def spec(self, refs=2):
        paths = []
        for i in range(refs):
            p = self.tmp / f"r{i}.png"
            p.write_bytes(png_bytes(8, 8))
            paths.append({"subject": f"s{i}", "path": f"references/r{i}.png", "abs_path": str(p)})
        return {"slot": "cut01_start", "prompt": "A scene.", "negative_prompt": "text, watermark",
                "resolution": "1080x1920", "references": paths}

    def test_edits_request_with_references(self):
        op = FakeOpener()
        gen = OpenAIImageGenerator(CFG, opener=op)
        out = gen.generate(self.spec(), self.tmp / "out.part")
        self.assertEqual(media.inspect_image(out)["format"], "png")
        req = op.requests[0]
        self.assertTrue(req.full_url.endswith("/v1/images/edits"))
        body = json.loads(req.data)
        self.assertEqual((body["model"], body["size"], body["quality"], body["n"]),
                         ("gpt-image-2.6-sunburst", "1152x2048", "high", 1))
        self.assertEqual(len(body["images"]), 2)
        self.assertTrue(body["images"][0]["image_url"].startswith("data:image/png;base64,"))
        self.assertIn("Do not include: text, watermark", body["prompt"])
        for absent in ("seed", "input_fidelity", "negative_prompt"):
            self.assertNotIn(absent, body)
        self.assertEqual(gen.last_usage, USAGE)
        self.assertAlmostEqual(gen.cost_from_usage(USAGE), (4600 * 8 + 400 * 5 + 1413 * 30) / 1e6)

    def test_generations_endpoint_without_references(self):
        op = FakeOpener()
        OpenAIImageGenerator(CFG, opener=op).generate(self.spec(refs=0), self.tmp / "o.part")
        self.assertTrue(op.requests[0].full_url.endswith("/v1/images/generations"))
        self.assertNotIn("images", json.loads(op.requests[0].data))

    def test_auth_header_placeholder_or_env(self):
        op = FakeOpener()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENAI_API_KEY", None)
            OpenAIImageGenerator(CFG, opener=op).generate(self.spec(), self.tmp / "a.part")
        self.assertEqual(op.requests[0].get_header("Authorization"), "Bearer injected-by-environment-proxy")
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-local"}):
            OpenAIImageGenerator(CFG, opener=op).generate(self.spec(), self.tmp / "b.part")
        self.assertEqual(op.requests[1].get_header("Authorization"), "Bearer sk-test-local")

    def test_http_error_is_described_without_key(self):
        body = json.dumps({"error": {"type": "invalid_request_error", "code": "invalid_api_key",
                                     "message": "Incorrect API key provided: sk-abc123xyz"}}).encode()
        err = urllib.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(body))
        gen = OpenAIImageGenerator(CFG, opener=FakeOpener(error=err))
        with self.assertRaises(Exception) as cm:
            gen.generate(self.spec(), self.tmp / "e.part")
        self.assertIn("HTTP 401", str(cm.exception))
        self.assertNotIn("sk-abc123xyz", str(cm.exception))

    def test_bad_size_rejected_before_request(self):
        op = FakeOpener()
        gen = OpenAIImageGenerator(dict(CFG, params={"size": "1080x1920", "quality": "high"}), opener=op)
        with self.assertRaises(Exception):
            gen.generate(self.spec(), self.tmp / "x.part")
        self.assertEqual(op.requests, [])

    def test_registered_provider(self):
        self.assertIsInstance(create_image_generator(CFG), OpenAIImageGenerator)


class TestPipelineWithOpenAI(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "series"
        shutil.copytree(SERIES_SRC, self.root, ignore=shutil.ignore_patterns("drafts", "media", ".lock"))
        self.series = load_series(self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_records_usage_cost_and_references(self):
        op = FakeOpener()
        gen = OpenAIImageGenerator(self.series.config["generation"]["image"], opener=op)
        res = media.run_images(self.series, 1, gen, cuts=[1])
        self.assertEqual(res.done, [1])
        v = read_json(self.series.media_records_path(1))["slots"]["cut01_start"]["versions"][-1]
        self.assertEqual(v["status"], "complete")
        self.assertIsNone(v["seed"])
        self.assertEqual(v["usage"], USAGE)
        self.assertGreater(v["cost_usd"], 0)
        self.assertEqual(v["request"]["reference_images"], 2)
        body = json.loads(op.requests[0].data)
        self.assertEqual(len(body["images"]), 2)  # ポコと自販機の基準画像
        self.assertIn("exactly one Poco", body["prompt"])

    def test_failed_request_keeps_usage_and_is_not_complete(self):
        gen = OpenAIImageGenerator(self.series.config["generation"]["image"],
                                   opener=FakeOpener(payload={"data": [], "usage": USAGE}))
        res = media.run_images(self.series, 1, gen, cuts=[1])
        self.assertEqual(res.failed[0][0], 1)
        v = read_json(self.series.media_records_path(1))["slots"]["cut01_start"]["versions"][-1]
        self.assertEqual(v["status"], "failed")
        self.assertIn("cost_usd", v)  # 失敗しても請求が発生しうるので記録する

    def run_cli(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["--series", str(self.root), "images", *args])
        return code, buf.getvalue()

    def test_cli_requires_allow_paid_and_shows_estimate(self):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network must not be used")):
            code, out = self.run_cli("--dry-run", "--cuts", "1")
            self.assertEqual(code, 0)
            self.assertIn("gpt-image-2.6-sunburst 1152x2048 quality=high", out)
            self.assertIn("1413 トークン", out)
            self.assertIn("未確定", out)
            code, out = self.run_cli("--cuts", "1")
            self.assertEqual(code, 2)
            self.assertIn("--allow-paid", out)
        self.assertFalse((self.root / "media").exists())


if __name__ == "__main__":
    unittest.main()
