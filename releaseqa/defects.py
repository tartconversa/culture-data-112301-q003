"""缺陷台账。

把适用的审核记录归约为每个 (引用, 作用域) 的当前缺陷状态，并处理两类传播：

* ``content`` 内容缺陷挂在条目内容指纹上，同一指纹的全部引用共同承担；
* ``context`` 上下文缺陷（截断、时序、上下文错误）只属于对应引用。

状态机
------
按 (审核日, 审核 id) 顺序处理同一 (引用, 作用域) 的记录：

* 严重度非 none 的记录开启或刷新一条缺陷；
* ``recheck`` 且严重度 none 才表示定向复查通过、缺陷关闭；
  普通抽检/投诉看到 none 不构成对已开启缺陷的关闭；
* 关闭后再次出现非 none 记录则重新开启。

失效证据（换版/上下文失效）单独列为 ``voided``：它既不能证明缺陷仍在，
也不能证明缺陷已消除。若失效的是阻断级（critical）发现，闸门按
「关键证据失效、必须定向复核」处理，而不是默认安全。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import (
    PURPOSE_RECHECK,
    SEVERITY_ORDER,
    SCOPE_CONTENT,
    SCOPE_CONTEXT,
)


@dataclass
class Finding:
    """一条当前可定位的缺陷事实。"""

    reference: str
    scope: str
    severity: str
    source_review: str
    source_purpose: str
    item: str
    context: str
    open: bool
    closed_by: str | None = None
    propagated_to: list[str] = field(default_factory=list)
    # 内容缺陷经同指纹条目传播时，原发现引用之外的引用。
    voided: bool = False
    void_reason: str | None = None
    # 失效后是否出现了新的适用复查（none 表示问题已解除，非 none 表示仍开启）。
    resolved_by_review: str | None = None
    resolved_severity: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.reference, self.scope)


@dataclass
class DefectLedger:
    version: int
    findings: list[Finding]
    voided_findings: list[Finding]
    by_key: dict[tuple[str, str], Finding]
    # 引用 -> 作用于该引用的开启缺陷（含传播）
    open_by_ref: dict[str, list[Finding]]

    def open_findings(self, reference: str | None = None) -> list[Finding]:
        if reference is None:
            return [f for f in self.findings if f.open]
        return self.open_by_ref.get(reference, [])

    def has_open_severity(self, min_severity: str) -> bool:
        level = SEVERITY_ORDER[min_severity]
        return any(
            SEVERITY_ORDER[f.severity] >= level
            for f in self.findings
            if f.open
        )

    def voided_blockers(self, block_severity: str) -> list[Finding]:
        level = SEVERITY_ORDER[block_severity]
        return [
            f
            for f in self.voided_findings
            if SEVERITY_ORDER[f.severity] >= level
        ]

    def is_defective(self, reference: str, min_severity: str = "minor") -> bool:
        level = SEVERITY_ORDER[min_severity]
        return any(
            SEVERITY_ORDER[f.severity] >= level
            for f in self.open_by_ref.get(reference, [])
        )

    def max_open_severity(self, reference: str) -> str | None:
        active = self.open_by_ref.get(reference, [])
        if not active:
            return None
        return max((f.severity for f in active), key=lambda s: SEVERITY_ORDER[s])


def build_ledger(data, version: int, extra_reviews: list[dict] | None = None) -> DefectLedger:
    """根据资料中的审核记录与状态层追加审核构建目标版本台账。

    ``extra_reviews`` 为本地状态中后续到达的审核（同一表结构），
    其适用性按同样规则判定。
    """
    applicability = {ar.review["id"]: ar for ar in data.applicable_reviews(version)}
    extra_reviews = extra_reviews or []
    for review in extra_reviews:
        applicability[review["id"]] = data.review_applicability(review, version)

    # (reference, scope) -> 按时间排序的适用记录
    chains: dict[tuple[str, str], list[dict]] = {}
    voided: list[Finding] = []
    for review in list(data.reviews) + list(extra_reviews):
        ar = applicability[review["id"]]
        ref = data.references[review["reference"]]
        if not ar.applicable:
            if review["severity"] != "none":
                voided.append(
                    Finding(
                        reference=review["reference"],
                        scope=review["defect_scope"],
                        severity=review["severity"],
                        source_review=review["id"],
                        source_purpose=review["purpose"],
                        item=ref["item"],
                        context=ref["context"],
                        open=False,
                        voided=True,
                        void_reason=ar.reason,
                    )
                )
            continue
        chains.setdefault((review["reference"], review["defect_scope"]), []).append(review)

    # 失效的旧发现若在目标版本已有新的适用复查，登记替代关系：
    # 复查为 none → 问题已解除；复查仍有严重度 → 问题在新版本仍开启。
    latest_applicable: dict[tuple[str, str], dict] = {}
    for key, chain in chains.items():
        chain.sort(key=lambda r: (r["reviewed_on"], r["id"]))
        latest_applicable[key] = chain[-1]
    for finding in voided:
        replacement = latest_applicable.get(finding.key)
        if replacement is not None and replacement["id"] != finding.source_review:
            finding.resolved_by_review = replacement["id"]
            finding.resolved_severity = replacement["severity"]

    findings: list[Finding] = []
    by_key: dict[tuple[str, str], Finding] = {}
    for key, chain in chains.items():
        chain.sort(key=lambda r: (r["reviewed_on"], r["id"]))
        current: Finding | None = None
        for review in chain:
            ref = data.references[review["reference"]]
            if review["severity"] == "none":
                if current is not None and current.open and review["purpose"] == PURPOSE_RECHECK:
                    current.open = False
                    current.closed_by = review["id"]
                continue
            current = Finding(
                reference=review["reference"],
                scope=review["defect_scope"],
                severity=review["severity"],
                source_review=review["id"],
                source_purpose=review["purpose"],
                item=ref["item"],
                context=ref["context"],
                open=True,
            )
        if current is not None:
            findings.append(current)
            by_key[key] = current

    # 内容缺陷沿同一条目（同指纹）传播到全部引用。
    open_by_ref: dict[str, list[Finding]] = {}
    for finding in findings:
        if finding.open and finding.scope == SCOPE_CONTENT:
            siblings = [
                rid
                for rid in data.refs_for_item(finding.item)
                if rid != finding.reference
            ]
            finding.propagated_to = siblings
        targets = [finding.reference]
        if finding.open and finding.scope == SCOPE_CONTENT:
            targets.extend(finding.propagated_to)
        if finding.open:
            for rid in targets:
                open_by_ref.setdefault(rid, []).append(finding)

    # 上下文缺陷只登记在自身引用。
    for finding in findings:
        if finding.scope == SCOPE_CONTEXT and finding.open:
            open_by_ref.setdefault(finding.reference, [])
            if finding not in open_by_ref[finding.reference]:
                open_by_ref[finding.reference].append(finding)

    return DefectLedger(
        version=version,
        findings=findings,
        voided_findings=voided,
        by_key=by_key,
        open_by_ref=open_by_ref,
    )


def scope_label(scope: str) -> str:
    return {SCOPE_CONTENT: "内容", SCOPE_CONTEXT: "上下文"}.get(scope, scope)
