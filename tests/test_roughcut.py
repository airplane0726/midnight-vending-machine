from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

from storyengine import roughcut, soundtrack
from storyengine.series import load_series
from tests.test_media import SERIES_SRC


def png(path: Path, color: str, size: str = "64x112") -> str:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color={color}:s={size}", "-frames:v", "1",
                    str(path)], check=True)
    return path.name


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg が必要")
class TestRoughcut(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "series"
        shutil.copytree(SERIES_SRC, self.root, ignore=shutil.ignore_patterns("drafts", "media", ".lock"))
        self.series = load_series(self.root)
        png(self.root / "a.png", "0x204060")
        png(self.root / "b.png", "0x806040")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def spec(self):
        return {"output": {"width": 64, "height": 112, "fps": 10},
                "assets": {"m": {"op": "mask", "size": [64, 112], "shapes": [{"ellipse": [32, 56, 10, 20]}], "grow": 2},
                           "plate": {"op": "fill", "src": "a.png", "mask": "@m", "sigma": 5,
                                     "patches": [{"from": [0, 0, 10, 10], "to": [5, 5], "feather": 3}]},
                           "cut": {"op": "cutout", "src": "b.png", "mask": "@m"},
                           "blank": {"op": "blank", "size": [20, 20]},
                           "rain": {"op": "rain", "size": [16, 16], "seed": 2},
                           "rb": {"op": "rainbow", "radius": 10, "thickness": 6},
                           "g": {"op": "glow", "radius": 8, "ry": 4},
                           "c": {"op": "crop", "src": "b.png", "rect": [0, 0, 20, 20], "vflip": True, "height": 10,
                                 "tint": [1, 0.9, 0.8]}},
                "shots": [{"duration": 1.0, "base": "a.png", "camera": [[0, 1.0, 0.5, 0.5], [1.0, 1.1, 0.5, 0.5]],
                           "layers": [{"asset": "g", "anchor": "center", "x": 30, "y": 30, "fade_in": [0.2, 0.3]}],
                           "transition": {"type": "fade", "duration": 0.2}},
                          {"duration": 1.0, "base": "@plate", "shake": [{"at": 0.2, "dur": 0.2, "amp": 2}],
                           "layers": [{"asset": "cut", "x": 0, "y": [[0, 0], [1, -10]]},
                                      {"asset": "blank", "anchor": "center", "x": 30, "y": 50, "angle": 30,
                                       "clip": [20, 40, 20, 20], "rain": {"asset": "rain", "dir": "up", "inset": 2}},
                                      {"asset": "rb", "x": 20, "y": 90}, {"asset": "c", "x": 5, "y": 5}],
                           "transition": {"type": "cut"}},
                          {"duration": 0.5, "base": "b.png"}],
                "sound": {"ambience": [{"type": "hum", "end": 2.5}, {"type": "rain", "start": 0.5, "end": 1.5}],
                          "events": [{"t": 0.1, "type": "bell", "note": "A5"}, {"t": 1.0, "type": "thump"}],
                          "music": {"notes": [[0.2, "musicbox", "C6", 0.5]]}}}

    def test_build_records_and_reuses_without_cost(self):
        res = roughcut.build(self.series, 1, self.spec())
        v = res["version"]
        self.assertEqual(res["action"], "built")
        self.assertEqual((v["width"], v["height"], v["cost_usd"]), (64, 112, 0.0))
        self.assertAlmostEqual(v["duration"], 2.5, delta=0.15)
        self.assertIn("a.png", v["conditions"]["inputs"])
        self.assertEqual(roughcut.build(self.series, 1, self.spec())["action"], "reuse")
        streams = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0",
                                  str(self.series.resolve(v["file"]))], capture_output=True, text=True).stdout.split()
        self.assertEqual(sorted(streams), ["audio", "video"])

    def test_unknown_asset_is_an_error(self):
        sp = self.spec()
        sp["shots"][0]["layers"][0]["asset"] = "nothing"
        with self.assertRaises(roughcut.RoughcutError):
            roughcut.build(self.series, 1, sp)


class TestSoundtrack(unittest.TestCase):
    def test_note_and_render(self):
        self.assertAlmostEqual(soundtrack.note_freq("A4"), 440.0)
        self.assertAlmostEqual(soundtrack.note_freq("A5"), 880.0)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.wav"
            soundtrack.render({"events": [{"t": 0.0, "type": t} for t in
                                          ("bell", "knock", "click", "rattle", "squeak", "beep", "whoosh", "pop")],
                               "ambience": [{"type": "rain_rise", "end": 0.5}]}, p, duration=1.0)
            with wave.open(str(p)) as w:
                self.assertEqual((w.getnchannels(), w.getframerate()), (2, 44100))
                self.assertGreater(w.getnframes(), 44000)


if __name__ == "__main__":
    unittest.main()
