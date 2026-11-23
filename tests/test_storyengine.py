"""storyengine のテスト。実行: python -m unittest discover -s tests -v"""
from __future__ import annotations

import copy
import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from storyengine import commit as commit_mod
from storyengine.cli import main
from storyengine.commit import commit, plan_commit
from storyengine.series import load_series
from storyengine.state import load_episodes, replay
from storyengine.storage import LockBusyError, exclusive_lock, read_json
from storyengine.validate import validate_structure

ROOT = Path(__file__).resolve().parent.parent
SERIES_SRC = ROOT / "series" / "midnight-vending-machine"
EP1 = read_json(SERIES_SRC / "episodes" / "ep001" / "episode.json")


def make_ep2(ep1: dict) -> dict:
    ep = copy.deepcopy(ep1)
    ep["episode_number"] = 2
    ep["title"] = "テスト用の第2話"
    ep["featured_object"]["first_appearance"] = False  # 雨のしかくの再登場（性質は同じ）
    ep["continuity"] = {"end_state": "テスト", "new_facts": ["テストの出来事"],
                        "mysteries_progressed": [{"id": "mochi-identity", "note": "テスト"}]}
    return ep


class SeriesTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "series"
        self.root.mkdir()
        shutil.copy(SERIES_SRC / "series.json", self.root / "series.json")
        self.series = load_series(self.root)
        self.ep1 = copy.deepcopy(EP1)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def state_bytes(self):
        p = self.series.state_path
        return p.read_bytes() if p.exists() else None


class TestValidation(SeriesTestCase):
    def errors(self, ep):
        return plan_commit(self.series, ep).errors

    def test_episode1_is_valid(self):
        self.assertEqual(self.errors(self.ep1), [])

    def test_wrong_cut_count(self):
        self.ep1["cuts"].pop()
        self.assertTrue(any("カット数" in e for e in self.errors(self.ep1)))

    def test_wrong_duration_and_total(self):
        self.ep1["cuts"][0]["duration_sec"] = 4
        errs = self.errors(self.ep1)
        self.assertTrue(any("cuts[0].duration_sec" in e for e in errs))
        self.assertTrue(any("合計尺が 21 秒" in e for e in errs))

    def test_missing_required_fields(self):
        del self.ep1["cuts"][2]["start_image"]
        self.ep1["cuts"][3]["sound"]["music"] = " "
        self.ep1["posts"]["youtube"]["title"] = ""
        errs = self.errors(self.ep1)
        self.assertIn("必須項目が空です: cuts[2].start_image", errs)
        self.assertIn("必須項目が空です: cuts[3].sound.music", errs)
        self.assertIn("必須項目が空です: posts.youtube.title", errs)

    def test_dialogue_forbidden(self):
        self.ep1["cuts"][1]["dialogue"] = "わあ！"
        self.assertTrue(any("せりふ" in e for e in self.errors(self.ep1)))

    def test_sfx_timing_out_of_range(self):
        self.ep1["cuts"][0]["sound"]["sfx"][0]["at_sec"] = 3.5
        self.assertTrue(any("at_sec" in e for e in self.errors(self.ep1)))

    def test_unknown_character(self):
        self.ep1["cuts"][0]["characters_in_shot"].append("stranger")
        self.assertTrue(any("未登録の人物" in e for e in self.errors(self.ep1)))

    def test_placeholder_rejected(self):
        self.ep1["summary"] = "TODO あとで書く"
        self.assertTrue(any("プレースホルダー" in e for e in self.errors(self.ep1)))

    def test_episode_number_must_be_next(self):
        self.ep1["episode_number"] = 3
        self.assertTrue(any("次に作るべき話数は 1" in e for e in self.errors(self.ep1)))

    def test_youtube_title_limit(self):
        self.ep1["posts"]["youtube"]["title"] = "あ" * 101
        self.assertTrue(any("YouTube のタイトル" in e for e in self.errors(self.ep1)))

    def test_generic_format_from_config(self):
        """エンジンは作品固有の尺を持たない：設定を変えれば別形式を検証できる。"""
        cfg = copy.deepcopy(self.series.config)
        cfg["format"]["cut_durations"] = [5, 5, 5, 5]
        other = type(self.series)(root=self.root, config=cfg)
        self.assertTrue(any("カット数が 5 です（必要: 4）" in e for e in validate_structure(other, self.ep1)))


class TestCommit(SeriesTestCase):
    def test_create_then_idempotent_rerun(self):
        plan, _ = commit(self.series, self.ep1)
        self.assertEqual(plan.action, "create")
        before = self.state_bytes()
        self.assertEqual(read_json(self.series.state_path)["next_episode"], 2)

        plan, _ = commit(self.series, self.ep1)
        self.assertEqual(plan.action, "unchanged")
        self.assertEqual(self.state_bytes(), before)
        self.assertEqual(len(load_episodes(self.series)), 1)

    def test_duplicate_number_with_different_content_rejected(self):
        commit(self.series, self.ep1)
        before = self.state_bytes()
        changed = copy.deepcopy(self.ep1)
        changed["summary"] += "（別案）"
        plan, _ = commit(self.series, changed)
        self.assertFalse(plan.ok)
        self.assertTrue(any("話数の重複" in e for e in plan.errors))
        self.assertEqual(self.state_bytes(), before)
        self.assertEqual(load_episodes(self.series)[0], self.ep1)

    def test_invalid_draft_does_not_touch_history(self):
        commit(self.series, self.ep1)
        before = self.state_bytes()
        bad = make_ep2(self.ep1)
        bad["cuts"].pop()
        plan, _ = commit(self.series, bad)
        self.assertFalse(plan.ok)
        self.assertEqual(self.state_bytes(), before)
        self.assertFalse(self.series.episode_dir(2).exists())

    def test_second_episode_and_history(self):
        commit(self.series, self.ep1)
        plan, _ = commit(self.series, make_ep2(self.ep1))
        self.assertEqual(plan.action, "create")
        state = read_json(self.series.state_path)
        self.assertEqual(state["next_episode"], 3)
        self.assertEqual(state["featured_objects"]["rain-cube"]["appearances"], [1, 2])
        self.assertEqual(state["mysteries"]["mochi-identity"]["notes"][0]["episode"], 2)

    def test_item_properties_cannot_change(self):
        commit(self.series, self.ep1)
        ep2 = make_ep2(self.ep1)
        ep2["featured_object"]["properties"][0] = "雨ではなく雪が降る"
        plan, _ = commit(self.series, ep2)
        self.assertTrue(any("性質" in e for e in plan.errors))

    def test_unknown_mystery_rejected(self):
        commit(self.series, self.ep1)
        ep2 = make_ep2(self.ep1)
        ep2["continuity"]["mysteries_resolved"] = [{"id": "no-such", "resolution": "x"}]
        plan, _ = commit(self.series, ep2)
        self.assertTrue(any("未登録の謎" in e for e in plan.errors))

    def test_replace_only_latest(self):
        commit(self.series, self.ep1)
        commit(self.series, make_ep2(self.ep1))
        changed = copy.deepcopy(self.ep1)
        changed["summary"] += "（改稿）"
        plan, _ = commit(self.series, changed, replace=True)
        self.assertTrue(any("最新話" in e for e in plan.errors))
        ep2b = make_ep2(self.ep1)
        ep2b["summary"] += "（改稿）"
        plan, _ = commit(self.series, ep2b, replace=True)
        self.assertEqual(plan.action, "replace")
        self.assertEqual(read_json(self.series.state_path)["next_episode"], 3)

    def test_state_write_failure_rolls_back(self):
        real = commit_mod.atomic_write_json

        def failing(path, obj):
            if Path(path) == self.series.state_path:
                raise OSError("disk full (simulated)")
            return real(path, obj)

        with mock.patch.object(commit_mod, "atomic_write_json", failing):
            with self.assertRaises(OSError):
                commit(self.series, self.ep1)
        self.assertFalse(self.series.episode_dir(1).exists())
        self.assertIsNone(self.state_bytes())
        self.assertEqual([p.name for p in self.series.episodes_dir.iterdir()], [])
        # 再実行で正常に登録できる
        plan, _ = commit(self.series, self.ep1)
        self.assertEqual(plan.action, "create")

    def test_replace_failure_restores_previous(self):
        commit(self.series, self.ep1)
        before = self.state_bytes()
        changed = copy.deepcopy(self.ep1)
        changed["summary"] += "（改稿）"
        real = commit_mod.atomic_write_json

        def failing(path, obj):
            if Path(path) == self.series.state_path:
                raise OSError("simulated")
            return real(path, obj)

        with mock.patch.object(commit_mod, "atomic_write_json", failing):
            with self.assertRaises(OSError):
                commit(self.series, changed, replace=True)
        self.assertEqual(load_episodes(self.series)[0], self.ep1)
        self.assertEqual(self.state_bytes(), before)

    def test_recovers_from_crash_before_state_write(self):
        """エピソード配置後・state 更新前に強制終了した状況からの復旧。"""
        commit(self.series, self.ep1)
        from storyengine.state import initial_state
        from storyengine.storage import atomic_write_json
        initial = initial_state(self.series)
        atomic_write_json(self.series.state_path, initial)  # 古い state に戻す
        (self.series.episodes_dir / ".staging-ep002-999").mkdir()  # 中断の残骸
        plan, notes = commit(self.series, self.ep1)
        self.assertEqual(plan.action, "unchanged")
        self.assertTrue(any("修復" in n for n in notes))
        self.assertTrue(any("一時ディレクトリ" in n for n in notes))
        self.assertEqual(read_json(self.series.state_path)["next_episode"], 2)

    def test_lock_prevents_concurrent_runs(self):
        with exclusive_lock(self.series.lock_path):
            with self.assertRaises(LockBusyError):
                commit(self.series, self.ep1)

    def test_state_is_rebuildable(self):
        commit(self.series, self.ep1)
        self.series.state_path.unlink()
        self.assertEqual(main(["--series", str(self.root), "rebuild"]), 0)
        self.assertEqual(read_json(self.series.state_path), replay(self.series, load_episodes(self.series)))


class TestCli(SeriesTestCase):
    def run_cli(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["--series", str(self.root), *args])
        return code, buf.getvalue()

    def test_scaffold_creates_unfinished_draft(self):
        code, _ = self.run_cli("scaffold")
        self.assertEqual(code, 0)
        draft = self.series.draft_path(1)
        tmpl = json.loads(draft.read_text(encoding="utf-8"))
        self.assertEqual([c["duration_sec"] for c in tmpl["cuts"]], [3, 4, 4, 5, 4])
        code, out = self.run_cli("validate", str(draft))
        self.assertEqual(code, 1)
        self.assertIn("プレースホルダー", out)

    def test_brief_and_status(self):
        commit(self.series, self.ep1)
        code, out = self.run_cli("brief")
        self.assertEqual(code, 0)
        self.assertIn("次は第2話", out)
        self.assertIn("rain-cube", out)
        code, out = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("一致", out)


class TestRepositoryData(unittest.TestCase):
    """リポジトリに保存されている実データの整合。"""

    def test_saved_state_matches_episodes(self):
        series = load_series(SERIES_SRC)
        self.assertEqual(read_json(series.state_path), replay(series, load_episodes(series)))


if __name__ == "__main__":
    unittest.main()
