"""構成確認用動画（animatic）のテスト。ffmpeg で実際に小さな動画を作る。通信・費用なし。"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from storyengine import animatic, media
from storyengine.series import load_series
from storyengine.storage import read_json
from tests.test_media import SERIES_SRC, FakeGen


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg が必要")
class TestAnimatic(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "series"
        shutil.copytree(SERIES_SRC, self.root, ignore=shutil.ignore_patterns("drafts", "media", ".lock"))
        cfg = read_json(self.root / "series.json")
        cfg["generation"]["image"] = {"provider": "test", "model": "m", "params": {}}
        (self.root / "series.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        self.series = load_series(self.root)
        self._old = (animatic.OUT_W, animatic.OUT_H, animatic.UPSCALE)
        animatic.OUT_W, animatic.OUT_H, animatic.UPSCALE = 72, 128, 2  # テストは小さく

    def tearDown(self):
        animatic.OUT_W, animatic.OUT_H, animatic.UPSCALE = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_requires_all_adopted_images(self):
        with self.assertRaises(animatic.AnimaticError):
            animatic.plan(self.series, 1)

    def test_build_reuse_and_persistable(self):
        media.run_images(self.series, 1, FakeGen())
        res = animatic.build(self.series, 1)
        self.assertEqual(res["action"], "built")
        v = res["version"]
        self.assertEqual((v["width"], v["height"]), (72, 128))
        self.assertAlmostEqual(v["duration"], 20.0, delta=0.25)
        self.assertEqual([c["duration_sec"] for c in v["conditions"]["cuts"]], [3, 4, 4, 5, 4])
        self.assertEqual(animatic.build(self.series, 1)["action"], "reuse")  # 同じ条件は作り直さない
        rec = read_json(self.series.media_records_path(1))
        self.assertIn("animatic", rec["slots"])
        self.assertIsNone(media._verify_local(self.series, v, None))  # persist / restore の検査に通る

    def test_invalid_move_rejected(self):
        with self.assertRaises(animatic.AnimaticError):
            animatic._move({"animatic": {"zoom": [0.5, 1.0]}})
        with self.assertRaises(animatic.AnimaticError):
            animatic._move({"animatic": {"focus": [[0.5, 1.5], [0.5, 0.5]]}})


if __name__ == "__main__":
    unittest.main()
