"""审核排程测试：资质、自译回避、组约束、工时与任务生命周期。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from releaseqa.defects import build_ledger  # noqa: E402
from releaseqa.estimation import estimate_strata  # noqa: E402
from releaseqa.model import ProjectData  # noqa: E402
from releaseqa.planning import (  # noqa: E402
    build_plan,
    eligible_reviewers,
    explain_invalidation,
    reviewer_remaining_hours,
)


class PlanningRuleTests(unittest.TestCase):
    def setUp(self):
        self.data = ProjectData.load(ROOT)

    def test_reviewer_must_cover_language_and_kinds(self):
        # X000 是 de/text；U01 只具备 ja 语种，必须被排除。
        candidates = eligible_reviewers(self.data, ["X000"], "de")
        self.assertIn("U04", candidates)
        self.assertNotIn("U01", candidates)

    def test_reviewer_cannot_audit_own_translation(self):
        author = self.data.author_of_ref("X000")
        candidates = eligible_reviewers(self.data, ["X000"], "de")
        self.assertNotIn(author, candidates)

    def test_multi_kind_unit_requires_full_qualification(self):
        # 同时含 text 与 subtitle 的单元，只有两种类型资质都具备的审核员可承担。
        unit = ["X000", "X002"]  # de text + de subtitle
        candidates = eligible_reviewers(self.data, unit, "de")
        for rid in candidates:
            self.assertIn("text", self.data.reviewers[rid]["kinds"])
            self.assertIn("subtitle", self.data.reviewers[rid]["kinds"])

    def test_plan_separates_random_and_directed_purposes(self):
        ledger = build_ledger(self.data, 2)
        universe = sorted({
            rid
            for content in self.data.content_sets("de", 2)
            for rid in content["references"]
        })
        estimates = estimate_strata(self.data, ledger, universe)
        plan = build_plan(self.data, ledger, estimates, "de", 2, {})
        for task in plan:
            if task.purpose == "random":
                self.assertTrue(task.extrapolable)
                self.assertIsNotNone(task.inclusion_probability)
            else:
                self.assertFalse(task.extrapolable)
                self.assertIsNone(task.inclusion_probability)

    def test_directed_plan_covers_voided_blocker_and_open_critical(self):
        ledger = build_ledger(self.data, 2)
        universe = sorted({
            rid
            for content in self.data.content_sets("de", 2)
            for rid in content["references"]
        })
        estimates = estimate_strata(self.data, ledger, universe)
        plan = build_plan(self.data, ledger, estimates, "de", 2, {})
        directed_refs = {rid for task in plan if task.purpose == "directed" for rid in task.references}
        # X099 的 critical 证据在 v2 失效，必须被定向追查。
        self.assertIn("X099", directed_refs)
        # X011/X055 是仍开启的 critical，也必须出现。
        self.assertIn("X011", directed_refs)

    def test_accepted_tasks_block_replanning_and_consume_hours(self):
        ledger = build_ledger(self.data, 2)
        universe = sorted({
            rid
            for content in self.data.content_sets("de", 2)
            for rid in content["references"]
        })
        estimates = estimate_strata(self.data, ledger, universe)
        first = build_plan(self.data, ledger, estimates, "de", 2, {})
        target = first[0]
        accepted = {
            target.id: {
                "id": target.id, "state": "accepted", "reviewer": target.assigned_reviewer,
                "references": target.references, "version": 2, "hours": target.hours,
            }
        }
        second = build_plan(self.data, ledger, estimates, "de", 2, accepted)
        second_refs = {rid for task in second for rid in task.references}
        for rid in target.references:
            self.assertNotIn(rid, second_refs)
        remaining = reviewer_remaining_hours(self.data, accepted)
        self.assertEqual(
            remaining[target.assigned_reviewer],
            self.data.reviewers[target.assigned_reviewer]["hours"] - target.hours,
        )

    def test_invalidation_explains_redispatch_scope(self):
        # 资料表 T-old（accepted, v1）命中 I001 换版。
        seed_tasks = {
            t["id"]: {**t, "history": []} for t in self.data.tasks
        }
        explanation = explain_invalidation(self.data, seed_tasks, 2)
        hit = next(a for a in explanation["affected_accepted_tasks"] if a["task"] == "T-old")
        self.assertEqual(sorted(hit["redispatch_scope"]), ["X001", "X181"])
        # 已完成任务不在失效重派范围。
        self.assertNotIn(
            "T-done",
            [a["task"] for a in explanation["affected_accepted_tasks"]],
        )

    def test_group_task_uses_group_hours_and_grouped_refs(self):
        ledger = build_ledger(self.data, 2)
        universe = sorted({
            rid
            for content in self.data.content_sets("de", 2)
            for rid in content["references"]
        })
        estimates = estimate_strata(self.data, ledger, universe)
        plan = build_plan(self.data, ledger, estimates, "de", 2, {})
        grouped = [t for t in plan if t.group is not None]
        self.assertTrue(grouped)
        for task in grouped:
            self.assertEqual(task.hours, self.data.groups[task.group]["audit_hours"])
            for rid in task.references:
                self.assertIn(rid, self.data.groups[task.group]["references"])
                self.assertEqual(self.data.language_of_ref(rid), "de")

    def test_state_layer_complaint_drives_directed_task(self):
        # 选一条当前没有任何定向理由的德语引用，通过状态层追加投诉。
        ledger = build_ledger(self.data, 2)
        universe = sorted({
            rid
            for content in self.data.content_sets("de", 2)
            for rid in content["references"]
        })
        estimates = estimate_strata(self.data, ledger, universe)
        baseline = build_plan(self.data, ledger, estimates, "de", 2, {})
        baseline_refs = {rid for task in baseline for rid in task.references}
        pick = next(rid for rid in universe if rid not in baseline_refs)
        complaint = {
            "id": "C-new", "reference": pick, "version": 2, "reviewer": "U04",
            "purpose": "complaint", "inclusion_probability": None,
            "defect_scope": "context", "severity": "none", "reviewed_on": "2026-10-07",
        }
        plan = build_plan(
            self.data, ledger, estimates, "de", 2, {}, extra_reviews=[complaint]
        )
        hit = [t for t in plan if pick in t.references and t.purpose == "directed"]
        self.assertTrue(hit)
        self.assertFalse(hit[0].extrapolable)

    def test_random_replenishment_for_strata_without_probability_sample(self):        # 构造一个全部缺少概率样本的小全集，计划必须给出 random 补样并保留纳入概率。
        random_refs = {r["reference"] for r in self.data.random_reviews(1)}
        universe = [rid for rid in self.data.references if rid not in random_refs][:8]
        ledger = build_ledger(self.data, 1)
        estimates = estimate_strata(self.data, ledger, universe)
        self.assertTrue(all(not e.extrapolable for e in estimates))
        plan = build_plan(self.data, ledger, estimates, "de", 1, {})
        random_tasks = [t for t in plan if t.purpose == "random"]
        self.assertTrue(random_tasks)
        for task in random_tasks:
            self.assertTrue(task.extrapolable)
            self.assertEqual(task.inclusion_probability, 0.4)
        # 定向任务始终不能被标记为可外推。
        for task in plan:
            if task.purpose == "directed":
                self.assertFalse(task.extrapolable)
                self.assertIsNone(task.inclusion_probability)


if __name__ == "__main__":
    unittest.main()
