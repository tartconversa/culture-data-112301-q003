"""渠道签发闸门、签发记录与撤回查询。

一份内容集合（渠道 × 语种 × 版本 × 引用集合）可发布，必须同时满足：

1. 渠道声明必须覆盖的每个风险层都具备可外推证据；
2. 在渠道要求的置信水平上，重大缺陷暴露上界不超过渠道阈值；
3. 渠道禁止带出的严重度不存在「仍开启」的缺陷；
4. 被换版/时序替换失效的阻断级证据已完成定向复核（不能拿失效证据当合格）；
5. 选择的子集满足 ``must_deliver_together`` 剧情组的同进退约束。

平均质量不能掩盖关键分支缺证：任一层不可外推即整体不放行。
"""
from __future__ import annotations

from dataclasses import dataclass

from . import SEVERITY_ORDER
from .defects import build_ledger
from .estimation import estimate_strata, overall_exposure


@dataclass
class GateResult:
    content_set: str
    channel: str
    language: str
    version: int
    passed: bool
    checks: list[dict]
    strata: list[dict]
    overall: dict
    open_blockers: list[dict]
    voided_blockers: list[dict]
    group_constraints: list[dict]

    def to_dict(self) -> dict:
        return {
            "content_set": self.content_set,
            "channel": self.channel,
            "language": self.language,
            "version": self.version,
            "passed": self.passed,
            "checks": self.checks,
            "strata": self.strata,
            "overall": self.overall,
            "open_blockers": self.open_blockers,
            "voided_blockers": self.voided_blockers,
            "group_constraints": self.group_constraints,
        }


def evaluate_gate(
    data,
    content_set_id: str,
    *,
    extra_reviews: list[dict] | None = None,
    subset: list[str] | None = None,
) -> GateResult:
    content = data.versions[content_set_id]
    policy = data.policies[content["channel"]]
    universe = sorted(content["references"] if subset is None else subset)
    version = content["version"]

    ledger = build_ledger(data, version, extra_reviews=extra_reviews)
    estimates = estimate_strata(
        data,
        ledger,
        universe,
        min_severity="major",
        confidence=policy["confidence"],
        extra_reviews=extra_reviews,
    )
    strata_dicts = [e.to_dict() for e in estimates]
    estimate_by_stratum = {e.stratum: e for e in estimates}
    checks: list[dict] = []

    # 检查 1：必须覆盖的风险层具备可外推证据。
    missing_strata = [
        stratum
        for stratum in policy["required_strata"]
        if stratum not in estimate_by_stratum
        or not estimate_by_stratum[stratum].extrapolable
    ]
    checks.append(
        {
            "id": "required_strata_coverage",
            "passed": not missing_strata,
            "detail": (
                "所有必要风险层均有可外推证据"
                if not missing_strata
                else f"缺少可外推样本的风险层: {missing_strata}"
            ),
        }
    )

    # 检查 2：重大缺陷暴露的置信上界不超过渠道阈值。
    exposure_failures = []
    for stratum in policy["required_strata"]:
        estimate = estimate_by_stratum.get(stratum)
        if estimate is None or not estimate.extrapolable:
            continue
        if estimate.ci_high > policy["max_major_exposure"] + 1e-12:
            exposure_failures.append(
                {
                    "stratum": stratum,
                    "ci_high": round(estimate.ci_high, 6),
                    "limit": policy["max_major_exposure"],
                }
            )
    checks.append(
        {
            "id": "major_exposure_limit",
            "passed": not exposure_failures,
            "detail": (
                "各层重大缺陷暴露置信上界均在阈值内"
                if not exposure_failures
                else f"超出渠道阈值的层: {exposure_failures}"
            ),
        }
    )

    # 检查 3：禁止带出的严重度没有开启缺陷。
    block_level = SEVERITY_ORDER[policy["block_severity"]]
    universe_set = set(universe)
    open_blockers = []
    for finding in ledger.open_findings():
        targets = {finding.reference, *finding.propagated_to}
        hits = sorted(targets & universe_set)
        if hits and SEVERITY_ORDER[finding.severity] >= block_level:
            open_blockers.append(
                {
                    "reference": finding.reference,
                    "scope": finding.scope,
                    "severity": finding.severity,
                    "source_review": finding.source_review,
                    "affected_in_set": hits,
                }
            )
    checks.append(
        {
            "id": "block_severity_closed",
            "passed": not open_blockers,
            "detail": (
                "禁止带出严重度缺陷均已关闭"
                if not open_blockers
                else f"仍开启的阻断级缺陷: {[b['reference'] for b in open_blockers]}"
            ),
        }
    )

    # 检查 4：失效的阻断级证据必须已被新的适用证据替代。
    voided_blockers = []
    seen_voided: set[tuple[str, str]] = set()
    for finding in ledger.voided_blockers(policy["block_severity"]):
        if finding.reference not in universe_set:
            continue
        dedup_key = (finding.reference, finding.scope)
        if dedup_key in seen_voided:
            continue
        seen_voided.add(dedup_key)
        # 失效旧证据必须有新的适用复查替代，且复查结论为 none（问题解除）。
        if finding.resolved_by_review is None or finding.resolved_severity != "none":
            voided_blockers.append(
                {
                    "reference": finding.reference,
                    "scope": finding.scope,
                    "severity": finding.severity,
                    "source_review": finding.source_review,
                    "void_reason": finding.void_reason,
                    "resolved_by_review": finding.resolved_by_review,
                    "resolved_severity": finding.resolved_severity,
                }
            )
    checks.append(
        {
            "id": "voided_blocker_evidence_replaced",
            "passed": not voided_blockers,
            "detail": (
                "失效阻断证据均已有新的适用复核"
                if not voided_blockers
                else f"阻断级证据失效且尚无替代复核: {[b['reference'] for b in voided_blockers]}"
            ),
        }
    )

    # 检查 5：剧情组同进退约束（按内容集合内成员计算）。
    group_violations = []
    if subset is not None:
        chosen = set(subset)
        for gid, group in sorted(data.groups.items()):
            if not group["must_deliver_together"]:
                continue
            in_set = set(group["references"]) & set(content["references"])
            if in_set and in_set & chosen and not in_set.issubset(chosen):
                group_violations.append(
                    {
                        "group": gid,
                        "missing": sorted(in_set - chosen),
                        "included": sorted(in_set & chosen),
                    }
                )
    checks.append(
        {
            "id": "story_group_integrity",
            "passed": not group_violations,
            "detail": (
                "剧情组同进退约束满足"
                if not group_violations
                else f"违反不可拆交付的剧情组: {[v['group'] for v in group_violations]}"
            ),
        }
    )

    return GateResult(
        content_set=content_set_id,
        channel=content["channel"],
        language=content["language"],
        version=version,
        passed=all(check["passed"] for check in checks),
        checks=checks,
        strata=strata_dicts,
        overall=overall_exposure(estimates),
        open_blockers=open_blockers,
        voided_blockers=voided_blockers,
        group_constraints=group_violations,
    )


def deliverable_subset_options(data, content_set_id: str, extra_reviews: list[dict] | None) -> list[dict]:
    """负责人可选的「只发布满足条件的集合」候选。

    完整渠道闸门要求各风险层证据齐全，无法逐条通过；因此子集选择分两步：
    先按引用级条件（无开启阻断缺陷、无未替代的失效阻断证据）选出干净引用，
    再按不可拆剧情组做同进退闭包，最后对候选子集跑一次完整闸门确认。
    """
    content = data.versions[content_set_id]
    version = content["version"]
    full = set(content["references"])
    ledger = build_ledger(data, version, extra_reviews=extra_reviews)
    policy = data.policies[content["channel"]]
    block_level = SEVERITY_ORDER[policy["block_severity"]]

    defective: set[str] = set()
    for finding in ledger.open_findings():
        if SEVERITY_ORDER[finding.severity] >= block_level:
            defective.add(finding.reference)
            defective.update(finding.propagated_to)
    unresolved_voided: set[str] = set()
    for finding in ledger.voided_blockers(policy["block_severity"]):
        if finding.resolved_by_review is None or finding.resolved_severity != "none":
            unresolved_voided.add(finding.reference)

    clean = full - defective - unresolved_voided

    # 不可拆剧情组闭包：组内任一成员不干净，整组都不带出。
    chosen = set(clean)
    for group in data.groups.values():
        if not group["must_deliver_together"]:
            continue
        in_set = set(group["references"]) & full
        if in_set and not in_set.issubset(clean):
            chosen -= in_set

    if not chosen:
        return [{
            "references": [],
            "passes": False,
            "reason": "不存在同时满足阻断缺陷与剧情组完整性条件的可发布子集",
            "excluded": sorted(full),
        }]

    gate = evaluate_gate(
        data, content_set_id, extra_reviews=extra_reviews, subset=sorted(chosen)
    )
    return [
        {
            "references": sorted(chosen),
            "passes": gate.passed,
            "failing_checks": [c["id"] for c in gate.checks if not c["passed"]],
            "reason": (
                "满足引用级缺陷条件且不违反剧情组完整性的最大可发布子集；"
                "若仍有检查未过，说明子集内风险层证据不足，应继续抽检"
            ),
            "excluded": sorted(full - chosen),
        }
    ]


def channel_receipts(data, store) -> list[dict]:
    """实际渠道状态以接收回执为准；同一集合取最新回执。"""
    latest: dict[str, dict] = {}
    for receipt in data.receipts:
        latest[receipt["content_set"]] = receipt
    # 状态层登记的回执（后续渠道事实）覆盖资料表中的初始回执。
    for receipt in getattr(store, "delivery_receipts", lambda: [])():
        latest[receipt["content_set"]] = receipt
    for event in store.signoffs():
        # 签发本身不是上架事实；这里仅登记，状态仍等待回执。
        latest.setdefault(
            event["content_set"],
            {
                "id": None,
                "content_set": event["content_set"],
                "received_on": None,
                "state": "awaiting-receipt",
            },
        )
    return sorted(latest.values(), key=lambda r: r["content_set"])


def withdrawal_impact(
    data,
    item_id: str,
    severity: str,
    *,
    extra_reviews: list[dict] | None = None,
    store=None,
) -> dict:
    """交付后确认严重缺陷：列出仍在使用该内容的渠道与可选下架范围。

    内容缺陷沿同指纹条目传播；只报告已有在线回执的渠道版本，
    其他渠道的有效版本保留。历史签发记录不被修改。
    """
    refs_using = data.refs_for_item(item_id)
    receipt_rows = list(data.receipts)
    if store is not None:
        receipt_rows += getattr(store, "delivery_receipts", lambda: [])()
    online = {r["content_set"]: r for r in receipt_rows if r["state"] == "online"}
    affected_sets = []
    for set_id, receipt in sorted(online.items()):
        content = data.versions[set_id]
        hits = sorted(set(content["references"]) & set(refs_using))
        if not hits:
            continue
        # 该在线版本中该条目指纹是否仍是缺陷版本。
        fp_now = data.item_fingerprint_at(item_id, content["version"])
        affected_sets.append(
            {
                "content_set": set_id,
                "channel": content["channel"],
                "language": content["language"],
                "version": content["version"],
                "references": hits,
                "receipt": receipt["id"],
                "fingerprint_at_version": fp_now,
                "withdrawal_scope_options": [
                    {
                        "scope": "content_set",
                        "description": f"下架整个内容集合 {set_id}",
                    },
                    {
                        "scope": "affected_references",
                        "description": "仅撤回受影响引用对应内容，保留集合内其他内容",
                        "references": hits,
                    },
                ],
            }
        )

    # 其他仍在线、未使用该条目的渠道版本明确列出为可保留。
    preserved = [
        set_id
        for set_id in sorted(online)
        if set_id not in {a["content_set"] for a in affected_sets}
    ]
    return {
        "item": item_id,
        "confirmed_severity": severity,
        "affected_online_sets": affected_sets,
        "preserved_online_sets": preserved,
        "immutable_signoff_note": "撤回查询不修改历史签发；处置以新事件追加登记。",
    }
