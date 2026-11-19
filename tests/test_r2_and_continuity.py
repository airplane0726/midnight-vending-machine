"""Cloudflare R2 保存先と、生成済み画像の参照（continuity_references）のテスト。通信なし・費用なし。"""
from __future__ import annotations

import copy
import io
import json
import shutil
import tempfile
import unittest
import urllib.error
from pathlib import Path

from storyengine import media
from storyengine.cloudflare_r2 import CloudflareR2Store, R2Error
from storyengine.commit import plan_commit
from storyengine.integrations import create_asset_store
from storyengine.series import load_series
from storyengine.storage import read_json
from tests.test_media import SERIES_SRC, FakeGen

R2CFG = {"provider": "cloudflare-r2", "params": {"account_id": "acc123", "bucket": "midnight-vending-machine-media"}}


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeR2:
    """URL ごとにバイト列を保存する、R2 REST API の代わり。"""

    def __init__(self, corrupt=False, status=None):
        self.objects: dict[str, bytes] = {}
        self.requests = []
        self.corrupt, self.status = corrupt, status

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        if self.status:
            body = json.dumps({"success": False, "errors": [{"code": 10006, "message": "not found"}]}).encode()
            raise urllib.error.HTTPError(req.full_url, self.status, "err", {}, io.BytesIO(body))
        if req.get_method() == "PUT":
            self.objects[req.full_url] = req.data
            return FakeResp(json.dumps({"success": True, "result": {}}).encode())
        data = self.objects[req.full_url]
        return FakeResp(data[:-1] if self.corrupt else data)


class TestR2Store(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.file = self.tmp / "a.png"
        self.file.write_bytes(b"\x89PNG fake bytes")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_upload_verify_and_download(self):
        fake = FakeR2()
        store = CloudflareR2Store(R2CFG, opener=fake)
        uri = store.upload(self.file, "midnight-vending-machine/media/ep001/images/a.png")
        self.assertEqual(uri, "r2://midnight-vending-machine-media/midnight-vending-machine/media/ep001/images/a.png")
        put = fake.requests[0]
        self.assertEqual(put.get_method(), "PUT")
        self.assertTrue(put.full_url.startswith("https://api.cloudflare.com/client/v4/accounts/acc123/r2/buckets/"))
        self.assertIn("/objects/midnight-vending-machine/media/ep001/images/a.png", put.full_url)  # スラッシュはそのまま
        self.assertEqual(put.get_header("Content-type"), "image/png")
        self.assertEqual(put.get_header("Authorization"), "Bearer injected-by-environment-proxy")
        self.assertEqual(fake.requests[1].get_method(), "GET")  # 保存後に照合
        dest = self.tmp / "b.png"
        store.download(uri, dest)
        self.assertEqual(dest.read_bytes(), self.file.read_bytes())

    def test_mismatch_after_upload_is_error(self):
        with self.assertRaises(R2Error):
            CloudflareR2Store(R2CFG, opener=FakeR2(corrupt=True)).upload(self.file, "k/a.png")

    def test_http_error_message(self):
        with self.assertRaises(R2Error) as cm:
            CloudflareR2Store(R2CFG, opener=FakeR2(status=404)).upload(self.file, "k/a.png")
        self.assertIn("HTTP 404", str(cm.exception))
        self.assertIn("バケット名", str(cm.exception))

    def test_config_required(self):
        with self.assertRaises(R2Error):
            create_asset_store({"provider": "cloudflare-r2", "params": {"bucket": "b"}})
        self.assertIsInstance(create_asset_store(R2CFG), CloudflareR2Store)


class SeriesCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "series"
        shutil.copytree(SERIES_SRC, self.root, ignore=shutil.ignore_patterns("drafts", "media", ".lock"))
        cfg = read_json(self.root / "series.json")
        cfg["generation"]["image"] = {"provider": "test", "model": "model-a", "params": {}}
        (self.root / "series.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        self.series = load_series(self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def plans(self):
        return {p.cut_number: p for p in media.plan_images(self.series, 1)[0]}


class TestPersist(SeriesCase):
    def test_persist_all_versions_once(self):
        media.run_images(self.series, 1, FakeGen(), cuts=[1])
        media.run_images(self.series, 1, FakeGen(), cuts=[1])  # 同じ条件なので再利用（版は1つ）
        fake = FakeR2()
        store = CloudflareR2Store(R2CFG, opener=fake)
        res = media.persist_all(self.series, store, [1])
        self.assertEqual([r[1] for r in res], ["persisted"])
        self.assertEqual(media.persist_all(self.series, store, [1]), [])  # 2回目は何もしない
        self.assertTrue(self.plans()[1].persisted)

    def test_failed_upload_is_not_persisted(self):
        media.run_images(self.series, 1, FakeGen(), cuts=[1])
        res = media.persist_all(self.series, CloudflareR2Store(R2CFG, opener=FakeR2(corrupt=True)), [1])
        self.assertEqual(res[0][1], "upload_failed")
        self.assertFalse(self.plans()[1].persisted)


class TestContinuityReferences(SeriesCase):
    """第1話のカット2は、採用したカット1の画像を参照する。"""

    def test_cut2_waits_for_cut1_and_uses_it(self):
        self.assertEqual(self.plans()[2].action, "blocked")  # カット1が未完成
        media.run_images(self.series, 1, FakeGen(), cuts=[1])
        p2 = self.plans()[2]
        self.assertEqual(p2.action, "generate")
        gen_refs = [r for r in p2.references if r.get("kind") == "generated"]
        self.assertEqual(gen_refs[0]["path"], self.plans()[1].current["file"])

        captured = []

        class Capture(FakeGen):
            def generate(self, spec, out_path):
                captured.append(len(spec["references"]))
                return super().generate(spec, out_path)

        media.run_images(self.series, 1, Capture(), cuts=[2])
        self.assertEqual(captured, [3])  # ポコ・自販機・カット1の画像

    def test_new_cut1_image_marks_cut2_for_update(self):
        media.run_images(self.series, 1, FakeGen(), cuts=[1, 2])  # 同じ実行でカット1→カット2
        self.assertEqual(self.plans()[2].action, "reuse")
        cfg = read_json(self.root / "series.json")
        cfg["style"]["negative_prompt"] += ", fog"  # カット1の条件が変わる
        (self.root / "series.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        self.series = load_series(self.root)
        plans = self.plans()
        self.assertEqual((plans[1].action, plans[2].action), ("update", "blocked"))
        media.run_images(self.series, 1, FakeGen(), cuts=[1])
        self.assertEqual(self.plans()[2].action, "update")  # 新しいカット1を参照し直す必要がある（以前の画像は保持）

    def test_validation_requires_earlier_cut(self):
        ep = copy.deepcopy(read_json(self.series.episode_dir(1) / "episode.json"))
        ep["cuts"][1]["continuity_references"] = [{"slot": "cut02_start"}]
        errs = plan_commit(self.series, ep, replace=True).errors
        self.assertTrue(any("continuity_references" in e for e in errs))


if __name__ == "__main__":
    unittest.main()
