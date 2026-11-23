"""Google ドライブ保存先と、生成済み画像の参照（continuity_references）のテスト。通信なし・費用なし。"""
from __future__ import annotations

import copy
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

from storyengine import media
from storyengine.commit import plan_commit
from storyengine.google_drive import DriveError, GoogleDriveStore
from storyengine.integrations import create_asset_store
from storyengine.series import load_series
from storyengine.storage import read_json
from tests.test_media import SERIES_SRC, FakeGen

DCFG = {"provider": "google-drive", "params": {"folder_name": "mvm-test", "min_free_mb": 1}}
ENV = {"GOOGLE_DRIVE_REFRESH_TOKEN": "refresh-test"}


class Resp(io.BytesIO):
    def __init__(self, body=b"", headers=None):
        super().__init__(body)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeDrive:
    """Drive API v3 とトークン発行の代わり（必要な分だけ）。"""

    def __init__(self, limit=15 * 1024 ** 3, usage=0, corrupt=False, token_status=None):
        self.files: dict[str, dict] = {}
        self.requests = []
        self.limit, self.usage, self.corrupt, self.token_status = limit, usage, corrupt, token_status
        self._n = 0
        self.sessions: dict[str, dict] = {}

    def _id(self):
        self._n += 1
        return f"id{self._n}"

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        url, method = req.full_url, req.get_method()
        u = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(u.query)
        if url.startswith("https://oauth2.googleapis.com/token"):
            if self.token_status:
                raise urllib.error.HTTPError(url, self.token_status, "x", {}, io.BytesIO(
                    json.dumps({"error": "invalid_grant", "error_description": "expired"}).encode()))
            return Resp(json.dumps({"access_token": "at-123", "expires_in": 3599}).encode())
        assert req.get_header("Authorization") == "Bearer at-123", req.get_header("Authorization")
        if u.path.endswith("/about"):
            return Resp(json.dumps({"storageQuota": {"limit": str(self.limit), "usage": str(self.usage)}}).encode())
        if u.path == "/drive/v3/files" and method == "GET":
            q = qs["q"][0]
            out = []
            for fid, f in self.files.items():
                if f"name = '{f['name']}'" in q and (f"'{f.get('parent')}' in parents" in q or "in parents" not in q):
                    out.append({"id": fid, "name": f["name"], "appProperties": f.get("appProperties", {})})
            return Resp(json.dumps({"files": out}).encode())
        if u.path == "/drive/v3/files" and method == "POST":  # フォルダー作成
            meta = json.loads(req.data)
            fid = self._id()
            self.files[fid] = {"name": meta["name"], "mime": meta["mimeType"]}
            return Resp(json.dumps({"id": fid}).encode())
        if u.path == "/upload/drive/v3/files" and method == "POST":
            meta = json.loads(req.data)
            sid = self._id()
            self.sessions[sid] = meta
            return Resp(b"", {"Location": f"https://www.googleapis.com/upload/drive/v3/files?upload_id={sid}"})
        if u.path == "/upload/drive/v3/files" and method == "PUT":
            meta = self.sessions.pop(qs["upload_id"][0])
            fid = self._id()
            self.files[fid] = {"name": meta["name"], "parent": meta["parents"][0],
                               "appProperties": meta.get("appProperties", {}), "data": req.data}
            return Resp(json.dumps({"id": fid}).encode())
        if u.path.startswith("/drive/v3/files/") and qs.get("alt") == ["media"]:
            data = self.files[u.path.rsplit("/", 1)[1]]["data"]
            return Resp(data[:-1] if self.corrupt else data)
        raise AssertionError(f"unexpected request {method} {url}")


class FakeDrive401(FakeDrive):
    def __call__(self, req, timeout=None):
        if req.get_header("Authorization") != "Bearer at-123" and "oauth2" not in req.full_url:
            raise urllib.error.HTTPError(req.full_url, 401, "x", {}, io.BytesIO(b'{"error":{"message":"Invalid Credentials"}}'))
        return super().__call__(req, timeout)


class TestDriveStore(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.file = self.tmp / "a.png"
        self.file.write_bytes(b"\x89PNG fake bytes" * 100)
        self.env = mock.patch.dict(os.environ, ENV)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_upload_restore_verify_and_no_duplicates(self):
        fake = FakeDrive()
        store = GoogleDriveStore(DCFG, opener=fake)
        uri = store.upload(self.file, "midnight-vending-machine/media/ep001/images/a.png")
        self.assertTrue(uri.startswith("gdrive://"))
        tok = fake.requests[0]
        self.assertEqual(tok.full_url, "https://oauth2.googleapis.com/token")
        self.assertTrue(tok.get_header("Authorization").startswith("Basic "))  # 通信経路が置き換える仮の値
        self.assertIn(b"refresh_token=refresh-test", tok.data)
        self.assertNotIn(b"client_secret", tok.data)
        dest = self.tmp / "restored.png"
        store.download(uri, dest)
        self.assertEqual(dest.read_bytes(), self.file.read_bytes())
        before = len(fake.files)
        self.assertEqual(GoogleDriveStore(DCFG, opener=fake).upload(self.file, "midnight-vending-machine/media/ep001/images/a.png"), uri)
        self.assertEqual(len(fake.files), before)  # 同じ内容は再アップロードしない
        self.assertEqual(sum(1 for r in fake.requests if r.full_url.startswith("https://oauth2")), 2)  # 店ごとに1回だけ

    def test_capacity_shortage_blocks_upload(self):
        fake = FakeDrive(limit=10 * 1024 ** 2, usage=10 * 1024 ** 2 - 1000)
        with self.assertRaises(DriveError) as cm:
            GoogleDriveStore(DCFG, opener=fake).upload(self.file, "k/a.png")
        self.assertIn("空き容量が不足", str(cm.exception))
        self.assertFalse(any("/upload/" in r.full_url for r in fake.requests))

    def test_restore_mismatch_is_error(self):
        with self.assertRaises(DriveError):
            GoogleDriveStore(DCFG, opener=FakeDrive(corrupt=True)).upload(self.file, "k/a.png")

    def test_injected_bearer_mode_without_refresh_token(self):
        class InjectingDrive(FakeDrive):  # 通信経路が Authorization を置き換えるのを模擬
            def __call__(self, req, timeout=None):
                self.seen = req.get_header("Authorization")
                if req.full_url.startswith("https://www.googleapis.com/"):
                    req.headers["Authorization"] = "Bearer at-123"
                return super().__call__(req, timeout)
        fake = InjectingDrive()
        with mock.patch.dict(os.environ, {}, clear=True):
            store = GoogleDriveStore(DCFG, opener=fake)
            uri = store.upload(self.file, "k/a.png")
        self.assertTrue(uri.startswith("gdrive://"))
        self.assertEqual(fake.seen, "Bearer injected-by-environment-proxy")
        self.assertFalse(any(r.full_url.startswith("https://oauth2") for r in fake.requests))  # トークン発行に行かない

    def test_expired_grant(self):
        with self.assertRaises(DriveError) as cm:
            GoogleDriveStore(DCFG, opener=FakeDrive(token_status=400)).quota()
        self.assertIn("HTTP 400", str(cm.exception))
        self.assertNotIn("refresh-test", str(cm.exception))

    def test_registered(self):
        self.assertIsInstance(create_asset_store(DCFG), GoogleDriveStore)


class SeriesCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "series"
        shutil.copytree(SERIES_SRC, self.root, ignore=shutil.ignore_patterns("drafts", "media", ".lock"))
        cfg = read_json(self.root / "series.json")
        cfg["generation"]["image"] = {"provider": "test", "model": "model-a", "params": {}}
        (self.root / "series.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        self.series = load_series(self.root)
        self.env = mock.patch.dict(os.environ, ENV)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def plans(self, store=None):
        return {p.cut_number: p for p in media.plan_images(self.series, 1, store=store)[0]}


class TestDriveWithPipeline(SeriesCase):
    def test_generate_persist_and_restore_after_environment_loss(self):
        fake = FakeDrive()
        store = GoogleDriveStore(DCFG, opener=fake)
        media.run_images(self.series, 1, FakeGen(), store=store, cuts=[1])
        self.assertTrue(self.plans(store)[1].persisted)
        shutil.rmtree(self.root / "media" / "ep001" / "images")  # クラウド環境の終了を想定
        self.assertEqual(self.plans(store)[1].action, "restore")
        gen = FakeGen()
        media.run_images(self.series, 1, gen, store=store, cuts=[1])
        self.assertEqual(gen.calls, [])  # 有料生成をせずに復元
        self.assertEqual(self.plans(store)[1].action, "reuse")

    def test_capacity_shortage_stops_paid_generation(self):
        store = GoogleDriveStore(DCFG, opener=FakeDrive(limit=10 * 1024 ** 2, usage=10 * 1024 ** 2))
        gen = FakeGen()
        res = media.run_images(self.series, 1, gen, store=store, cuts=[1])
        self.assertEqual(gen.calls, [])
        self.assertIn("空き容量が不足", res.failed[0][1])

    def test_unreachable_store_stops_paid_generation(self):
        with mock.patch.dict(os.environ, {}, clear=True):  # アクセストークン未登録（通信経路が置き換えない）→ 401
            gen = FakeGen()
            res = media.run_images(self.series, 1, gen, store=GoogleDriveStore(DCFG, opener=FakeDrive401()), cuts=[1])
        self.assertEqual(gen.calls, [])
        self.assertIn("確認できません", res.failed[0][1])

    def test_persist_all_versions_once(self):
        media.run_images(self.series, 1, FakeGen(), cuts=[1])  # 保存先なしで生成
        store = GoogleDriveStore(DCFG, opener=FakeDrive())
        res = media.persist_all(self.series, store, [1])
        self.assertEqual([r[1] for r in res], ["persisted"])
        self.assertEqual(media.persist_all(self.series, store, [1]), [])

    def test_failed_restore_check_is_not_persisted(self):
        media.run_images(self.series, 1, FakeGen(), cuts=[1])
        res = media.persist_all(self.series, GoogleDriveStore(DCFG, opener=FakeDrive(corrupt=True)), [1])
        self.assertEqual(res[0][1], "upload_failed")
        self.assertFalse(self.plans()[1].persisted)


class TestRestoreAndImport(SeriesCase):
    def test_restore_command_never_generates(self):
        store = GoogleDriveStore(DCFG, opener=FakeDrive())
        media.run_images(self.series, 1, FakeGen(), store=store, cuts=[1])
        self.assertEqual([r[1] for r in media.restore_all(self.series, store, [1], check_all=True)], ["verified"])
        shutil.rmtree(self.root / "media" / "ep001" / "images")
        self.assertEqual([r[1] for r in media.restore_all(self.series, store, [1])], ["restored"])
        self.assertEqual(self.plans(store)[1].action, "reuse")

    def test_import_zip_accepts_only_recorded_content(self):
        import zipfile
        media.run_images(self.series, 1, FakeGen(), cuts=[1])
        f = self.root / self.plans()[1].current["file"]
        zp = self.tmp / "imgs.zip"
        with zipfile.ZipFile(zp, "w") as z:
            z.writestr("ep001/" + f.name, f.read_bytes())
            z.writestr("ep001/evil.png", b"\x89PNG not recorded")
        f.unlink()
        res = dict(media.import_zip(self.series, zp, [1]))
        self.assertEqual(res[self.plans()[1].current["file"]], "imported")
        self.assertTrue(res["ep001/evil.png"].startswith("skipped"))
        self.assertEqual(self.plans()[1].action, "reuse")


class TestContinuityReferences(SeriesCase):
    """第1話のカット2以降は、前のカットの採用画像を参照する。"""

    def test_cut2_waits_for_cut1_and_uses_it(self):
        self.assertEqual(self.plans()[2].action, "blocked")
        captured = []

        class Capture(FakeGen):
            def generate(self, spec, out_path):
                captured.append((spec["slot"], len(spec["references"])))
                return super().generate(spec, out_path)

        media.run_images(self.series, 1, Capture(), cuts=[1, 2])  # 同じ実行でカット1→カット2
        self.assertEqual(captured, [("cut01_start", 2), ("cut02_start", 3)])
        p2 = self.plans()[2]
        gen_ref = [r for r in p2.references if r.get("kind") == "generated"][0]
        self.assertEqual(gen_ref["path"], self.plans()[1].current["file"])

    def test_new_cut1_image_marks_cut2_for_update(self):
        media.run_images(self.series, 1, FakeGen(), cuts=[1, 2])
        cfg = read_json(self.root / "series.json")
        cfg["style"]["negative_prompt"] += ", fog"
        (self.root / "series.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        self.series = load_series(self.root)
        self.assertEqual((self.plans()[1].action, self.plans()[2].action), ("update", "blocked"))
        media.run_images(self.series, 1, FakeGen(), cuts=[1])
        self.assertEqual(self.plans()[2].action, "update")

    def test_validation_requires_earlier_cut(self):
        ep = copy.deepcopy(read_json(self.series.episode_dir(1) / "episode.json"))
        ep["cuts"][1]["continuity_references"] = [{"slot": "cut02_start"}]
        errs = plan_commit(self.series, ep, replace=True).errors
        self.assertTrue(any("continuity_references" in e for e in errs))


if __name__ == "__main__":
    unittest.main()
