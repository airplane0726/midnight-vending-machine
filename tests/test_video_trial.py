"""動画の試作（Runway・技術確認）のテスト。通信は差し替え、費用なし。"""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path

from storyengine import media, video_trial as vt
from storyengine.runway_video import RunwayVideoGenerator, RunwayError, estimate_usd
from storyengine.series import load_series
from storyengine.state import load_episodes
from storyengine.storage import read_json
from tests.test_media import SERIES_SRC, FakeGen


def tiny_mp4(path: Path) -> bytes:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=72x128:d=1", "-pix_fmt", "yuv420p",
                    str(path)], check=True)
    return path.read_bytes()


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeRunway:
    def __init__(self, video: bytes, final="SUCCEEDED", polls=2):
        self.video, self.final, self.polls = video, final, polls
        self.requests = []
        self.n = 0

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        url = req.full_url
        if url.endswith("/v1/image_to_video"):
            return Resp(json.dumps({"id": "task-1"}).encode())
        if "/v1/tasks/" in url:
            self.n += 1
            status = self.final if self.n >= self.polls else "RUNNING"
            return Resp(json.dumps({"id": "task-1", "status": status, "output": ["https://cdn.example/out.mp4"],
                                    "failure": "bad"}).encode())
        if url == "https://cdn.example/out.mp4":
            return Resp(self.video)
        raise AssertionError(url)


CFG = {"provider": "runway", "model": "gen4_turbo", "params": {"max_cost_usd": 0.25}}


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg が必要")
class TestVideoTrial(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "series"
        shutil.copytree(SERIES_SRC, self.root, ignore=shutil.ignore_patterns("drafts", "media", ".lock"))
        cfg = read_json(self.root / "series.json")
        cfg["generation"]["image"] = {"provider": "test", "model": "m", "params": {}}
        (self.root / "series.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        self.series = load_series(self.root)
        media.run_images(self.series, 1, FakeGen())
        self.video = tiny_mp4(self.tmp / "v.mp4")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def gen(self, **kw):
        return RunwayVideoGenerator(CFG, opener=FakeRunway(self.video, **kw), sleep=lambda s: None, poll_interval=1)

    def test_estimate_and_cap(self):
        self.assertAlmostEqual(estimate_usd("gen4_turbo", 5), 0.25)
        self.assertAlmostEqual(estimate_usd("gen4_turbo", 1), 0.10)  # 最低10クレジット
        g = RunwayVideoGenerator({**CFG, "params": {"max_cost_usd": 0.20}}, opener=FakeRunway(self.video))
        with self.assertRaises(RunwayError):
            g.build_request({"prompt": "p", "duration_sec": 5}, self.tmp / "x.png")

    def test_request_shape_uses_adopted_image_and_episode_prompt(self):
        g = self.gen()
        res = vt.run(self.series, 1, 4, g, store=None)
        self.assertEqual(res["action"], "generated")
        body = json.loads(g._opener.requests[0].data)
        self.assertEqual((body["model"], body["ratio"], body["duration"]), ("gen4_turbo", "720:1280", 5))
        cut4 = next(c for c in load_episodes(self.series)[0]["cuts"] if c["cut_number"] == 4)
        self.assertEqual(body["promptText"], cut4["video_trial"]["prompt"])
        self.assertIn("no mouth", body["promptText"])
        self.assertIn("both wooden arms", body["promptText"])
        self.assertNotIn("180", body["promptText"])
        self.assertTrue(body["promptImage"][0]["uri"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(body["promptImage"][0]["position"], "first")
        self.assertEqual(g._opener.requests[0].get_header("X-runway-version"), "2024-11-06")
        v = res["version"]
        self.assertEqual((v["status"], v["cost_usd"], v["task"]["id"]), ("complete", 0.25, "task-1"))
        self.assertEqual(vt.run(self.series, 1, 4, self.gen(), store=None)["action"], "reuse")

    def test_failure_is_recorded_and_never_resubmitted(self):
        g = self.gen(final="FAILED")
        with self.assertRaises(vt.TrialError):
            vt.run(self.series, 1, 4, g, store=None)
        self.assertEqual(sum(1 for r in g._opener.requests if r.full_url.endswith("/image_to_video")), 1)
        v = read_json(self.series.media_records_path(1))["slots"]["cut04_video_trial"]["versions"][-1]
        self.assertEqual((v["status"], v["task"]["id"]), ("failed", "task-1"))
        g2 = self.gen()
        with self.assertRaises(vt.TrialError):  # 明示の指示がなければ二度目は送らない
            vt.run(self.series, 1, 4, g2, store=None)
        self.assertEqual(g2._opener.requests, [])
        self.assertEqual(vt.run(self.series, 1, 4, g2, store=None, again=True)["action"], "generated")

    def test_download_failure_then_refetch_same_task_without_new_request(self):
        class DownloadBlocked(FakeRunway):
            def __call__(self, req, timeout=None):
                if req.full_url == "https://cdn.example/out.mp4":
                    raise urllib.error.URLError("blocked")
                return super().__call__(req, timeout)
        g = RunwayVideoGenerator(CFG, opener=DownloadBlocked(self.video), sleep=lambda s: None, poll_interval=1)
        with self.assertRaises(vt.TrialError):
            vt.run(self.series, 1, 4, g, store=None)
        g2 = self.gen()
        res = vt.run(self.series, 1, 4, g2, store=None, resume_task="task-1")
        self.assertEqual(res["action"], "refetched")
        self.assertEqual(res["version"]["cost_usd"], 0.0)
        self.assertFalse(any(r.full_url.endswith("/image_to_video") for r in g2._opener.requests))
        with self.assertRaises(vt.TrialError):
            vt._refetch(self.series, 1, vt.plan(self.series, 1, 4, g2), g2, None, "unknown-task")

    def test_review_marks_not_adopted_without_touching_cost(self):
        res = vt.run(self.series, 1, 4, self.gen(), store=None)
        sha = res["version"]["sha256"]
        with self.assertRaises(vt.TrialError):
            vt.review(self.series, 1, 4, sha[:12], {"adopted": False})
        v = vt.review(self.series, 1, 4, sha[:12], {"adopted": False, "use": "技術確認用", "checks": [{"item": "口なし", "ok": False}]})
        self.assertFalse(v["review"]["adopted"])
        self.assertEqual(v["cost_usd"], res["version"]["cost_usd"])
        with self.assertRaises(vt.TrialError):
            vt.review(self.series, 1, 4, "ffff", {"adopted": True, "use": "x", "checks": []})

    def test_timeout_does_not_resubmit(self):
        g = RunwayVideoGenerator(CFG, opener=FakeRunway(self.video, polls=999), sleep=lambda s: None,
                                 poll_interval=1, max_wait_sec=3)
        with self.assertRaises(vt.TrialError):
            vt.run(self.series, 1, 4, g, store=None)
        self.assertEqual(sum(1 for r in g._opener.requests if r.full_url.endswith("/image_to_video")), 1)

    def test_requires_trial_spec_and_adopted_image(self):
        with self.assertRaises(vt.TrialError):
            vt.plan(self.series, 1, 3, None)  # カット3には video_trial がない

    def test_http_error_message(self):
        class Err:
            def __call__(self, req, timeout=None):
                raise urllib.error.HTTPError(req.full_url, 402, "x", {}, io.BytesIO(b'{"error":"insufficient credits"}'))
        g = RunwayVideoGenerator(CFG, opener=Err())
        with self.assertRaises(vt.TrialError) as cm:
            vt.run(self.series, 1, 4, g, store=None)
        self.assertIn("残高不足", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
