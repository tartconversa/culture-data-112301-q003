"""下一轮审核计划与任务生命周期。

两条工作流严格区分
------------------
1. 随机补样（``random``）：为缺少可外推证据的风险层补足概率样本，
   计划保留设计纳入概率（资料中的现行设计为 0.4），产出可用于整层外推。
2. 定向核验（``directed``）：追查失效的阻断级证据、开启的严重缺陷与
   主动投诉，明确标记 ``extrapolable=false``，其结果不能冒充发生率。

约束
----
* 审核员须同时具备语种与内容类型资质，且不能复核自己参与翻译的条目；
* 剧情组必须成组查看，组任务按整组 ``audit_hours`` 计工时；
* 任务接受即占用工时；已接受任务不会被新投诉/换版静默取消，
  换版失效通过显式 ``invalidate`` 事件登记并说明重派范围；
* 已完成任务不退回工时。
"""
from __future__ import annotations

from dataclasses import dataclass

from . import (
    PURPOSE_COMPLAINT,
    PURPOSE_RANDOM,
    SEVERITY_ORDER,
    SOLO_AUDIT_HOURS,
)

# 新随机补样沿用资料中现行的层内等概率纳入设计。
DEFAULT_INCLUSION_PROBABILITY = 0.4
ACTIVE_STATES = {"proposed", "accepted"}


@dataclass
class PlannedTask:
    id: str
    purpose: str  # random | directed
    reason: str
    references: list[str]
    group: str | None
    version: int
    language: str
    hours: float
    inclusion_probability: float | None
    extrapolable: bool
    eligible_reviewers: list[str]
    assigned_reviewer: str | None
    unassignable_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "purpose": self.purpose,
            "reason": self.reason,
            "references": self.references,
            "group": self.group,
            "version": self.version,
            "language": self.language,
            "hours": self.hours,
            "inclusion_probability": self.inclusion_probability,
            "extrapolable": self.extrapolable,
            "eligible_reviewers": self.eligible_reviewers,
            "assigned_reviewer": self.assigned_reviewer,
            "unassignable_reason": self.unassignable_reason,
        }


def reviewer_remaining_hours(data, tasks: dict[str, dict]) -> dict[str, float]:
    """已接受（含已完成）的任务持续占用工时；退回/失效任务释放工时。"""
    used: dict[str, float] = {}
    for task in tasks.values():
        if task.get("state") in {"accepted", "completed"}:
            reviewer = task.get("reviewer")
            if reviewer:
                used[reviewer] = used.get(reviewer, 0.0) + float(task.get("hours", 0.0))
    return {
        rid: float(reviewer["hours"]) - used.get(rid, 0.0)
        for rid, reviewer in data.reviewers.items()
    }


def eligible_reviewers(data, ref_ids: list[str], language: str) -> list[str]:
    """资质 + 非自译双重过滤，返回按 id 排序的候选（保证排程可复现）。"""
    kinds = {data.kind_of_ref(rid) for rid in ref_ids}
    authors = {data.author_of_ref(rid) for rid in ref_ids}
    out = []
    for rid, reviewer in sorted(data.reviewers.items()):
        if language not in reviewer["languages"]:
            continue
        # 审核员的资质集合必须覆盖单元内全部内容类型。
        if not set(reviewer["kinds"]).issuperset(kinds):
            continue
        if rid in authors:
            continue
        out.append(rid)
    return out


def _group_units(data, refs: list[str], language: str) -> list[tuple[str | None, list[str]]]:
    """把引用折叠为审核单元：剧情组成组，其余为单条单元。

    剧情组跨语种构建，而计划按语种下达，因此只取组内属于该语种计划
    全集的成员；组内成组查看的要求对该子集仍然成立。
    """
    wanted = set(refs)
    grouped: dict[str, list[str]] = {}
    covered: set[str] = set()
    for gid, group in sorted(data.groups.items()):
        members = sorted(set(group["references"]) & wanted)
        if members:
            # 防御性校验：折叠进来的成员必须属于计划语种。
            members = [rid for rid in members if data.language_of_ref(rid) == language]
            if members:
                grouped[gid] = members
                covered.update(members)
    units: list[tuple[str | None, list[str]]] = [
        (gid, grouped[gid]) for gid in sorted(grouped)
    ]
    for rid in sorted(wanted - covered):
        units.append((None, [rid]))
    return units


def _unit_hours(data, group: str | None, unit_size: int) -> float:
    """组任务即使只审组内部分语种成员，也需通读整组上下文，按整组
    ``audit_hours`` 计；非组单条任务按默认逐条工时计。"""
    if group is not None:
        return float(data.groups[group]["audit_hours"])
    return SOLO_AUDIT_HOURS * unit_size


def _blocked_by_active_tasks(tasks: dict[str, dict], version: int) -> set[str]:
    blocked: set[str] = set()
    for task in tasks.values():
        if task.get("version") != version:
            continue
        if task.get("state") in ACTIVE_STATES:
            blocked.update(task.get("references", []))
    return blocked


def build_plan(
    data,
    ledger,
    estimates,
    language: str,
    version: int,
    tasks: dict[str, dict],
    *,
    inclusion_probability: float = DEFAULT_INCLUSION_PROBABILITY,
    extra_reviews: list[dict] | None = None,
) -> list[PlannedTask]:
    """生成下一周期审核计划（不写状态）。

    ``extra_reviews`` 为状态层后续到达的审核（新投诉、复查等），
    与资料表审核按同样的适用性规则参与定向候选判断。
    """
    extra_reviews = extra_reviews or []
    all_reviews = list(data.reviews) + list(extra_reviews)
    universe = sorted({
        rid
        for content in data.content_sets(language, version)
        for rid in content["references"]
    })
    blocked = _blocked_by_active_tasks(tasks, version)
    remaining_hours = reviewer_remaining_hours(data, tasks)
    # 已失效/退回任务覆盖的引用不在 blocked 中，因此会重新进入补样/定向候选。

    # ---- 定向核验候选 ---------------------------------------------------
    directed: dict[str, str] = {}
    block_level = SEVERITY_ORDER["critical"]
    # 0) 换版/时序替换使证据失效的引用：旧结论（即使是 none）已不可用，
    #    必须重新取得适用证据，否则该引用在新版本处于无证据状态。
    applicability = [data.review_applicability(r, version) for r in all_reviews]
    for ar in applicability:
        if not ar.applicable and ar.review["reference"] in universe:
            directed.setdefault(
                ar.review["reference"],
                f"历史证据因{ar.reason}失效，须重新定向核验",
            )
    # 1) 失效的阻断级证据：原发现严重度达到 critical 且证据已失效。
    for finding in ledger.voided_blockers("critical"):
        if finding.reference in universe:
            directed[finding.reference] = "阻断级缺陷证据随换版/时序替换失效，须定向复核"
    # 2) 仍然开启的 critical 缺陷。
    for finding in ledger.open_findings():
        if (
            finding.reference in universe
            and SEVERITY_ORDER[finding.severity] >= block_level
        ):
            directed.setdefault(finding.reference, "存在开启的阻断级缺陷")
        for rid in finding.propagated_to:
            if rid in universe and SEVERITY_ORDER[finding.severity] >= block_level:
                directed.setdefault(rid, "内容缺陷经共享条目传播至本引用")
    # 3) 主动投诉（投诉量不是发生率，只生成定向工作）。
    for review in all_reviews:
        if (
            review["purpose"] == PURPOSE_COMPLAINT
            and review["reference"] in universe
            and data.review_applicability(review, version).applicable
        ):
            directed.setdefault(review["reference"], "收到主动投诉，须定向核验")

    # ---- 随机补样候选：层内尚无足够可外推样本的未占用引用 ---------------
    strata_status = {e.stratum: e for e in estimates}
    random_needed: set[str] = set()
    for stratum, estimate in strata_status.items():
        if not estimate.extrapolable:
            for rid in universe:
                ref = data.references[rid]
                if ref["risk"] == stratum:
                    random_needed.add(rid)
    # 已在本版本有适用概率抽检的引用不重复抽（含状态层追加的抽检记录）。
    sampled = set()
    for review in all_reviews:
        ar = data.review_applicability(review, version)
        if (
            ar.applicable
            and review["purpose"] == PURPOSE_RANDOM
            and review["inclusion_probability"] is not None
            and review["reference"] in universe
        ):
            sampled.add(review["reference"])
    random_needed -= sampled

    def make_task(
        seq: int,
        purpose: str,
        reason: str,
        group: str | None,
        unit_refs: list[str],
    ) -> PlannedTask:
        hours = _unit_hours(data, group, len(unit_refs))
        candidates = eligible_reviewers(data, unit_refs, language)
        affordable = [rid for rid in candidates if remaining_hours.get(rid, 0.0) >= hours]
        assigned = affordable[0] if affordable else None
        unassignable = None
        if not candidates:
            unassignable = "无同时满足语种/类型资质且非自译的审核员"
        elif not affordable:
            unassignable = "候选审核员剩余工时不足"
        return PlannedTask(
            id=f"P{version}-{language}-{seq:03d}",
            purpose=purpose,
            reason=reason,
            references=unit_refs,
            group=group,
            version=version,
            language=language,
            hours=hours,
            inclusion_probability=(
                inclusion_probability if purpose == PURPOSE_RANDOM else None
            ),
            extrapolable=(purpose == PURPOSE_RANDOM),
            eligible_reviewers=candidates,
            assigned_reviewer=assigned,
            unassignable_reason=unassignable,
        )

    plan: list[PlannedTask] = []
    seq = 0

    # 定向优先（高风险先追查），按组折叠。
    directed_refs = sorted(rid for rid in directed if rid not in blocked)
    for group, unit_refs in _group_units(data, directed_refs, language):
        seq += 1
        reasons = sorted({directed[rid] for rid in unit_refs if rid in directed})
        plan.append(make_task(seq, "directed", "；".join(reasons), group, unit_refs))

    # 随机补样随后，同样按组折叠；与定向重复的引用不再排入。
    random_refs = sorted(rid for rid in random_needed if rid not in blocked and rid not in directed_refs)
    for group, unit_refs in _group_units(data, random_refs, language):
        seq += 1
        plan.append(
            make_task(
                seq,
                PURPOSE_RANDOM,
                "风险层概率样本不足，随机补样以取得可外推证据",
                group,
                unit_refs,
            )
        )

    return plan


def explain_invalidation(data, tasks: dict[str, dict], version: int) -> dict:
    """换版后说明受影响的已接受工作与需要重派的范围。"""
    changed_items = data.changed_items(version)
    invalid_contexts = data.invalid_contexts_at(version)
    affected: list[dict] = []
    for task in sorted(tasks.values(), key=lambda t: t["id"]):
        if task.get("version") >= version or task.get("state") != "accepted":
            continue
        hit_refs, reasons = [], set()
        for rid in task["references"]:
            ref = data.references[rid]
            if ref["item"] in changed_items:
                hit_refs.append(rid)
                reasons.add("条目内容指纹变化")
            elif ref["context"] in invalid_contexts:
                hit_refs.append(rid)
                reasons.add("上下文/时序证据失效")
        if hit_refs:
            affected.append(
                {
                    "task": task["id"],
                    "reviewer": task.get("reviewer"),
                    "state": task.get("state"),
                    "affected_references": sorted(hit_refs),
                    "reasons": sorted(reasons),
                    "redispatch_scope": sorted(hit_refs),
                    "hours": task.get("hours"),
                }
            )
    return {
        "version": version,
        "changed_items": sorted(changed_items),
        "invalid_contexts": sorted(invalid_contexts),
        "affected_accepted_tasks": affected,
    }
