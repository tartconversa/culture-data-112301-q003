"""风险层缺陷暴露估计与不确定性。

口径（与资料契约一致）
----------------------
* 只有 ``purpose == "random"`` 且带 ``inclusion_probability``、在目标版本仍
  适用的记录具备概率样本资格；投诉与定向复查不能用于估计整层发生率。
* 复查合格不会删除原抽检记录的抽样身份：抽样基数保留，缺陷是否开启由
  :mod:`releaseqa.defects` 的台账状态决定。
* 估计量为风险层内「缺陷引用的受众暴露占比」，使用 Horvitz-Thompson
  加权（权重为纳入概率的倒数），对受众权重与不等概率纳入同时成立。
* 不确定性用固定种子的分层加权自助法给出百分位置信区间；无概率样本的层
  标记为 ``extrapolable = false``，只返回已知事实，不外推。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from . import (
    BOOTSTRAP_REPLICATES,
    MIN_EXTRAPOLABLE_SAMPLE,
    PURPOSE_COMPLAINT,
    PURPOSE_RECHECK,
    SEVERITY_ORDER,
)


@dataclass
class StratumEstimate:
    stratum: str
    universe_refs: int
    universe_weight: float
    sampled_refs: int
    sampled_weight: float
    inclusion_probabilities: list[float]
    defective_sampled: int
    # 观测到的缺陷暴露权重（仅样本内，未经外推）
    observed_defect_weight: float
    point: float | None
    ci_low: float | None
    ci_high: float | None
    extrapolable: bool
    reason: str
    wilson_low: float | None = None
    wilson_high: float | None = None
    complaint_refs: list[str] = field(default_factory=list)
    recheck_refs: list[str] = field(default_factory=list)
    voided_refs: list[str] = field(default_factory=list)
    open_severities: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "stratum": self.stratum,
            "universe_refs": self.universe_refs,
            "universe_weight": round(self.universe_weight, 6),
            "sampled_refs": self.sampled_refs,
            "sampled_weight": round(self.sampled_weight, 6),
            "inclusion_probabilities": self.inclusion_probabilities,
            "defective_sampled": self.defective_sampled,
            "observed_defect_weight": round(self.observed_defect_weight, 6),
            "point": None if self.point is None else round(self.point, 6),
            "ci_low": None if self.ci_low is None else round(self.ci_low, 6),
            "ci_high": None if self.ci_high is None else round(self.ci_high, 6),
            "wilson_low": self.wilson_low,
            "wilson_high": self.wilson_high,
            "extrapolable": self.extrapolable,
            "reason": self.reason,
            "complaint_refs": self.complaint_refs,
            "recheck_refs": self.recheck_refs,
            "voided_refs": self.voided_refs,
            "open_severities": self.open_severities,
        }


def _wilson(successes: int, n: int, confidence: float) -> tuple[float, float]:
    """无权重二项 Wilson 区间，作为辅助交叉核对。"""
    if n == 0:
        return (0.0, 0.0)
    z = {0.90: 1.6448536269514722, 0.95: 1.959963984540054, 0.99: 2.5758293035489004}.get(
        confidence, 1.959963984540054
    )
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def estimate_strata(
    data,
    ledger,
    universe_refs: list[str],
    *,
    min_severity: str = "minor",
    confidence: float = 0.95,
    seed: int = 20261123,
    extra_reviews: list[dict] | None = None,
) -> list[StratumEstimate]:
    """对给定引用全集按风险层分别估计缺陷暴露占比。"""
    extra_reviews = extra_reviews or []
    all_reviews = list(data.reviews) + list(extra_reviews)
    applicable_map = {r["id"]: data.review_applicability(r, ledger.version) for r in all_reviews}

    # 引用 -> 适用的概率抽检
    random_by_ref: dict[str, dict] = {}
    complaint_refs: set[str] = set()
    recheck_refs: set[str] = set()
    voided_refs: set[str] = set()
    for review in all_reviews:
        ar = applicable_map[review["id"]]
        rid = review["reference"]
        if rid not in universe_refs:
            continue
        if not ar.applicable:
            # 只有换版/时序替换造成的失效才计入「证据缺口」；
            # 晚于目标版本的审核只是当时尚不存在，不属于本版本的失效证据。
            if ar.reason != "review-from-later-version":
                voided_refs.add(rid)
            continue
        if review["purpose"] == "random" and review["inclusion_probability"] is not None:
            random_by_ref.setdefault(rid, review)
        elif review["purpose"] == PURPOSE_COMPLAINT:
            complaint_refs.add(rid)
        elif review["purpose"] == PURPOSE_RECHECK:
            recheck_refs.add(rid)

    strata_names = sorted({data.references[rid]["risk"] for rid in universe_refs})
    rng = random.Random(seed)
    level = SEVERITY_ORDER[min_severity]
    results: list[StratumEstimate] = []

    for stratum in strata_names:
        members = [rid for rid in universe_refs if data.references[rid]["risk"] == stratum]
        weights = {rid: float(data.references[rid]["audience_weight"]) for rid in members}
        universe_weight = sum(weights.values())
        sampled = [rid for rid in members if rid in random_by_ref]
        probs = [random_by_ref[rid]["inclusion_probability"] for rid in sampled]

        defective = [rid for rid in sampled if ledger.is_defective(rid, min_severity)]
        sev_counter: dict[str, int] = {}
        for rid in sampled:
            top = ledger.max_open_severity(rid)
            if top is not None and SEVERITY_ORDER[top] >= level:
                sev_counter[top] = sev_counter.get(top, 0) + 1

        # Horvitz-Thompson：分析权重 = 受众权重 / 纳入概率。
        ht = {
            rid: weights[rid] / random_by_ref[rid]["inclusion_probability"]
            for rid in sampled
        }
        observed_defect_weight = sum(
            weights[rid] for rid in defective
        )

        if len(sampled) < MIN_EXTRAPOLABLE_SAMPLE:
            results.append(
                StratumEstimate(
                    stratum=stratum,
                    universe_refs=len(members),
                    universe_weight=universe_weight,
                    sampled_refs=len(sampled),
                    sampled_weight=sum(weights[rid] for rid in sampled),
                    inclusion_probabilities=probs,
                    defective_sampled=len(defective),
                    observed_defect_weight=observed_defect_weight,
                    point=None,
                    ci_low=None,
                    ci_high=None,
                    extrapolable=False,
                    reason="概率样本不足，缺少可外推样本",
                    complaint_refs=sorted(complaint_refs & set(members)),
                    recheck_refs=sorted(recheck_refs & set(members)),
                    voided_refs=sorted(voided_refs & set(members)),
                    open_severities=sev_counter,
                )
            )
            continue

        def ratio(replica: list[str]) -> float:
            denom = sum(ht[rid] for rid in replica)
            numer = sum(ht[rid] for rid in replica if ledger.is_defective(rid, min_severity))
            return numer / denom if denom > 0 else 0.0

        point = ratio(sampled)
        replicates = [
            ratio(rng.choices(sampled, k=len(sampled)))
            for _ in range(BOOTSTRAP_REPLICATES)
        ]
        replicates.sort()
        tail = (1 - confidence) / 2
        ci_low = replicates[int(tail * BOOTSTRAP_REPLICATES)]
        ci_high = replicates[min(
            BOOTSTRAP_REPLICATES - 1,
            int((1 - tail) * BOOTSTRAP_REPLICATES),
        )]
        w_low, w_high = _wilson(len(defective), len(sampled), confidence)

        results.append(
            StratumEstimate(
                stratum=stratum,
                universe_refs=len(members),
                universe_weight=universe_weight,
                sampled_refs=len(sampled),
                sampled_weight=sum(weights[rid] for rid in sampled),
                inclusion_probabilities=probs,
                defective_sampled=len(defective),
                observed_defect_weight=observed_defect_weight,
                point=point,
                ci_low=ci_low,
                ci_high=ci_high,
                extrapolable=True,
                reason="基于概率抽检的加权估计",
                wilson_low=round(w_low, 6),
                wilson_high=round(w_high, 6),
                complaint_refs=sorted(complaint_refs & set(members)),
                recheck_refs=sorted(recheck_refs & set(members)),
                voided_refs=sorted(voided_refs & set(members)),
                open_severities=sev_counter,
            )
        )

    return results


def overall_exposure(estimates: list[StratumEstimate]) -> dict:
    """汇总可外推层；任一层不可外推则整体不给出单一比率，避免平均掩盖缺证。"""
    usable = [e for e in estimates if e.extrapolable]
    missing = [e.stratum for e in estimates if not e.extrapolable]
    if not usable:
        return {"point": None, "ci_low": None, "ci_high": None, "missing_strata": missing}
    weight = sum(e.universe_weight for e in usable)
    point = sum(e.point * e.universe_weight for e in usable) / weight
    ci_low = sum(e.ci_low * e.universe_weight for e in usable) / weight
    ci_high = sum(e.ci_high * e.universe_weight for e in usable) / weight
    return {
        "point": round(point, 6),
        "ci_low": round(ci_low, 6),
        "ci_high": round(ci_high, 6),
        "missing_strata": missing,
    }
