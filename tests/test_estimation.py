"""风险层估计口径测试：概率样本资格、投诉不外推、缺证层不给点估计。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from releaseqa.defects import build_ledger  # noqa: E402
from releaseqa.estimation import estimate_strata, overall_exposure  # noqa: E402
from releaseqa.model import ProjectData  # noqa: E402


class EstimationTests(unittest.TestCase):
    def setUp(self):
        self.data = ProjectData.load(ROOT)
        self.de_v2 = sorted({
            rid
            for content in self.data.content_sets("de", 2)
            for rid in content["references"]
        })

    def test_only_random_records_enter_sample_base(self):
        ledger = build_ledger(self.data, 2)
        estimates = estimate_strata(self.data, ledger, self.de_v2)
        total_sampled = sum(e.sampled_refs for e in estimates)
        random_refs = {
            r["reference"]
            for r in self.data.random_reviews(2)
            if r["reference"] in set(self.de_v2)
        }
        self.assertEqual(total_sampled, len(random_refs))
        for estimate in estimates:
            # 投诉与复查只列示，不进入样本数。
            self.assertNotIn("complaint", estimate.reason)
            self.assertTrue(all(p > 0 for p in estimate.inclusion_probabilities))

    def test_complaints_are_listed_but_not_rates(self):
        ledger = build_ledger(self.data, 1)
        universe = self.data.versions["V1-de-pc"]["references"]
        estimates = estimate_strata(self.data, ledger, universe)
        with_complaints = [e for e in estimates if e.complaint_refs]
        self.assertTrue(with_complaints)
        # 点估计只取决于 random 样本；投诉记录再多也不改变 sampled_refs。
        for estimate in estimates:
            self.assertLessEqual(estimate.sampled_refs, estimate.universe_refs)

    def test_stratum_without_probability_sample_is_not_extrapolated(self):
        # 人造一个只含单条引用、且无概率抽检的全集。
        ledger = build_ledger(self.data, 2)
        # 选一条在 v2 没有适用 random 审核的引用。
        random_refs = {r["reference"] for r in self.data.random_reviews(2)}
        pick = next(rid for rid in self.de_v2 if rid not in random_refs)
        estimates = estimate_strata(self.data, ledger, [pick])
        self.assertEqual(len(estimates), 1)
        self.assertFalse(estimates[0].extrapolable)
        self.assertIsNone(estimates[0].point)
        self.assertIsNone(estimates[0].ci_high)

    def test_overall_refuses_single_rate_when_stratum_missing(self):
        ledger = build_ledger(self.data, 2)
        random_refs = {r["reference"] for r in self.data.random_reviews(2)}
        pick = next(rid for rid in self.de_v2 if rid not in random_refs)
        bad = estimate_strata(self.data, ledger, [pick])
        overall = overall_exposure(bad)
        self.assertIsNone(overall["point"])
        self.assertEqual(len(overall["missing_strata"]), 1)

    def test_confidence_interval_brackets_point_and_within_unit(self):
        ledger = build_ledger(self.data, 1)
        estimates = estimate_strata(
            self.data, ledger, self.data.versions["V1-de-pc"]["references"]
        )
        for estimate in estimates:
            if not estimate.extrapolable:
                continue
            self.assertLessEqual(estimate.ci_low, estimate.point + 1e-12)
            self.assertGreaterEqual(estimate.ci_high, estimate.point - 1e-12)
            self.assertGreaterEqual(estimate.ci_low, 0.0)
            self.assertLessEqual(estimate.ci_high, 1.0)

    def test_recheck_pass_keeps_random_unit_in_sample_base(self):
        target = "X022"  # v1 random major，补一条 recheck none 关闭；该引用属于日语集合。
        recheck = {
            "id": "N-rc", "reference": target, "version": 1, "reviewer": "U08",
            "purpose": "recheck", "inclusion_probability": None,
            "defect_scope": "context", "severity": "none", "reviewed_on": "2026-09-30",
        }
        universe = self.data.versions["V1-ja-pc"]["references"]
        ledger_before = build_ledger(self.data, 1)
        ledger_after = build_ledger(self.data, 1, extra_reviews=[recheck])
        before = {e.stratum: e for e in estimate_strata(self.data, ledger_before, universe)}
        after = {e.stratum: e for e in estimate_strata(
            self.data, ledger_after, universe, extra_reviews=[recheck])}
        stratum = self.data.references[target]["risk"]
        # 样本基数不变，变化的只是缺陷计数。
        self.assertEqual(before[stratum].sampled_refs, after[stratum].sampled_refs)
        self.assertEqual(after[stratum].defective_sampled, before[stratum].defective_sampled - 1)


if __name__ == "__main__":
    unittest.main()
