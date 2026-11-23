"""予算（月・1本の上限）のテスト。通信・費用なし。"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from storyengine import budget, media
from storyengine.series import load_series
from storyengine.storage import read_json
from tests.test_media import SERIES_SRC, FakeGen

ROOT = Path(__file__).resolve().parent.parent


class BudgetCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        shutil.copytree(ROOT / "budget", self.tmp / "budget")
        self.rec_dir = self.tmp / "series" / "s1" / "media" / "ep001"
        self.rec_dir.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_spends(self, items):
        versions = [{"status": st, "cost_usd": usd, "created_at": ts} for usd, ts, st in items]
        (self.rec_dir / "records.json").write_text(json.dumps(
            {"episode_number": 1, "slots": {"cut01_start": {"versions": versions}}}), encoding="utf-8")


class TestBudget(BudgetCase):
    def test_policy_values(self):
        p, ledger = budget.load(self.tmp)
        self.assertEqual((p["monthly_cap_jpy"], p["per_episode_cap_jpy"], p["auto_topup"]), (3000, 300, False))
        self.assertTrue(all(d.get("auto_topup") is False for d in ledger["deposits"]))

    def test_month_uses_japan_time(self):
        self.assertEqual(budget.month_of("2026-11-30T15:30:00+00:00", "Asia/Tokyo"), "2026-12")
        self.assertEqual(budget.month_of("2026-11-30T14:30:00+00:00", "Asia/Tokyo"), "2026-11")

    def test_failed_attempts_count_and_month_cap_is_hard(self):
        # 今月 $18（=2880円）消費済み、失敗分も含む
        self.write_spends([(10.0, "2026-11-20T00:00:00Z", "complete"), (8.0, "2026-11-21T00:00:00Z", "failed")])
        s = budget.summary(self.tmp, month="2026-11", series="s1", episode=1)
        self.assertAlmostEqual(s["month_spent_jpy"], 2880)
        import unittest.mock as m
        with m.patch.object(budget, "current_month", return_value="2026-11"):
            with self.assertRaises(budget.BudgetError) as cm:  # 1本の上限を認めても月の上限は超えられない
                budget.check(1.0, series="s1", episode=2, root=self.tmp, allow_over_episode=True)
            self.assertIn("月の上限", str(cm.exception))
            budget.check(0.5, series="s1", episode=2, root=self.tmp)  # 80円は入る

    def test_episode_cap_is_soft(self):
        self.write_spends([(1.5, "2026-11-20T00:00:00Z", "complete")])  # 240円
        import unittest.mock as m
        with m.patch.object(budget, "current_month", return_value="2026-11"):
            with self.assertRaises(budget.BudgetError) as cm:
                budget.check(0.5, series="s1", episode=1, root=self.tmp)  # 240+80 > 300
            self.assertIn("1本の上限", str(cm.exception))
            budget.check(0.5, series="s1", episode=1, root=self.tmp, allow_over_episode=True)

    def test_other_month_not_counted(self):
        self.write_spends([(18.0, "2026-10-20T00:00:00Z", "complete")])  # 先月の第1話
        import unittest.mock as m
        with m.patch.object(budget, "current_month", return_value="2026-11"):
            budget.check(1.0, series="s1", episode=2, root=self.tmp)  # 月の上限は今月分だけで判定
            with self.assertRaises(budget.BudgetError):  # 1本の上限は、その話の全期間の合計で判定
                budget.check(1.0, series="s1", episode=1, root=self.tmp)


class TestGuardStopsGeneration(unittest.TestCase):
    def test_before_paid_refusal_generates_nothing(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            root = tmp / "series"
            shutil.copytree(SERIES_SRC, root, ignore=shutil.ignore_patterns("drafts", "media", ".lock"))
            cfg = read_json(root / "series.json")
            cfg["generation"]["image"] = {"provider": "test", "model": "m", "params": {}}
            (root / "series.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
            series = load_series(root)

            class PaidGen(FakeGen):
                paid = True

            gen = PaidGen()

            def refuse(plan):
                raise budget.BudgetError("月の上限を超えるため送りません")

            res = media.run_images(series, 1, gen, before_paid=refuse)
            self.assertEqual(gen.calls, [])
            self.assertIn("月の上限", res.failed[0][1])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
