"""证据适用性与缺陷台账的单元测试。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from releaseqa.defects import build_ledger  # noqa: E402
from releaseqa.model import ProjectData  # noqa: E402


class ApplicabilityTests(unittest.TestCase):
    def setUp(self):
        self.data = ProjectData.load(ROOT)

    def test_v1_evidence_applicable_in_v1(self):
        review = next(r for r in self.data.reviews if r["id"] == "A051")
        ar = self.data.review_applicability(review, 1)
        self.assertTrue(ar.applicable)

    def test_old_context_evidence_invalidated_after_audio_swap(self):
        # A051 是 ctx-17 上的 critical 发现；CH1 在 v2 使 ctx-17 时序证据失效。
        review = next(r for r in self.data.reviews if r["id"] == "A051")
        ar = self.data.review_applicability(review, 2)
        self.assertFalse(ar.applicable)
        self.assertEqual(ar.reason, "context-invalidated")

    def test_new_review_after_change_is_applicable(self):
        # 换版之后产生的新审核针对新条件，不能被旧失效名单连坐。
        new_review = {
            "id": "N-new", "reference": "X099", "version": 2, "reviewer": "U06",
            "purpose": "recheck", "inclusion_probability": None,
            "defect_scope": "context", "severity": "none", "reviewed_on": "2026-10-03",
        }
        ar = self.data.review_applicability(new_review, 2)
        self.assertTrue(ar.applicable)

    def test_item_identity_alone_does_not_convey_inheritance(self):
        # I001 在 v2 换指纹，其 v1 content/context 证据均不得继承。
        review = next(r for r in self.data.reviews if r["id"] == "A001")
        self.assertEqual(self.data.references[review["reference"]]["item"], "I001")
        ar = self.data.review_applicability(review, 2)
        self.assertFalse(ar.applicable)

    def test_later_version_review_not_usable_for_earlier_version(self):
        review = {
            "id": "N-future", "reference": "X000", "version": 2, "reviewer": "U04",
            "purpose": "random", "inclusion_probability": 0.4,
            "defect_scope": "content", "severity": "none", "reviewed_on": "2026-10-01",
        }
        ar = self.data.review_applicability(review, 1)
        self.assertFalse(ar.applicable)
        self.assertEqual(ar.reason, "review-from-later-version")


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.data = ProjectData.load(ROOT)

    def test_voided_critical_listed_and_resolvable(self):
        ledger = build_ledger(self.data, 2)
        voided_x099 = [f for f in ledger.voided_findings if f.reference == "X099"]
        self.assertTrue(voided_x099)
        self.assertTrue(all(f.voided for f in voided_x099))
        self.assertIsNone(voided_x099[0].resolved_by_review)

        recheck = {
            "id": "N-close", "reference": "X099", "version": 2, "reviewer": "U06",
            "purpose": "recheck", "inclusion_probability": None,
            "defect_scope": "context", "severity": "none", "reviewed_on": "2026-10-03",
        }
        ledger2 = build_ledger(self.data, 2, extra_reviews=[recheck])
        resolved = [f for f in ledger2.voided_findings if f.reference == "X099"]
        self.assertTrue(all(f.resolved_by_review == "N-close" for f in resolved))
        self.assertTrue(all(f.resolved_severity == "none" for f in resolved))

    def test_recheck_none_closes_open_defect_but_keeps_sampling_identity(self):
        # X022 在 v1 有 major 上下文缺陷且尚未关闭。
        ledger = build_ledger(self.data, 1)
        self.assertTrue(ledger.is_defective("X022", "major"))
        recheck = {
            "id": "N-fix", "reference": "X022", "version": 1, "reviewer": "U08",
            "purpose": "recheck", "inclusion_probability": None,
            "defect_scope": "context", "severity": "none", "reviewed_on": "2026-09-30",
        }
        ledger2 = build_ledger(self.data, 1, extra_reviews=[recheck])
        self.assertFalse(ledger2.is_defective("X022", "major"))
        finding = ledger2.by_key[("X022", "context")]
        self.assertFalse(finding.open)
        self.assertEqual(finding.closed_by, "N-fix")
        # 原 random 记录仍在，抽样身份不被复查抹掉。
        random_record = next(
            r for r in self.data.reviews
            if r["reference"] == "X022" and r["purpose"] == "random"
        )
        self.assertIsNotNone(random_record["inclusion_probability"])

    def test_content_defect_propagates_to_shared_item_refs(self):
        # 构造一个 content 缺陷：条目被两个引用共用，缺陷应同时作用于两者。
        item_id = self.data.references["X000"]["item"]
        sibling = next(rid for rid in self.data.refs_for_item(item_id) if rid != "X000")
        review = {
            "id": "N-content", "reference": "X000", "version": 1, "reviewer": "U04",
            "purpose": "random", "inclusion_probability": 0.4,
            "defect_scope": "content", "severity": "critical", "reviewed_on": "2026-09-30",
        }
        ledger = build_ledger(self.data, 1, extra_reviews=[review])
        self.assertTrue(ledger.is_defective("X000", "critical"))
        self.assertTrue(ledger.is_defective(sibling, "critical"))
        finding = ledger.by_key[("X000", "content")]
        self.assertIn(sibling, finding.propagated_to)

    def test_context_defect_does_not_propagate(self):
        ledger = build_ledger(self.data, 1)
        finding = next(f for f in ledger.open_findings() if f.reference == "X099")
        self.assertEqual(finding.scope, "context")
        self.assertEqual(finding.propagated_to, [])


if __name__ == "__main__":
    unittest.main()
