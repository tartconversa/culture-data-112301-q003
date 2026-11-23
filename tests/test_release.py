"""渠道闸门、剧情组完整性、签发与撤回测试。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from releaseqa.model import ProjectData  # noqa: E402
from releaseqa.release import (  # noqa: E402
    deliverable_subset_options,
    evaluate_gate,
    withdrawal_impact,
)
from releaseqa.store import Store  # noqa: E402


class GateTests(unittest.TestCase):
    def setUp(self):
        self.data = ProjectData.load(ROOT)

    def test_v2_gate_fails_on_open_and_voided_blockers(self):
        gate = evaluate_gate(self.data, "V2-de-pc")
        self.assertFalse(gate.passed)
        ids = {c["id"]: c["passed"] for c in gate.checks}
        self.assertFalse(ids["block_severity_closed"])
        self.assertFalse(ids["voided_blocker_evidence_replaced"])
        self.assertTrue(ids["required_strata_coverage"])
        self.assertTrue(ids["story_group_integrity"])
        self.assertTrue({b["reference"] for b in gate.voided_blockers} >= {"X099"})

    def test_recheck_replaces_voided_blocker_evidence(self):
        recheck = {
            "id": "N-rc", "reference": "X099", "version": 2, "reviewer": "U06",
            "purpose": "recheck", "inclusion_probability": None,
            "defect_scope": "context", "severity": "none", "reviewed_on": "2026-10-03",
        }
        gate = evaluate_gate(self.data, "V2-de-pc", extra_reviews=[recheck])
        ids = {c["id"]: c["passed"] for c in gate.checks}
        self.assertTrue(ids["voided_blocker_evidence_replaced"])

    def test_recheck_still_defective_does_not_clear_gate(self):
        recheck = {
            "id": "N-rc2", "reference": "X099", "version": 2, "reviewer": "U06",
            "purpose": "recheck", "inclusion_probability": None,
            "defect_scope": "context", "severity": "critical", "reviewed_on": "2026-10-03",
        }
        gate = evaluate_gate(self.data, "V2-de-pc", extra_reviews=[recheck])
        ids = {c["id"]: c["passed"] for c in gate.checks}
        self.assertFalse(ids["voided_blocker_evidence_replaced"])

    def test_story_group_cannot_be_split(self):
        content = self.data.versions["V2-de-pc"]
        locked = next(
            g for g in self.data.groups.values()
            if g["must_deliver_together"] and set(g["references"]) & set(content["references"])
        )
        in_set = sorted(set(locked["references"]) & set(content["references"]))
        subset = [in_set[0]]  # 只交组内一个成员
        gate = evaluate_gate(self.data, "V2-de-pc", subset=subset)
        ids = {c["id"]: c["passed"] for c in gate.checks}
        self.assertFalse(ids["story_group_integrity"])
        violation = gate.group_constraints[0]
        self.assertEqual(violation["group"], locked["id"])

    def test_subset_options_respect_group_closure(self):
        options = deliverable_subset_options(self.data, "V2-de-pc", [])
        # 当前数据闸门整体不过；即使有局部可过单元，也不会输出违反组约束的子集。
        option = options[0]
        if option["references"]:
            chosen = set(option["references"])
            for group in self.data.groups.values():
                if not group["must_deliver_together"]:
                    continue
                in_set = set(group["references"]) & set(self.data.versions["V2-de-pc"]["references"])
                if in_set & chosen:
                    self.assertTrue(in_set.issubset(chosen))
            # 含开启阻断缺陷的引用不得进入子集。
            for blocker in (
                evaluate_gate(self.data, "V2-de-pc").open_blockers
            ):
                for rid in blocker["affected_in_set"]:
                    self.assertNotIn(rid, chosen)

    def test_withdrawal_only_touches_online_sets_using_item(self):
        impact = withdrawal_impact(self.data, "I001", "critical", store=Store(ROOT / "state-empty"))
        # I001 是德语音频，在线回执只有 ja/fr 集合 → 无在线渠道受影响，其他版本保留。
        self.assertEqual(impact["affected_online_sets"], [])
        self.assertIn("V1-ja-pc", impact["preserved_online_sets"])
        self.assertIn("V1-fr-console", impact["preserved_online_sets"])

    def test_withdrawal_detects_affected_online_set(self):
        # 构造一个使用 I001 引用 X001 的在线集合（模拟 de 渠道已上架）。
        data = ProjectData.load(ROOT)
        data.receipts.append(
            {"id": "D-fake", "content_set": "V1-de-pc", "received_on": "2026-09-12", "state": "online"}
        )
        impact = withdrawal_impact(data, "I001", "critical", store=Store(ROOT / "state-empty"))
        affected = {a["content_set"] for a in impact["affected_online_sets"]}
        self.assertIn("V1-de-pc", affected)
        entry = next(a for a in impact["affected_online_sets"] if a["content_set"] == "V1-de-pc")
        scopes = {o["scope"] for o in entry["withdrawal_scope_options"]}
        self.assertEqual(scopes, {"content_set", "affected_references"})


class StoreImmutabilityTests(unittest.TestCase):
    def test_signoffs_are_append_only(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            event = {
                "id": "S1", "at": "2026-10-01", "content_set": "V1-de-pc",
                "channel": "pc", "language": "de", "version": 1,
                "decision": "hold_channel_version", "references": ["X000"],
                "gate": {"passed": False}, "snapshot": {},
            }
            store.add_signoff(event)
            with self.assertRaises(ValueError):
                store.add_signoff(event)
            self.assertEqual(len(store.signoffs()), 1)


if __name__ == "__main__":
    unittest.main()
