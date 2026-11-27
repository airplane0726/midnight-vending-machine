from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import tempfile
import unittest
import urllib.parse
from pathlib import Path

from storyengine import instagram, publish, roughcut
from storyengine.finalize import export
from storyengine.media import load_records, save_records
from storyengine.series import load_series
from tests.test_media import SERIES_SRC

ROOT = Path(__file__).resolve().parents[1]


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeGraph:
    """Instagram API の模擬。送られた依頼を記録し、決めた応答を返す。"""

    def __init__(self, statuses=("IN_PROGRESS", "FINISHED")):
        self.statuses = list(statuses)
        self.requests = []

    def __call__(self, req, timeout=None):
        u = urllib.parse.urlparse(req.full_url)
        path = u.path.split("/", 2)[2]
        body = dict(urllib.parse.parse_qsl(req.data.decode())) if req.data else {}
        self.requests.append((req.get_method(), path, body, req.get_header("Authorization")))
        if path == "me":
            out = {"user_id": "17841", "username": "moyrr_s", "account_type": "MEDIA_CREATOR"}
        elif path.endswith("content_publishing_limit"):
            out = {"data": [{"quota_usage": 0, "config": {"quota_total": 50}}]}
        elif path.endswith("/media") and req.get_method() == "POST":
            out = {"id": "c-1"}
        elif path == "c-1":
            out = {"status_code": self.statuses.pop(0) if self.statuses else "FINISHED"}
        elif path.endswith("media_publish"):
            out = {"id": "m-9"}
        elif path == "m-9":
            out = {"permalink": "https://www.instagram.com/reel/TEST/"}
        else:
            out = {}
        return FakeResp(json.dumps(out).encode())

    def posts(self):
        return [(m, p) for m, p, _, _ in self.requests if m == "POST"]


class FakeStore:
    def __init__(self, files: dict[str, bytes]):
        self.files, self.shared, self.unshared = files, [], []

    def download(self, uri, dest):
        Path(dest).write_bytes(self.files[uri])
        return Path(dest)

    def share_public(self, uri):
        self.shared.append(uri)
        return f"perm-{len(self.shared)}", f"https://drive.example/{uri[-6:]}"

    def unshare(self, uri, perm):
        self.unshared.append((uri, perm))


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg が必要")
class TestInstagram(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        shutil.copytree(ROOT / "brand", self.tmp / "brand", ignore=shutil.ignore_patterns("out"))
        b = json.loads((self.tmp / "brand" / "brand.json").read_text(encoding="utf-8"))
        b["logo"]["font_size"] = 10
        (self.tmp / "brand" / "brand.json").write_text(json.dumps(b, ensure_ascii=False), encoding="utf-8")
        self.root = self.tmp / "series" / "s"
        shutil.copytree(SERIES_SRC, self.root, ignore=shutil.ignore_patterns("drafts", "media", ".lock", "ledger.json"))
        shutil.rmtree(self.root / "publish" / "ep001", ignore_errors=True)
        real = json.loads((SERIES_SRC / "edits" / "ep001_publish.json").read_text(encoding="utf-8"))
        (self.root / "edits" / "ep001_publish.json").write_text(
            json.dumps({"cover": None, "stills": [], "en": real["en"]}, ensure_ascii=False), encoding="utf-8")
        (self.root / "edits" / "ep001_covers.json").unlink(missing_ok=True)
        self.series = load_series(self.root)
        self.series.config["format"] = dict(self.series.config["format"], resolution="108x192", total_seconds=2)
        self.series.config["publish_export"] = {"loudness": [-30, -5]}
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=0x405060:s=108x192",
                        "-frames:v", "1", str(self.root / "a.png")], check=True)
        roughcut.build(self.series, 1, {"output": {"width": 108, "height": 192, "fps": 30},
                                        "shots": [{"duration": 2.0, "base": "a.png"}],
                                        "sound": {"ambience": [{"type": "hum", "end": 2.0, "gain_db": -20}]}})
        export(self.series, 1)
        man = publish.build_package(self.series, 1)
        self.post_rel, self.sha = man["video"]["post"]["file"], man["video"]["post"]["sha256"]
        # 保存済みとして記録（模擬の保存先）
        rec = load_records(self.series, 1)
        for slot in rec["slots"].values():
            for v in slot["versions"]:
                v["storage"] = {"status": "persisted", "uri": f"gdrive://{Path(v['file']).name}"}
        save_records(self.series, 1, rec)
        self.uri = f"gdrive://{Path(self.post_rel).name}"
        self.store = FakeStore({self.uri: self.series.resolve(self.post_rel).read_bytes()})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def approve(self):
        e = publish.request_review(self.series, 1)
        publish.approve(self.series, 1, e["post_sha256"][:12])

    def client(self, **kw):
        g = FakeGraph(**kw)
        return instagram.InstagramClient("TOKEN-SECRET", "17841", opener=g), g

    def run_ig(self, mode, client, sha=None, logs=None):
        return instagram.run(self.series, 1, mode=mode, expected_sha=sha or self.sha[:16], client=client,
                             store=self.store, request_id="ab" * 16, log=(logs.append if logs is not None else lambda s: None),
                             sleep=lambda s: None, poll_interval=1, max_wait=5)

    def test_refuses_unapproved_and_wrong_sha_without_sending(self):
        c, g = self.client()
        with self.assertRaises(publish.PublishError):
            self.run_ig("publish", c)
        self.approve()
        with self.assertRaises(instagram.InstagramError):
            self.run_ig("publish", c, sha="0" * 16)
        self.assertEqual(g.requests, [])
        self.assertEqual(self.store.shared, [])

    def test_check_mode_sends_nothing_and_never_logs_token(self):
        self.approve()
        c, g = self.client()
        logs = []
        res = self.run_ig("check", c, logs=logs)
        self.assertEqual(res["status"], "checked")
        self.assertEqual(g.posts(), [])
        self.assertEqual(self.store.shared, [])
        self.assertNotIn("TOKEN-SECRET", "\n".join(logs))
        self.assertTrue(all(h == "Bearer TOKEN-SECRET" for *_, h in g.requests))

    def test_container_mode_does_not_publish_and_unshares(self):
        self.approve()
        c, g = self.client()
        res = self.run_ig("container", c)
        self.assertEqual(res["status"], "container_ready")
        self.assertNotIn("media_publish", " ".join(p for _, p in g.posts()))
        reel = next(b for m, p, b, _ in g.requests if p.endswith("/media"))
        self.assertEqual((reel["media_type"], reel["share_to_feed"]), ("REELS", "true"))
        self.assertIn("Made from AI-generated images", reel["caption"])
        self.assertEqual(len(self.store.unshared), len(self.store.shared))

    def test_publish_then_record_once(self):
        self.approve()
        c, g = self.client()
        logs = []
        res = self.run_ig("publish", c, logs=logs)
        self.assertEqual((res["status"], res["media_id"]), ("published", "m-9"))
        self.assertEqual(len(self.store.unshared), len(self.store.shared))
        log = self.tmp / "job.log"
        log.write_text("2026-01-01T00:00:00Z " + "\n".join(logs), encoding="utf-8")
        e = instagram.record_from_log(self.series, 1, log, "ab" * 16)
        self.assertEqual(e["posts"]["instagram"]["url"], "https://www.instagram.com/reel/TEST/")
        with self.assertRaises(publish.PublishError):  # 二重記録・二重投稿の防止
            instagram.record_from_log(self.series, 1, log, "ab" * 16)
        c2, g2 = self.client()
        with self.assertRaises(publish.PublishError):
            self.run_ig("publish", c2)
        self.assertEqual(g2.requests, [])

    def test_container_error_stops_without_retry_and_unshares(self):
        self.approve()
        c, g = self.client(statuses=("IN_PROGRESS", "ERROR"))
        with self.assertRaises(instagram.InstagramError):
            self.run_ig("publish", c)
        self.assertEqual([p for _, p in g.posts()].count("17841/media"), 1)  # 作り直さない
        self.assertNotIn("17841/media_publish", [p for _, p in g.posts()])
        self.assertEqual(len(self.store.unshared), len(self.store.shared))

    def test_drive_copy_must_match_and_restores_missing_local(self):
        self.approve()
        self.series.resolve(self.post_rel).unlink()  # Actions には素材がない
        c, g = self.client()
        self.run_ig("check", c)
        self.assertTrue(self.series.resolve(self.post_rel).exists())
        self.store.files[self.uri] = b"other"
        c2, g2 = self.client()
        with self.assertRaises(instagram.InstagramError):
            self.run_ig("publish", c2)
        self.assertEqual(g2.requests, [])


class TestWorkflowStatic(unittest.TestCase):
    def test_workflow_is_minimal(self):
        y = (ROOT / ".github" / "workflows" / "instagram-publish.yml").read_text(encoding="utf-8")
        self.assertNotRegex(y, r"(?m)^\s*-?\s*uses:")
        self.assertIn("contents: read", y)
        self.assertNotRegex(y, r"contents:\s*write")
        self.assertIn("options: [check, container, publish]", y)
        self.assertIn("default: check", y)
        # 入力やシークレットを run の命令文へ直接埋め込まない
        for block in re.findall(r"run: \|\n((?:\s{10}.*\n?)+)", y):
            self.assertNotIn("${{", block)


class TestDriveShare(unittest.TestCase):
    def test_share_and_unshare_requests(self):
        import time
        from storyengine import google_drive as gd
        seen = []

        def opener(req, timeout=None):
            seen.append((req.get_method(), req.full_url, json.loads(req.data) if req.data else None))
            return FakeResp(json.dumps({"id": "perm-1"}).encode() if req.get_method() == "POST" else b"")
        gd.set_session_token("t", time.time() + 600)
        try:
            s = gd.GoogleDriveStore({"params": {}}, opener=opener)
            perm, url = s.share_public("gdrive://FILE1")
            s.unshare("gdrive://FILE1", perm)
        finally:
            gd.clear_session_token()
        self.assertEqual(perm, "perm-1")
        self.assertIn("id=FILE1", url)
        self.assertEqual(seen[0][0], "POST")
        self.assertEqual(seen[0][2], {"type": "anyone", "role": "reader", "allowFileDiscovery": False})
        self.assertEqual((seen[1][0], seen[1][1].endswith("/files/FILE1/permissions/perm-1")), ("DELETE", True))


if __name__ == "__main__":
    unittest.main()
