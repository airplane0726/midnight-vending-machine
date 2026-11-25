from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from storyengine import publish, roughcut, site
from storyengine.finalize import export
from storyengine.series import load_series
from tests.test_media import SERIES_SRC

ROOT = Path(__file__).resolve().parents[1]


class TestFill(unittest.TestCase):
    def test_fill_and_missing(self):
        ctx = {"a": {"b": "x", "n": None, "l": ["#1", "#2"]}}
        self.assertEqual(publish.fill("{{a.b}}-{{ a.l }}-{{a.n|準備中}}", ctx), "x-#1 #2-準備中")
        with self.assertRaises(publish.PublishError):
            publish.fill("{{a.n}}", ctx)  # 値がなく代わりもない → 存在しない値を作らない


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg が必要")
class TestPublishFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        shutil.copytree(ROOT / "brand", self.tmp / "brand", ignore=shutil.ignore_patterns("out"))
        self.root = self.tmp / "series" / "s"
        shutil.copytree(SERIES_SRC, self.root,
                        ignore=shutil.ignore_patterns("drafts", "media", ".lock", "ledger.json"))
        shutil.rmtree(self.root / "publish" / "ep001", ignore_errors=True)
        (self.root / "edits" / "ep001_covers.json").unlink(missing_ok=True)
        (self.root / "edits" / "ep001_publish.json").write_text(json.dumps({"cover": None, "stills": [1.0]}))
        self.series = load_series(self.root)
        self.series.config["format"] = dict(self.series.config["format"], resolution="108x192", total_seconds=2)
        self.series.config["publish_export"] = {"loudness": [-30, -5]}
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=0x405060:s=108x192",
                        "-frames:v", "1", str(self.root / "a.png")], check=True)
        roughcut.build(self.series, 1, {"output": {"width": 108, "height": 192, "fps": 30},
                                        "shots": [{"duration": 2.0, "base": "a.png"}],
                                        "sound": {"ambience": [{"type": "hum", "end": 2.0, "gain_db": -20}]}})
        export(self.series, 1)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def set_brand(self, status):
        p = self.tmp / "brand" / "brand.json"
        b = json.loads(p.read_text(encoding="utf-8"))
        b.update(status=status, name_ja="てすと", name_en="test")
        b["logo"]["font_size"] = 10
        p.write_text(json.dumps(b, ensure_ascii=False), encoding="utf-8")

    def test_gate_package_approve_post_once(self):
        self.set_brand("provisional")
        with self.assertRaises(publish.GateError):  # 第1話が公開準備中 → 第2話は生成しない
            publish.assert_can_generate(self.series, 2)
        man = publish.build_package(self.series, 1)
        self.assertTrue(man["video"]["post"]["checks_ok"])
        self.assertNotEqual(man["video"]["post"]["sha256"], man["video"]["master"]["sha256"])  # 原版は別に残る
        self.assertIn("youtube", man["texts"])
        self.assertNotIn("{{", (publish.package_dir(self.series, 1) / "youtube.md").read_text(encoding="utf-8"))
        self.assertEqual(len(man["stills"]), 1)
        with self.assertRaises(publish.GateError):  # 未承認は投稿できない
            publish.assert_can_post(self.series, 1, "youtube")
        e = publish.request_review(self.series, 1)
        with self.assertRaises(publish.GateError):  # 仮のブランド名では承認できない
            publish.approve(self.series, 1, e["post_sha256"][:12])
        # ブランド確定 → 作り直し → 承認
        self.set_brand("final")
        man = publish.build_package(self.series, 1)
        e = publish.request_review(self.series, 1)
        with self.assertRaises(publish.GateError):  # 別のファイルの sha では承認できない
            publish.approve(self.series, 1, "00000000")
        publish.approve(self.series, 1, e["post_sha256"][:12])
        publish.assert_can_post(self.series, 1, "youtube")
        publish.record_post(self.series, 1, "youtube", url=None)
        with self.assertRaises(publish.GateError):  # 二重投稿の防止
            publish.record_post(self.series, 1, "youtube")
        with self.assertRaises(publish.GateError):  # まだ全部の投稿先に出していない
            publish.assert_can_generate(self.series, 2)
        publish.record_post(self.series, 1, "instagram", done=True)
        publish.assert_can_generate(self.series, 2)  # 公開済みになったら次の話へ進める

    def test_rebuilt_package_invalidates_approval(self):
        self.set_brand("final")
        publish.build_package(self.series, 1)
        e = publish.request_review(self.series, 1)
        publish.approve(self.series, 1, e["post_sha256"][:12])
        (publish.package_dir(self.series, 1) / "manifest.json").write_text(
            json.dumps(dict(json.loads((publish.package_dir(self.series, 1) / "manifest.json").read_text()),
                            package_sha256="sha256:changed")))
        with self.assertRaises(publish.GateError):
            publish.assert_can_post(self.series, 1, "youtube")

    def test_site_never_links_missing_accounts(self):
        self.set_brand("provisional")
        publish.build_package(self.series, 1)
        out = self.tmp / "site"
        site.build(self.series, out)
        html = (out / "index.html").read_text(encoding="utf-8")
        self.assertIn("準備中", html)
        self.assertNotIn("href=\"http", html)
        self.assertIn("noindex", html)
        site.build(self.series, self.tmp / "site2", public_only=True)
        self.assertNotIn("ep001.html", (self.tmp / "site2" / "index.html").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
