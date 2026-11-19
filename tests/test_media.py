"""画像の生成記録・再利用・参照画像・差し替え時の保持のテスト。

外部サービスには接続しない。テスト用の生成器はテスト内だけで使う。
"""
from __future__ import annotations

import copy
import io
import json
import shutil
import struct
import tempfile
import unittest
import zlib
from contextlib import redirect_stdout
from pathlib import Path

from storyengine import media
from storyengine.cli import main
from storyengine.commit import commit
from storyengine.integrations import AssetStore, ImageGenerator, NotConfiguredError
from storyengine.series import load_series
from storyengine.storage import read_json

ROOT = Path(__file__).resolve().parent.parent
SERIES_SRC = ROOT / "series" / "midnight-vending-machine"


def png_bytes(w: int = 9, h: int = 16, color: tuple[int, int, int] = (40, 60, 120)) -> bytes:
    def chunk(t: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + t + body + struct.pack(">I", zlib.crc32(t + body) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + bytes(color) * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def jpeg_bytes(w: int = 9, h: int = 16) -> bytes:
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\x01\x11\x00"
    sos = b"\xff\xda" + struct.pack(">HB", 8, 1) + b"\x01\x00\x00\x3f\x00"
    return b"\xff\xd8" + sof + sos + b"\x00\x00" + b"\xff\xd9"


class FakeGen(ImageGenerator):
    provider = "test"
    supports_seed = True

    def __init__(self, fail_slots=(), payload=None):
        super().__init__({})
        self.calls: list[str] = []
        self.fail_slots = set(fail_slots)
        self.payload = payload
        self.seeds: list = []

    def generate(self, spec, out_path):
        self.calls.append(spec["slot"])
        self.seeds.append(spec.get("seed"))
        if spec["slot"] in self.fail_slots:
            Path(out_path).write_bytes(b"\x89PNG partial")  # 不完全なファイルを残して失敗
            raise RuntimeError("service error (simulated)")
        Path(out_path).write_bytes(self.payload if self.payload is not None
                                   else png_bytes(color=(len(self.calls), 0, 0)))
        return Path(out_path)


class NoSeedGen(FakeGen):
    supports_seed = False


class PlaceholderGen(FakeGen):
    placeholder = True


class FakeStore(AssetStore):
    provider = "fake-store"

    def __init__(self, root: Path, fail=False):
        super().__init__({})
        self.root, self.fail = root, fail

    def upload(self, local_path, object_key):
        if self.fail:
            raise OSError("upload failed (simulated)")
        dst = self.root / object_key
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(local_path, dst)
        return f"fake://{object_key}"

    def download(self, uri, dest):
        shutil.copy(self.root / uri.removeprefix("fake://"), dest)
        return dest


class MediaTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "series"
        shutil.copytree(SERIES_SRC, self.root,
                        ignore=shutil.ignore_patterns("drafts", "media", ".lock"))
        self.set_config(lambda c: c.setdefault("generation", {}).update(
            image={"provider": "test", "model": "model-a", "params": {}}))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def set_config(self, fn):
        p = self.root / "series.json"
        cfg = read_json(p)
        fn(cfg)
        p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        self.series = load_series(self.root)

    def plans(self, **kw):
        return {p.cut_number: p for p in media.plan_images(self.series, 1, **kw)[0]}

    def records(self):
        return read_json(self.series.media_records_path(1))

    def image_files(self):
        d = self.root / "media" / "ep001" / "images"
        return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


class TestInspectImage(MediaTestCase):
    def test_valid_png_and_jpeg(self):
        p = self.tmp / "a.png"
        p.write_bytes(png_bytes())
        self.assertEqual(media.inspect_image(p)["format"], "png")
        j = self.tmp / "a.jpg"
        j.write_bytes(jpeg_bytes())
        info = media.inspect_image(j)
        self.assertEqual((info["format"], info["width"], info["height"]), ("jpeg", 9, 16))

    def test_broken_images_rejected(self):
        good = png_bytes()
        cases = {
            "truncated": good[:-20],
            "bad_crc": good[:40] + bytes([good[40] ^ 0xFF]) + good[41:],
            "not_image": b"hello",
            "jpeg_truncated": jpeg_bytes()[:-2],
        }
        for name, data in cases.items():
            p = self.tmp / name
            p.write_bytes(data)
            with self.subTest(name), self.assertRaises(media.InvalidImageError):
                media.inspect_image(p)


class TestGeneration(MediaTestCase):
    def test_dry_run_plan(self):
        plans = self.plans()
        self.assertEqual(sorted(plans), [1, 2, 3, 4, 5])
        for p in plans.values():
            self.assertEqual(p.action, "generate")
            # 開始画像に映るのはポコと自販機の路地（モチはカット5の途中から）
            self.assertEqual([r["subject"] for r in p.references], ["poco", "vending-corner"])
            self.assertEqual(p.missing_references, [])
            self.assertEqual(p.output_dir, "media/ep001/images")
        self.assertFalse((self.root / "media").exists())  # dry-run は何も書かない

    def test_generate_then_reuse(self):
        gen = FakeGen()
        res = media.run_images(self.series, 1, gen)
        self.assertEqual(res.done, [1, 2, 3, 4, 5])
        self.assertEqual(len(self.image_files()), 5)
        rec = self.records()
        v = rec["slots"]["cut01_start"]["versions"][-1]
        self.assertEqual(v["status"], "complete")
        self.assertEqual(v["seed"], media.seed_for(v["key"]))
        self.assertEqual(v["storage"]["status"], "local_only")
        self.assertEqual(v["conditions"]["model"]["model"], "model-a")
        self.assertTrue(v["file"].startswith("media/ep001/images/cut01_start-"))

        gen2 = FakeGen()
        media.run_images(self.series, 1, gen2)
        self.assertEqual(gen2.calls, [])
        self.assertTrue(all(p.action == "reuse" for p in self.plans().values()))
        self.assertIn("保存完了ではありません", media.storage_summary(list(self.plans().values()), None))

    def test_seed_only_when_supported(self):
        gen = NoSeedGen()
        media.run_images(self.series, 1, gen, cuts=[1])
        self.assertEqual(gen.seeds, [None])
        self.assertIsNone(self.records()["slots"]["cut01_start"]["versions"][-1]["seed"])

    def test_resume_after_failure(self):
        media.run_images(self.series, 1, FakeGen(fail_slots={"cut03_start"}))
        rec = self.records()["slots"]
        self.assertEqual(rec["cut03_start"]["versions"][-1]["status"], "failed")
        self.assertNotIn("cut04_start", rec)  # 失敗後は止まる
        self.assertEqual(len(self.image_files()), 2)  # 不完全なファイルは残らない
        plans = self.plans()
        self.assertEqual([plans[i].action for i in (1, 2, 3)], ["reuse", "reuse", "generate"])

        gen = FakeGen()
        res = media.run_images(self.series, 1, gen)
        self.assertEqual(gen.calls, ["cut03_start", "cut04_start", "cut05_start"])
        self.assertEqual(res.done, [3, 4, 5])

    def test_invalid_outputs_not_registered(self):
        for payload in (png_bytes()[:-10], png_bytes(10, 10)):  # 壊れた画像 / 縦横比違い
            with self.subTest(len(payload)):
                res = media.run_images(self.series, 1, FakeGen(payload=payload), cuts=[1])
                self.assertEqual(res.failed[0][0], 1)
                versions = self.records()["slots"]["cut01_start"]["versions"]
                self.assertTrue(all(v["status"] == "failed" for v in versions))
                self.assertEqual(self.image_files(), [])
                self.assertEqual(self.plans()[1].action, "generate")

    def test_placeholder_generator_refused(self):
        with self.assertRaises(NotConfiguredError):
            media.run_images(self.series, 1, PlaceholderGen())
        self.assertEqual(self.image_files(), [])
        self.assertFalse(self.series.media_records_path(1).exists())

    def test_unconnected_generator_generates_nothing(self):
        res = media.run_images(self.series, 1, None)
        self.assertIn("未接続", res.failed[0][1])
        self.assertEqual(self.image_files(), [])

    def test_condition_change_marks_update_and_keeps_old(self):
        media.run_images(self.series, 1, FakeGen())
        old_files = self.image_files()
        self.set_config(lambda c: c["generation"]["image"].update(model="model-b"))
        plans = self.plans()
        self.assertTrue(all(p.action == "update" for p in plans.values()))
        self.assertTrue(all(len(p.previous) == 1 for p in plans.values()))
        media.run_images(self.series, 1, FakeGen())
        self.assertEqual(len(self.image_files()), 10)
        self.assertTrue(set(old_files) <= set(self.image_files()))

        self.set_config(lambda c: c["style"].update(negative_prompt=c["style"]["negative_prompt"] + ", fog"))
        self.assertTrue(all(p.action == "update" for p in self.plans().values()))

    def test_lost_or_corrupt_file_needs_regeneration(self):
        media.run_images(self.series, 1, FakeGen())
        f = self.root / self.records()["slots"]["cut02_start"]["versions"][-1]["file"]
        f.write_bytes(png_bytes()[:-5])
        self.assertEqual(self.plans()[2].action, "regenerate")
        f.unlink()
        self.assertEqual(self.plans()[2].action, "regenerate")

    def test_persistent_storage_and_restore(self):
        store = FakeStore(self.tmp / "bucket")
        media.run_images(self.series, 1, FakeGen(), store=store)
        plans = list(self.plans(store=store).values())
        self.assertTrue(all(p.persisted for p in plans))
        self.assertIn("保存完了", media.storage_summary(plans, store))
        shutil.rmtree(self.root / "media" / "ep001" / "images")  # クラウド環境の終了を想定
        self.assertEqual(self.plans(store=store)[1].action, "restore")
        gen = FakeGen()
        media.run_images(self.series, 1, gen, store=store)
        self.assertEqual(gen.calls, [])
        self.assertEqual(len(self.image_files()), 5)

    def test_upload_failure_is_not_saved_and_retried(self):
        media.run_images(self.series, 1, FakeGen(), store=FakeStore(self.tmp / "b", fail=True))
        v = self.records()["slots"]["cut01_start"]["versions"][-1]
        self.assertEqual((v["status"], v["storage"]["status"]), ("complete", "upload_failed"))
        store = FakeStore(self.tmp / "b")
        self.assertNotIn("すべて永続保存済み", media.storage_summary(list(self.plans(store=store).values()), store))
        media.run_images(self.series, 1, FakeGen(), store=store)
        plans = list(self.plans(store=store).values())
        self.assertTrue(all(p.persisted for p in plans))


class TestReferences(MediaTestCase):
    def add_refs(self, create_files=True):
        paths = {"poco": "references/characters/poco/front.png",
                 "mochi": "references/characters/mochi/front.png",
                 "vending-corner": "references/locations/vending-corner/wide.png"}
        if create_files:
            for p in paths.values():
                (self.root / p).write_bytes(png_bytes(8, 8))

        def fn(c):
            c["characters"]["poco"]["reference_images"] = [{"path": paths["poco"], "view": "front"}]
            c["characters"]["mochi"]["reference_images"] = [{"path": paths["mochi"], "view": "front"}]
            c["locations"]["vending-corner"]["reference_images"] = [{"path": paths["vending-corner"], "view": "wide"}]
        self.set_config(fn)
        return paths

    def test_references_flow_into_production_and_generation(self):
        paths = self.add_refs()
        prod, _ = media.current_production(self.series, 1)
        cut5 = prod["cuts"][4]
        self.assertEqual({r["subject"] for r in cut5["image"]["references"]}, {"poco", "vending-corner"})
        self.assertEqual({r["subject"] for r in cut5["video"]["references"]}, {"poco", "mochi", "vending-corner"})

        captured = []

        class Capture(FakeGen):
            def generate(self, spec, out_path):
                captured.append(spec["references"])
                return super().generate(spec, out_path)

        media.run_images(self.series, 1, Capture(), cuts=[1])
        self.assertTrue(all(Path(r["abs_path"]).is_file() for r in captured[0]))
        # 参照画像の中身が変わると更新が必要になる
        (self.root / paths["poco"]).write_bytes(png_bytes(8, 8, color=(1, 2, 3)))
        self.assertEqual(self.plans()[1].action, "update")

    def test_missing_reference_blocks_generation(self):
        self.add_refs(create_files=False)
        plans = self.plans()
        self.assertEqual(plans[1].action, "blocked")
        gen = FakeGen()
        res = media.run_images(self.series, 1, gen)
        self.assertEqual(gen.calls, [])
        self.assertIn("参照画像", res.failed[0][1])

    def test_reference_path_must_stay_inside(self):
        self.set_config(lambda c: None)
        cfg = read_json(self.root / "series.json")
        cfg["characters"]["poco"]["reference_images"] = [{"path": "../../secret.png"}]
        (self.root / "series.json").write_text(json.dumps(cfg), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_series(self.root)


class TestRegisteredReferences(MediaTestCase):
    """リポジトリに登録した基準画像と、生成指示への反映。"""

    def test_registered_reference_files_are_valid_images(self):
        for group in ("characters", "locations"):
            for item in self.series.config[group].values():
                for r in item.get("reference_images", []):
                    media.inspect_image(self.series.resolve(r["path"]))

    def test_prompts_require_single_instance_and_reference_notes(self):
        prod, _ = media.current_production(self.series, 1)
        for cut in prod["cuts"]:
            img = cut["image"]["prompt"]
            self.assertIn("exactly one Poco in the image", img)
            self.assertNotIn("Mochi", img)  # 開始画像にモチはいない
            self.assertIn("ONE single Poco", img)
            self.assertIn("vending corner reference", img)
        vid = prod["cuts"][4]["video"]["prompt"]
        self.assertIn("exactly one Poco and exactly one Mochi at all times", vid)
        self.assertIn("ONE single Mochi", vid)
        self.assertEqual({r["subject"] for r in prod["cuts"][4]["video"]["references"]},
                         {"poco", "mochi", "vending-corner"})

    def test_unreadable_reference_blocks(self):
        ref = self.series.resolve("references/characters/poco/poco_reference.png")
        ref.write_bytes(ref.read_bytes()[:1000])
        self.assertTrue(all(p.action == "blocked" for p in self.plans().values()))


class TestReplaceKeepsMedia(MediaTestCase):
    def test_replace_keeps_images_records_and_legacy_assets(self):
        media.run_images(self.series, 1, FakeGen())
        legacy = self.series.episode_dir(1) / "assets"
        legacy.mkdir()
        (legacy / "cut01_start.png").write_bytes(png_bytes())
        before_files = self.image_files()
        before_records = self.records()

        ep = read_json(self.series.episode_dir(1) / "episode.json")
        ep["cuts"][1]["image_prompt"] += " Slightly warmer light."
        plan, _ = commit(self.series, ep, replace=True)
        self.assertEqual(plan.action, "replace")

        self.assertEqual(self.image_files(), before_files)
        self.assertEqual(self.records(), before_records)
        self.assertTrue((legacy / "cut01_start.png").is_file())
        plans = self.plans()
        self.assertEqual(plans[2].action, "update")
        self.assertEqual(len(plans[2].previous), 1)
        self.assertEqual([plans[i].action for i in (1, 3, 4, 5)], ["reuse"] * 4)


class TestLegacyData(MediaTestCase):
    def test_old_production_and_series_without_new_fields(self):
        def strip(c):
            c.pop("generation", None)
            c.pop("asset_storage", None)
            for group in ("characters", "locations"):
                for item in c[group].values():
                    item.pop("reference_images", None)
        self.set_config(strip)
        old = read_json(self.series.episode_dir(1) / "production.json")
        old.pop("schema_version", None)
        for cut in old["cuts"]:
            cut["image"] = {"prompt": cut["image"]["prompt"], "output": f"assets/cut{cut['cut_number']:02d}_start.png"}
        (self.series.episode_dir(1) / "production.json").write_text(json.dumps(old), encoding="utf-8")
        ep = read_json(self.series.episode_dir(1) / "episode.json")
        for cut in ep["cuts"]:
            cut.pop("characters_in_start_image", None)
        (self.series.episode_dir(1) / "episode.json").write_text(json.dumps(ep), encoding="utf-8")

        plans, notes = media.plan_images(self.series, 1)
        self.assertEqual(len(plans), 5)
        self.assertTrue(any("旧形式" in n for n in notes))
        self.assertTrue(all(p.references == [] and p.action == "generate" for p in plans))
        self.assertIsNone(media.model_descriptor(self.series.config.get("generation", {}).get("image"))["provider"])


class TestImagesCli(MediaTestCase):
    def run_cli(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["--series", str(self.root), "images", *args])
        return code, buf.getvalue()

    def test_dry_run_and_unconnected_run(self):
        self.set_config(lambda c: c["generation"]["image"].update(provider=None, model=None))
        code, out = self.run_cli("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("未接続", out)
        self.assertIn("保存完了ではありません", out)
        self.assertIn("media/ep001/images/cut01_start-", out)
        code, out = self.run_cli()
        self.assertEqual(code, 1)
        self.assertEqual(self.image_files(), [])

    def test_unknown_provider_is_error(self):
        self.set_config(lambda c: c["generation"]["image"].update(provider="no-such-service"))
        code, _ = self.run_cli("--dry-run")
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
