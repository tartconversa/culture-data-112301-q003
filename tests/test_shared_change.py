"""端到端业务测试：一次共享音轨替换的连锁影响。

场景对应资料中的 CH1（条目 I001 在 v2 替换音轨，失效上下文 ctx-1/ctx-17）：
I001 被 X001 与 X181 两个引用共用，且同时出现在 pc 与 console 两个德语
内容集合中。本测试按时间顺序观察同一次变化如何同时改变：

1. 抽样证据：v1 适用的 random 记录在 v2 失效，样本数下降、层被标记；
2. 审核任务：已接受的 v1 工单 T-old 失效，需要定向重派，工时释放；
3. 渠道可发布范围：两个德语渠道同时出现阻断级证据缺口，补齐复查后
   失效证据条件同时解除（共享修复一次覆盖多渠道）。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from releaseqa.cli import main  # noqa: E402
from releaseqa.defects import build_ledger  # noqa: E402
from releaseqa.model import ProjectData  # noqa: E402
from releaseqa.release import evaluate_gate  # noqa: E402
from releaseqa.store import Store  # noqa: E402


class SharedMaterialChangeTests(unittest.TestCase):
    def setUp(self):
        self.data = ProjectData.load(ROOT)
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name)
        self.store = Store(self.state_dir)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv):
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            main(["--state", str(self.state_dir), *argv])
        return json.loads(buf.getvalue())

    def test_shared_audio_swap_propagates_through_evidence_tasks_and_channels(self):
        # ---- 环节 0：基线。v1 下 X001/X181 的 random 证据适用 -------------
        ledger_v1 = build_ledger(self.data, 1)
        applicable_v1 = {
            ar.review["id"]: ar.applicable
            for ar in self.data.applicable_reviews(1)
            if ar.review["reference"] in {"X001", "X181"}
        }
        self.assertTrue(all(applicable_v1.values()))
        gate_v1_pc = evaluate_gate(self.data, "V1-de-pc")
        gate_v1_console = evaluate_gate(self.data, "V1-de-console")
        # v1 没有失效证据类缺口。
        self.assertTrue(
            next(c for c in gate_v1_pc.checks if c["id"] == "voided_blocker_evidence_replaced")["passed"]
        )

        # ---- 环节 1：换版后抽样证据同时失效 -------------------------------
        ledger_v2 = build_ledger(self.data, 2)
        applicability_v2 = {
            ar.review["id"]: ar for ar in self.data.applicable_reviews(2)
            if ar.review["reference"] in {"X001", "X181"}
        }
        self.assertTrue(all(not ar.applicable for ar in applicability_v2.values()))
        # 两个引用分属不同风险层，失效在两层都留下痕迹。
        analyze = self.run_cli("analyze", "--language", "de", "--version", "2", "--channel", "pc")
        strata = {s["stratum"]: s for s in analyze["strata"]}
        self.assertIn("X001", strata["critical"]["voided_refs"])
        self.assertIn("X181", strata["dialogue"]["voided_refs"])
        # 失效证据不参与发生率估计，但层仍由其他概率样本支撑时保留可外推标记。
        for stratum in strata.values():
            self.assertGreaterEqual(stratum["sampled_refs"], 2)

        # ---- 环节 2：已接受的 v1 工单被换版失效，必须显式重派 -------------
        # T-old 是资料表中 U04 已接受、覆盖 X001/X181 的 v1 复查任务。
        self.assertEqual(
            sorted(next(t for t in self.data.tasks if t["id"] == "T-old")["references"]),
            ["X001", "X181"],
        )
        invalidation = self.run_cli("invalidate", "--version", "2", "--on", "2026-10-05")
        self.assertEqual(invalidation["invalidated_tasks"], ["T-old"])
        affected = invalidation["affected_accepted_tasks"][0]
        self.assertEqual(sorted(affected["redispatch_scope"]), ["X001", "X181"])
        # 已接受工作没有被静默取消：状态轨迹里看得到 propose→accept→invalidate。
        view = self.store.tasks()["T-old"]
        self.assertEqual(view["state"], "invalidated")
        self.assertEqual([h["action"] for h in view["history"]],
                         ["propose", "accept", "invalidate"])

        # ---- 环节 3：两个德语渠道闸门同时报阻断级证据缺口 -----------------
        for set_id in ("V2-de-pc", "V2-de-console"):
            gate = evaluate_gate(self.data, set_id, extra_reviews=self.store.reviews())
            check = next(c for c in gate.checks if c["id"] == "voided_blocker_evidence_replaced")
            self.assertFalse(check["passed"], f"{set_id} 应存在失效阻断证据缺口")

        # ---- 环节 4：定向计划把受影响引用排入重派，且不可外推 -------------
        plan = self.run_cli("plan", "--language", "de", "--version", "2", "--channel", "pc",
                            "--commit", "--on", "2026-10-06")
        directed = [t for t in plan["tasks"] if t["purpose"] == "directed"]
        directed_refs = {rid for t in directed for rid in t["references"]}
        self.assertTrue({"X001", "X181"}.issubset(directed_refs))
        self.assertTrue(all(not t["extrapolable"] for t in directed))
        # 随机补样任务若存在，必须保留纳入概率与可外推标记。
        for task in plan["tasks"]:
            if task["purpose"] == "random":
                self.assertIsNotNone(task["inclusion_probability"])
                self.assertTrue(task["extrapolable"])

        # ---- 环节 5：共享素材的一次定向复查同时解禁两个渠道 ---------------
        # X099（ctx-17）的 critical 旧证据同样因 CH1 失效；复查合格后，
        # pc 与 console 的失效阻断证据条件应同时转为通过。
        self.run_cli(
            "add-review", "--id", "N-x099", "--reference", "X099", "--version", "2",
            "--reviewer", "U06", "--purpose", "recheck", "--scope", "context",
            "--severity", "none", "--on", "2026-10-08",
        )
        for set_id in ("V2-de-pc", "V2-de-console"):
            gate = evaluate_gate(self.data, set_id, extra_reviews=self.store.reviews())
            check = next(c for c in gate.checks if c["id"] == "voided_blocker_evidence_replaced")
            self.assertTrue(check["passed"], f"{set_id} 的失效阻断证据条件应已解除")

        # ---- 环节 6：历史签发不可被新统计改写 -----------------------------
        self.run_cli(
            "signoff", "--id", "S-hold-de", "--content-set", "V2-de-pc",
            "--decision", "hold_channel_version", "--on", "2026-10-09",
        )
        before = list(self.store.signoffs())
        # 再次分析（统计可能变化）不得改动既有签发事件。
        self.run_cli("analyze", "--language", "de", "--version", "2", "--channel", "pc")
        self.assertEqual(self.store.signoffs(), before)

        # ---- 环节 7：资料包可导入后续版本并保留全部事件 -------------------
        bundle_path = self.state_dir / "bundle.json"
        self.run_cli("export", "--out", str(bundle_path))
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        self.assertEqual(bundle["bundle_format"], "releaseqa-bundle-v1")
        self.assertTrue(bundle["review_events"])
        self.assertTrue(any(e["action"] == "invalidate" for e in bundle["task_events"]))
        self.assertEqual(len(bundle["signoff_events"]), 1)


if __name__ == "__main__":
    unittest.main()
