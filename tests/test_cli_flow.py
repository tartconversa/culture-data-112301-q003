"""CLI 端到端测试：任务、审核、闸门、签发与资料包的命令行契约。"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from releaseqa.cli import main  # noqa: E402
from releaseqa.model import ProjectData  # noqa: E402
from releaseqa.release import evaluate_gate  # noqa: E402
from releaseqa.store import Store  # noqa: E402


class CliFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            main(["--state", str(self.state), *argv])
        return json.loads(buf.getvalue())

    def test_review_qualification_and_self_audit_are_enforced(self):
        # U03 是 X099 条目的译作者 → 自译复核必须被拒绝。
        with self.assertRaises(SystemExit):
            self.cli(
                "add-review", "--id", "X1", "--reference", "X099", "--version", "2",
                "--reviewer", "U03", "--purpose", "recheck", "--scope", "context",
                "--severity", "none", "--on", "2026-10-03",
            )
        # 资质不符（U01 只会日语，X099 是德语）同样拒绝。
        with self.assertRaises(SystemExit):
            self.cli(
                "add-review", "--id", "X2", "--reference", "X099", "--version", "2",
                "--reviewer", "U01", "--purpose", "recheck", "--scope", "context",
                "--severity", "none", "--on", "2026-10-03",
            )
        # 合格且非自译的审核员 U06 可以登记。
        out = self.cli(
            "add-review", "--id", "X3", "--reference", "X099", "--version", "2",
            "--reviewer", "U06", "--purpose", "recheck", "--scope", "context",
            "--severity", "none", "--on", "2026-10-03",
        )
        self.assertEqual(out["added_review"]["id"], "X3")

    def test_inclusion_probability_contract_is_enforced(self):
        # random 必须带纳入概率。
        with self.assertRaises(SystemExit):
            self.cli(
                "add-review", "--id", "P1", "--reference", "X000", "--version", "1",
                "--reviewer", "U04", "--purpose", "random",
                "--scope", "context", "--severity", "none", "--on", "2026-10-03",
            )
        # 定向复查不允许携带纳入概率。
        with self.assertRaises(SystemExit):
            self.cli(
                "add-review", "--id", "P2", "--reference", "X099", "--version", "2",
                "--reviewer", "U06", "--purpose", "recheck",
                "--inclusion-probability", "0.4",
                "--scope", "context", "--severity", "none", "--on", "2026-10-03",
            )
        # 合规 random 可以登记。
        out = self.cli(
            "add-review", "--id", "P3", "--reference", "X003", "--version", "1",
            "--reviewer", "U04", "--purpose", "random",
            "--inclusion-probability", "0.4",
            "--scope", "context", "--severity", "none", "--on", "2026-10-03",
        )
        self.assertEqual(out["added_review"]["inclusion_probability"], 0.4)

    def test_task_lifecycle_consumes_and_releases_hours(self):
        plan = self.cli(
            "plan", "--language", "de", "--version", "2", "--channel", "pc",
            "--commit", "--on", "2026-10-01",
        )
        data = ProjectData.load(ROOT)
        target = next(t for t in plan["tasks"] if t["assigned_reviewer"])
        from releaseqa.cli import _all_tasks  # noqa: E402
        from releaseqa.planning import reviewer_remaining_hours  # noqa: E402
        baseline = reviewer_remaining_hours(
            data, _all_tasks(data, Store(self.state))
        )[target["assigned_reviewer"]]
        accept = self.cli("task-accept", "--task", target["id"], "--on", "2026-10-02")
        self.assertEqual(accept["reviewer"], target["assigned_reviewer"])
        self.assertEqual(
            accept["remaining_hours"][target["assigned_reviewer"]],
            baseline - target["hours"],
        )
        # 重复接受被拒绝。
        with self.assertRaises(SystemExit):
            self.cli("task-accept", "--task", target["id"], "--on", "2026-10-02")
        # 退回释放工时并给出重派范围。
        returned = self.cli(
            "task-return", "--task", target["id"], "--reason", "排期冲突", "--on", "2026-10-03"
        )
        self.assertEqual(returned["released_hours"], target["hours"])
        self.assertEqual(sorted(returned["redispatch_references"]), sorted(target["references"]))

    def test_signoff_requires_passing_gate_and_is_immutable(self):
        data = ProjectData.load(ROOT)
        # V2-de-pc 在无额外证据时闸门不过 → 子集/继续抽检之外的发布必须被拒绝。
        with self.assertRaises(SystemExit):
            self.cli(
                "signoff", "--id", "S1", "--content-set", "V2-de-pc",
                "--decision", "release_qualified_subset", "--on", "2026-10-05",
            )
        # 暂缓始终允许，且写入后不可变。
        self.cli(
            "signoff", "--id", "S2", "--content-set", "V2-de-pc",
            "--decision", "hold_channel_version", "--on", "2026-10-05",
        )
        store = Store(self.state)
        self.assertEqual(len(store.signoffs()), 1)
        with self.assertRaises(ValueError):
            store.add_signoff(store.signoffs()[0])

    def test_gate_passes_then_full_signoff_allowed(self):
        # 受控构造一个全绿内容集合：每层挑两条干净引用补 random 证据，
        # 使三类条件（估计上界、阻断关闭、层覆盖）同时成立后才允许签发。
        data = ProjectData.load(ROOT)
        random_refs = {r["reference"] for r in data.random_reviews(1)}
        universe = set(data.versions["V1-de-pc"]["references"])
        ledger_clean = __import__(
            "releaseqa.defects", fromlist=["build_ledger"]
        ).build_ledger(data, 1)
        from releaseqa.planning import eligible_reviewers

        def clean(rid: str) -> bool:
            return (
                rid not in random_refs
                and not ledger_clean.is_defective(rid, "minor")
                and bool(eligible_reviewers(data, [rid], "de"))
            )

        # 以不可拆剧情组在集合内的成员为最小选择单元，保证同进退；
        # 非锁定组的引用可单独选取。
        units: list[list[str]] = []
        assigned: set[str] = set()
        for group in data.groups.values():
            if not group["must_deliver_together"]:
                continue
            members = sorted(set(group["references"]) & universe)
            if members and all(clean(rid) for rid in members):
                units.append(members)
                assigned.update(members)
        for rid in sorted(universe - assigned):
            # 属于任何不可拆组的引用只能整组选取，不能作为单条候选。
            if any(
                group["must_deliver_together"]
                and rid in group["references"]
                for group in data.groups.values()
            ):
                continue
            if clean(rid):
                units.append([rid])

        chosen: list[str] = []
        strata_count: dict[str, int] = {}
        for unit in units:
            if all(strata_count.get(s, 0) >= 2 for s in ("critical", "dialogue", "ambient")):
                break
            chosen.extend(unit)
            for rid in unit:
                stratum = data.references[rid]["risk"]
                strata_count[stratum] = strata_count.get(stratum, 0) + 1
        self.assertEqual(set(strata_count), {"critical", "dialogue", "ambient"})
        self.assertTrue(all(v >= 2 for v in strata_count.values()))
        for rid in chosen:
            reviewer = eligible_reviewers(data, [rid], "de")[0]
            self.cli(
                "add-review", "--id", f"R-{rid}", "--reference", rid, "--version", "1",
                "--reviewer", reviewer, "--purpose", "random",
                "--inclusion-probability", "0.4", "--scope", "context",
                "--severity", "none", "--on", "2026-09-20",
            )
        extra = Store(self.state).reviews()
        gate = evaluate_gate(data, "V1-de-pc", extra_reviews=extra, subset=sorted(chosen))
        self.assertTrue(gate.passed, [c["detail"] for c in gate.checks if not c["passed"]])
        self.cli(
            "signoff", "--id", "S3", "--content-set", "V1-de-pc",
            "--decision", "release_qualified_subset",
            "--references", ",".join(sorted(chosen)), "--on", "2026-10-06",
        )
        signoff = Store(self.state).signoffs()[-1]
        self.assertEqual(signoff["decision"], "release_qualified_subset")
        self.assertEqual(sorted(signoff["references"]), sorted(chosen))

    def test_bundle_roundtrip_preserves_events(self):
        self.cli(
            "add-review", "--id", "B1", "--reference", "X099", "--version", "2",
            "--reviewer", "U06", "--purpose", "recheck", "--scope", "context",
            "--severity", "none", "--on", "2026-10-03",
        )
        bundle = self.state / "bundle.json"
        self.cli("export", "--out", str(bundle))
        other = self.state / "other"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            main(["--state", str(other), "import-bundle", "--bundle", str(bundle)])
        imported = json.loads(buf.getvalue())["imported"]
        self.assertEqual(imported["reviews"], 1)
        self.assertEqual(len(Store(other).reviews()), 1)

    def test_receipt_determines_online_state_and_withdrawal_scope(self):
        # 登记前 V2-de-pc 不在在线列表，撤回查询找不到它。
        before = self.cli("withdraw", "--item", "I001", "--severity", "critical")
        self.assertEqual(before["affected_online_sets"], [])
        # 回执登记后，同一集合成为在线版本，交付后缺陷能定位到它。
        self.cli(
            "receipt-register", "--id", "D1", "--content-set", "V2-de-pc",
            "--channel-state", "online", "--on", "2026-10-20",
        )
        receipts = self.cli("receipts")["receipts"]
        self.assertIn(
            ("V2-de-pc", "online"),
            [(r["content_set"], r["state"]) for r in receipts],
        )
        after = self.cli("withdraw", "--item", "I001", "--severity", "critical")
        affected = {a["content_set"]: a for a in after["affected_online_sets"]}
        self.assertIn("V2-de-pc", affected)
        self.assertEqual(sorted(affected["V2-de-pc"]["references"]), ["X001", "X181"])
        # 其他语种在线版本未使用该条目，明确保留。
        self.assertIn("V1-ja-pc", after["preserved_online_sets"])
        # 历史签发不被撤回查询改写。
        self.cli(
            "signoff", "--id", "S4", "--content-set", "V2-de-pc",
            "--decision", "hold_channel_version", "--on", "2026-10-21",
        )
        signoffs_before = list(Store(self.state).signoffs())
        self.cli("withdraw", "--item", "I001", "--severity", "critical")
        self.assertEqual(Store(self.state).signoffs(), signoffs_before)


if __name__ == "__main__":
    unittest.main()
