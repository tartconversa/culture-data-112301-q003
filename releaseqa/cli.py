"""命令行入口：分析、排程、任务、签发、撤回与资料包。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import SCHEMA_VERSION
from .defects import build_ledger
from .estimation import estimate_strata, overall_exposure
from .model import ProjectData
from .planning import build_plan, explain_invalidation, reviewer_remaining_hours
from .release import (
    channel_receipts,
    deliverable_subset_options,
    evaluate_gate,
    withdrawal_impact,
)
from .store import Store

ROOT = Path(__file__).resolve().parents[1]


def _print(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False))


def _parse_refs(spec: str) -> list[str]:
    """支持逗号分隔列表或 @文件路径（每行一个引用）。"""
    if spec.startswith("@"):
        return [
            line.strip()
            for line in Path(spec[1:]).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    return [part.strip() for part in spec.split(",") if part.strip()]


def _load(root: Path, state: Path):
    data = ProjectData.load(root)
    store = Store(state)
    return data, store


def cmd_analyze(args) -> None:
    data, store = _load(Path(args.root), Path(args.state))
    extra = store.reviews()
    ledger = build_ledger(data, args.version, extra_reviews=extra)
    universe = sorted({
        rid
        for content in data.content_sets(args.language, args.version)
        for rid in content["references"]
    })
    policy = data.policies[args.channel]
    estimates = estimate_strata(
        data, ledger, universe,
        min_severity="major", confidence=policy["confidence"],
        extra_reviews=extra,
    )
    payload = {
        "schema": SCHEMA_VERSION,
        "language": args.language,
        "version": args.version,
        "universe_refs": len(universe),
        "strata": [e.to_dict() for e in estimates],
        "overall_major_exposure": overall_exposure(estimates),
        "open_findings": [
            {
                "reference": f.reference,
                "scope": f.scope,
                "severity": f.severity,
                "source_review": f.source_review,
                "source_purpose": f.source_purpose,
                "propagated_to": f.propagated_to,
            }
            for f in ledger.open_findings()
            if f.reference in universe
        ],
        "voided_findings": [
            {
                "reference": f.reference,
                "scope": f.scope,
                "severity": f.severity,
                "source_review": f.source_review,
                "void_reason": f.void_reason,
            }
            for f in ledger.voided_findings
            if f.reference in universe
        ],
        "method_notes": [
            "发生率仅由 purpose=random 且带纳入概率的适用记录估计",
            "complaint/recheck 为定向来源，单独列示，不参与发生率外推",
            "复查合格不改变原抽检记录的抽样身份",
            "不可外推层不给点估计，避免用平均质量掩盖关键分支缺证",
        ],
    }
    _print(payload)


def cmd_plan(args) -> None:
    data, store = _load(Path(args.root), Path(args.state))
    extra = store.reviews()
    ledger = build_ledger(data, args.version, extra_reviews=extra)
    universe = sorted({
        rid
        for content in data.content_sets(args.language, args.version)
        for rid in content["references"]
    })
    policy = data.policies[args.channel]
    estimates = estimate_strata(
        data, ledger, universe,
        min_severity="major", confidence=policy["confidence"],
        extra_reviews=extra,
    )
    tasks = _all_tasks(data, store)
    plan = build_plan(data, ledger, estimates, args.language, args.version, tasks,
                      extra_reviews=extra)
    committed = []
    if args.commit:
        existing = set(store.tasks())
        for task in plan:
            if task.id in existing:
                continue
            event = {
                "id": task.id,
                "at": args.on,
                "action": "propose",
                "reviewer": task.assigned_reviewer,
                "references": task.references,
                "version": task.version,
                "purpose": task.purpose,
                "hours": task.hours,
                "group": task.group,
                "inclusion_probability": task.inclusion_probability,
                "extrapolable": task.extrapolable,
                "reason": task.reason,
                "eligible_reviewers": task.eligible_reviewers,
                "unassignable_reason": task.unassignable_reason,
            }
            store.add_task_event(event)
            committed.append(task.id)
    payload = {
        "language": args.language,
        "version": args.version,
        "remaining_hours": reviewer_remaining_hours(data, tasks),
        "tasks": [t.to_dict() for t in plan],
        "committed": committed,
        "active_task_refs": sorted(
            {rid for t in tasks.values() if t.get("state") in {"proposed", "accepted"} for rid in t.get("references", [])}
        ),
        "notes": [
            "random 任务保留设计纳入概率，结果可外推",
            "directed 任务标记 extrapolable=false，不能用于整层发生率",
            "已接受任务不会被新投诉或换版静默取消",
        ],
    }
    _print(payload)


def cmd_task_accept(args) -> None:
    data, store = _load(Path(args.root), Path(args.state))
    tasks = _all_tasks(data, store)
    if args.task not in tasks:
        raise SystemExit(f"任务不存在: {args.task}")
    task = tasks[args.task]
    if task.get("state") != "proposed":
        raise SystemExit(f"任务状态为 {task.get('state')}，仅 proposed 可接受")
    reviewer = args.reviewer
    if reviewer is None:
        reviewer = task.get("reviewer")
    if not reviewer:
        raise SystemExit("该任务无可指派审核员，需先解决资质/工时缺口")
    remaining = reviewer_remaining_hours(data, tasks)
    if remaining.get(reviewer, 0.0) < task["hours"]:
        raise SystemExit(f"审核员 {reviewer} 剩余工时 {remaining.get(reviewer)} 不足 {task['hours']}")
    event = {
        "id": args.task, "at": args.on, "action": "accept",
        "reviewer": reviewer, "hours": task["hours"],
    }
    store.add_task_event(event)
    _print({"accepted": args.task, "reviewer": reviewer, "hours": task["hours"],
            "remaining_hours": reviewer_remaining_hours(data, _all_tasks(data, store))})


def cmd_task_complete(args) -> None:
    data, store = _load(Path(args.root), Path(args.state))
    tasks = _all_tasks(data, store)
    if args.task not in tasks or tasks[args.task].get("state") != "accepted":
        raise SystemExit("仅 accepted 任务可登记完成")
    store.add_task_event({"id": args.task, "at": args.on, "action": "complete"})
    _print({"completed": args.task, "note": "已完成任务不退回工时"})


def cmd_task_return(args) -> None:
    data, store = _load(Path(args.root), Path(args.state))
    tasks = _all_tasks(data, store)
    if args.task not in tasks or tasks[args.task].get("state") != "accepted":
        raise SystemExit("仅 accepted 任务可退回")
    task = tasks[args.task]
    store.add_task_event(
        {"id": args.task, "at": args.on, "action": "return", "reason": args.reason}
    )
    _print({
        "returned": args.task,
        "released_hours": task["hours"],
        "redispatch_references": task["references"],
        "reason": args.reason,
        "remaining_hours": reviewer_remaining_hours(data, _all_tasks(data, store)),
    })


def _all_tasks(data, store) -> dict[str, dict]:
    """合并资料表中的历史工单与状态层任务事件（状态层优先回放）。"""
    merged = {
        task["id"]: {
            "id": task["id"],
            "reviewer": task["reviewer"],
            "references": list(task["references"]),
            "version": task["version"],
            "purpose": task["purpose"],
            "hours": task["hours"],
            "state": task["state"],
            "accepted_on": task.get("accepted_on"),
            "history": [{"id": task["id"], "action": "seed", "at": task.get("accepted_on")}],
        }
        for task in data.tasks
    }
    merged.update(store.tasks())
    return merged


def cmd_invalidate(args) -> None:
    data, store = _load(Path(args.root), Path(args.state))
    tasks = _all_tasks(data, store)
    explanation = explain_invalidation(data, tasks, args.version)
    invalidated = []
    known_event_tasks = set(store.tasks())
    for item in explanation["affected_accepted_tasks"]:
        tid = item["task"]
        if tasks[tid].get("state") != "accepted":
            continue
        event = {
            "id": tid, "at": args.on, "action": "invalidate",
            "reason": "；".join(item["reasons"]),
            "redispatch_scope": item["redispatch_scope"],
        }
        if tid in known_event_tasks:
            store.add_task_event(event)
        else:
            # 资料表中的历史工单首次进入状态管理：先补登记再记失效，保留轨迹。
            seed = tasks[tid]
            store.add_task_event({
                "id": tid, "at": seed.get("accepted_on") or args.on,
                "action": "propose", "reviewer": seed["reviewer"],
                "references": seed["references"], "version": seed["version"],
                "purpose": seed["purpose"], "hours": seed["hours"],
            })
            store.add_task_event({
                "id": tid, "at": seed.get("accepted_on") or args.on,
                "action": "accept", "reviewer": seed["reviewer"],
                "hours": seed["hours"],
            })
            store.add_task_event(event)
        invalidated.append(tid)
    explanation["invalidated_tasks"] = invalidated
    explanation["note"] = "仅登记失效并释放未完成任务工时；重派范围已列出，历史事件保留"
    _print(explanation)


def cmd_add_review(args) -> None:
    data, store = _load(Path(args.root), Path(args.state))
    if args.reference not in data.references:
        raise SystemExit(f"引用不存在: {args.reference}")
    if args.reviewer not in data.reviewers:
        raise SystemExit(f"审核员不存在: {args.reviewer}")
    ref = data.references[args.reference]
    language = data.language_of_ref(args.reference)
    kind = data.kind_of_ref(args.reference)
    reviewer = data.reviewers[args.reviewer]
    if language not in reviewer["languages"] or kind not in reviewer["kinds"]:
        raise SystemExit("审核员不具备该语种/内容类型资质")
    if data.author_of_ref(args.reference) == args.reviewer:
        raise SystemExit("审核员不能复核自己参与翻译的条目")
    if args.purpose == "random" and args.inclusion_probability is None:
        raise SystemExit("random 概率抽检必须提供 --inclusion-probability")
    if args.purpose != "random" and args.inclusion_probability is not None:
        raise SystemExit("complaint/recheck 为定向来源，不能携带纳入概率")
    event = {
        "id": args.id,
        "reference": args.reference,
        "version": args.version,
        "reviewer": args.reviewer,
        "purpose": args.purpose,
        "inclusion_probability": args.inclusion_probability,
        "defect_scope": args.scope,
        "severity": args.severity,
        "reviewed_on": args.on,
    }
    store.add_review(event)
    _print({"added_review": event})


def cmd_gate(args) -> None:
    data, store = _load(Path(args.root), Path(args.state))
    extra = store.reviews()
    result = evaluate_gate(data, args.content_set, extra_reviews=extra)
    _print(result.to_dict())


def cmd_options(args) -> None:
    data, store = _load(Path(args.root), Path(args.state))
    _print({
        "content_set": args.content_set,
        "subset_options": deliverable_subset_options(data, args.content_set, store.reviews()),
        "decisions": ["continue_sampling", "release_qualified_subset", "hold_channel_version"],
    })


def cmd_signoff(args) -> None:
    data, store = _load(Path(args.root), Path(args.state))
    extra = store.reviews()
    subset = None
    subset_option = None
    if args.decision == "release_qualified_subset":
        if args.references:
            subset = sorted(_parse_refs(args.references))
            unknown = [rid for rid in subset if rid not in data.versions[args.content_set]["references"]]
            if unknown:
                raise SystemExit(f"引用不属于该内容集合: {unknown}")
        else:
            # 未显式指定时，给出系统计算的最大可发布子集候选。
            options = deliverable_subset_options(data, args.content_set, extra)
            subset_option = options[0]
            if not subset_option["references"]:
                raise SystemExit("不存在满足引用级条件与剧情组完整性的可发布子集，不能签发")
            subset = subset_option["references"]
    gate = evaluate_gate(data, args.content_set, extra_reviews=extra, subset=subset)
    if args.decision == "release_qualified_subset":
        # 三类条件必须同时成立：风险估计合格、严重问题关闭、必要证据覆盖；
        # 剧情组完整性是子集选择自身的约束，同样不得违反。
        if not gate.passed:
            failed = [c["id"] for c in gate.checks if not c["passed"]]
            raise SystemExit(
                f"子集未同时满足全部渠道条件: {failed}；可继续抽检或暂缓整个渠道版本"
            )
    elif args.decision == "continue_sampling" and gate.passed:
        raise SystemExit("闸门已全部通过，无需以继续抽检名义暂缓")
    event = {
        "id": args.id,
        "at": args.on,
        "content_set": args.content_set,
        "channel": gate.channel,
        "language": gate.language,
        "version": gate.version,
        "decision": args.decision,
        "references": sorted(
            subset if subset is not None else data.versions[args.content_set]["references"]
        ),
        "gate": gate.to_dict(),
        "subset_option": subset_option,
        "snapshot": {
            "open_blocker_count": len(gate.open_blockers),
            "voided_blocker_count": len(gate.voided_blockers),
            "overall": gate.overall,
        },
    }
    store.add_signoff(event)
    _print({"signed_off": event["id"], "immutable": True, "decision": args.decision,
            "outstanding_checks": [c["id"] for c in gate.checks if not c["passed"]]})


def cmd_receipt_register(args) -> None:
    data, store = _load(Path(args.root), Path(args.state))
    if args.content_set not in data.versions:
        raise SystemExit(f"内容集合不存在: {args.content_set}")
    receipt = {
        "id": args.id,
        "content_set": args.content_set,
        "received_on": args.on,
        "state": args.channel_state,
    }
    store.add_receipt(receipt)
    _print({"registered_receipt": receipt,
            "note": "回执是实际上架事实；同一集合的最新回执决定渠道状态"})


def cmd_receipts(args) -> None:
    data, store = _load(Path(args.root), Path(args.state))
    _print({"receipts": channel_receipts(data, store)})


def cmd_withdraw(args) -> None:
    data, store = _load(Path(args.root), Path(args.state))
    _print(withdrawal_impact(
        data, args.item, args.severity,
        extra_reviews=store.reviews(), store=store,
    ))


def cmd_export(args) -> None:
    _, store = _load(Path(args.root), Path(args.state))
    path = store.export_bundle(args.out, {"source_root": str(args.root)})
    _print({"exported": str(path)})


def cmd_import(args) -> None:
    _, store = _load(Path(args.root), Path(args.state))
    _print({"imported": store.import_bundle(args.bundle)})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="releaseqa", description="本地化版本质量证据与签发应用")
    parser.add_argument("--root", default=str(ROOT), help="项目根目录（含 data/）")
    parser.add_argument("--state", default=str(ROOT / "state"), help="本地状态目录")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("analyze", help="估计语种版本各风险层缺陷暴露")
    p.add_argument("--language", required=True)
    p.add_argument("--version", type=int, required=True)
    p.add_argument("--channel", default="pc")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("plan", help="生成下一周期审核计划；--commit 将提议任务写入状态")
    p.add_argument("--language", required=True)
    p.add_argument("--version", type=int, required=True)
    p.add_argument("--channel", default="pc")
    p.add_argument("--commit", action="store_true")
    p.add_argument("--on", default="2026-10-01")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("task-accept", help="接受审核任务并占用工时")
    p.add_argument("--task", required=True)
    p.add_argument("--reviewer")
    p.add_argument("--on", required=True)
    p.set_defaults(func=cmd_task_accept)

    p = sub.add_parser("task-complete", help="登记任务完成")
    p.add_argument("--task", required=True)
    p.add_argument("--on", required=True)
    p.set_defaults(func=cmd_task_complete)

    p = sub.add_parser("task-return", help="退回任务并解释重派范围")
    p.add_argument("--task", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--on", required=True)
    p.set_defaults(func=cmd_task_return)

    p = sub.add_parser("invalidate", help="换版后登记已接受任务的失效与重派范围")
    p.add_argument("--version", type=int, required=True)
    p.add_argument("--on", required=True)
    p.set_defaults(func=cmd_invalidate)

    p = sub.add_parser("add-review", help="追加审核记录（含资质与自译校验）")
    p.add_argument("--id", required=True)
    p.add_argument("--reference", required=True)
    p.add_argument("--version", type=int, required=True)
    p.add_argument("--reviewer", required=True)
    p.add_argument("--purpose", choices=["random", "complaint", "recheck"], required=True)
    p.add_argument("--inclusion-probability", dest="inclusion_probability", type=float)
    p.add_argument("--scope", choices=["content", "context"], required=True)
    p.add_argument("--severity", choices=["none", "minor", "major", "critical"], required=True)
    p.add_argument("--on", required=True)
    p.set_defaults(func=cmd_add_review)

    p = sub.add_parser("gate", help="评估内容集合的渠道签发闸门")
    p.add_argument("--content-set", required=True)
    p.set_defaults(func=cmd_gate)

    p = sub.add_parser("options", help="给出继续抽检/子集发布/暂缓的可选方案")
    p.add_argument("--content-set", required=True)
    p.set_defaults(func=cmd_options)

    p = sub.add_parser("signoff", help="登记不可变签发决策")
    p.add_argument("--id", required=True)
    p.add_argument("--content-set", required=True)
    p.add_argument("--decision",
                   choices=["continue_sampling", "release_qualified_subset", "hold_channel_version"],
                   required=True)
    p.add_argument("--references",
                   help="release_qualified_subset 时显式指定交付引用（逗号分隔或 @文件）")
    p.add_argument("--on", required=True)
    p.set_defaults(func=cmd_signoff)

    p = sub.add_parser("receipts", help="查询渠道接收回执与实际上架状态")
    p.set_defaults(func=cmd_receipts)

    p = sub.add_parser("receipt-register", help="登记渠道接收回执（实际上架事实）")
    p.add_argument("--id", required=True)
    p.add_argument("--content-set", required=True)
    p.add_argument("--channel-state", dest="channel_state", required=True,
                   choices=["online", "withdrawn", "rejected"])
    p.add_argument("--on", required=True)
    p.set_defaults(func=cmd_receipt_register)

    p = sub.add_parser("withdraw", help="交付后严重缺陷的影响面与下架范围查询")
    p.add_argument("--item", required=True)
    p.add_argument("--severity", required=True)
    p.set_defaults(func=cmd_withdraw)

    p = sub.add_parser("export", help="导出可带入后续版本的状态资料包")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("import-bundle", help="导入后续版本的状态资料包")
    p.add_argument("--bundle", required=True)
    p.set_defaults(func=cmd_import)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
