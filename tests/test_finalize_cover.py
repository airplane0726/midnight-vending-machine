from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from storyengine import cover, finalize, roughcut
from storyengine.series import load_series
from tests.test_media import SERIES_SRC


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg が必要")
class TestFinalizeCover(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "series"
        shutil.copytree(SERIES_SRC, self.root, ignore=shutil.ignore_patterns("drafts", "media", ".lock"))
        self.series = load_series(self.root)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=0x405060:s=108x192",
                        "-frames:v", "1", str(self.root / "a.png")], check=True)
        spec = {"output": {"width": 108, "height": 192, "fps": 30},
                "shots": [{"duration": 2.0, "base": "a.png"}],
                "sound": {"ambience": [{"type": "hum", "end": 2.0, "gain_db": -20}],
                          "events": [{"t": 0.5, "type": "bell", "note": "A5"}]}}
        roughcut.build(self.series, 1, spec)
        self.fmt = {"resolution": "108x192", "fps": 30, "total_seconds": 2}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_export_checks_and_reuse(self):
        self.series.config["format"] = dict(self.series.config["format"], resolution="108x192", total_seconds=2)
        self.series.config["publish_export"] = {"loudness": [-30, -5]}
        res = finalize.export(self.series, 1)
        v = res["version"]
        items = {i["item"]: i for i in v["checks"]["items"]}
        self.assertTrue(items["キーフレーム間隔（最大）"]["ok"], items)
        self.assertTrue(items["faststart（先頭に目次）"]["ok"])
        self.assertTrue(items["色の情報"]["ok"])
        self.assertTrue(items["ピーク（音割れ）"]["ok"])
        self.assertTrue(v["checks"]["ok"], v["checks"])
        self.assertEqual(finalize.export(self.series, 1)["action"], "reuse")
        # 表紙: 完成動画の1コマ＋文字
        spec = {"source_slot": "final", "covers": [
            {"name": "x", "time": 1.0, "shade": {"top": 0.5, "height": 0.3},
             "lines": [{"text": "第1話", "size": 20, "y": 20}]}]}
        out = cover.build(self.series, 1, spec, preview_dir=self.tmp / "pv")
        cv = out[0]["version"]
        self.assertEqual((cv["width"], cv["height"], cv["cost_usd"]), (108, 192, 0.0))
        self.assertTrue((self.tmp / "pv" / "covers_preview.png").exists())
        self.assertEqual(cover.build(self.series, 1, spec)[0]["action"], "reuse")

    def test_check_flags_wrong_size(self):
        src = self.series.resolve(roughcut_file(self.series))
        res = finalize.check(src, {"resolution": "1080x1920", "fps": 30, "total_seconds": 2})
        bad = {i["item"] for i in res["items"] if not i["ok"]}
        self.assertIn("大きさ", bad)
        self.assertFalse(res["ok"])


def roughcut_file(series):
    from storyengine.media import load_records
    return load_records(series, 1)["slots"]["roughcut"]["versions"][-1]["file"]


if __name__ == "__main__":
    unittest.main()
