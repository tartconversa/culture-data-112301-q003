"""资料装载与版本证据适用性。

核心概念
--------
每条审核记录针对一个引用（reference）与一个审核版本。引用指向的条目（item）
带有内容指纹；引用自身带有上下文指纹。版本之间：

* 条目出现在 ``changes`` 中且变更版本不晚于目标版本 → 内容指纹已变，
  针对**旧内容**的 ``content`` 作用域证据不再适用；
* 该变更的 ``invalid_contexts`` 列出受音轨/时序替换影响的上下文，
  落在这些上下文上的 ``context`` 作用域证据不再适用；
* 条目编号不变不构成继承理由，只有「内容指纹未变且适用条件一致」才可沿用。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import PURPOSE_RANDOM


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@dataclass(frozen=True)
class ApplicableReview:
    """一条在目标版本下仍适用的审核证据。"""

    review: dict
    applicable: bool
    reason: str  # applicable | item-changed | context-invalidated


@dataclass
class ProjectData:
    """装载后的合成资料，按主键建立索引。"""

    root: Path
    items: dict[str, dict]
    references: dict[str, dict]
    versions: dict[str, dict]
    reviews: list[dict]
    reviewers: dict[str, dict]
    groups: dict[str, dict]
    policies: dict[str, dict]
    changes: list[dict]
    tasks: list[dict]
    receipts: list[dict]

    @classmethod
    def load(cls, root: str | Path) -> "ProjectData":
        root = Path(root)
        data_dir = root / "data"
        items = {r["id"]: r for r in _read_jsonl(data_dir / "items.jsonl")}
        references = {r["id"]: r for r in _read_jsonl(data_dir / "references.jsonl")}
        versions = {r["id"]: r for r in _read_jsonl(data_dir / "versions.jsonl")}
        reviews = _read_jsonl(data_dir / "reviews.jsonl")
        reviewers = {r["id"]: r for r in _read_jsonl(data_dir / "reviewers.jsonl")}
        groups = {r["id"]: r for r in _read_jsonl(data_dir / "groups.jsonl")}
        policies = {r["id"]: r for r in _read_jsonl(data_dir / "channel_policies.jsonl")}
        changes = sorted(_read_jsonl(data_dir / "changes.jsonl"), key=lambda r: r["version"])
        tasks = _read_jsonl(data_dir / "task_history.jsonl")
        receipts = _read_jsonl(data_dir / "delivery_receipts.jsonl")
        return cls(
            root=root,
            items=items,
            references=references,
            versions=versions,
            reviews=reviews,
            reviewers=reviewers,
            groups=groups,
            policies=policies,
            changes=changes,
            tasks=tasks,
            receipts=receipts,
        )

    # ---- 版本指纹 -------------------------------------------------------
    def item_fingerprint_at(self, item_id: str, version: int) -> str:
        """条目在目标版本的有效指纹：依次应用不晚于该版本的变更。"""
        fingerprint = self.items[item_id]["fingerprint"]
        for change in self.changes:
            if change["item"] == item_id and change["version"] <= version:
                fingerprint = change["new_fingerprint"]
        return fingerprint

    def changed_items(self, version: int) -> set[str]:
        return {c["item"] for c in self.changes if c["version"] <= version}

    def invalid_contexts_at(self, version: int) -> set[str]:
        """截至目标版本累计被时序替换宣告失效的上下文集合。"""
        contexts: set[str] = set()
        for change in self.changes:
            if change["version"] <= version:
                contexts.update(change["invalid_contexts"])
        return contexts

    def context_invalidation_version(self, context: str) -> int | None:
        """上下文首次被时序替换失效的版本；从未失效返回 None。"""
        versions = [c["version"] for c in self.changes if context in c["invalid_contexts"]]
        return min(versions) if versions else None

    def item_change_version(self, item_id: str) -> int | None:
        versions = [c["version"] for c in self.changes if c["item"] == item_id]
        return min(versions) if versions else None

    # ---- 证据适用性 -----------------------------------------------------
    def review_applicability(self, review: dict, version: int) -> ApplicableReview:
        """判断一条审核记录在目标版本是否仍可作为证据。

        审核版本晚于目标版本时，该证据当时尚不存在 → 不可用于该版本；
        审核版本不晚于目标版本时，再按内容指纹与上下文适用性判断。
        """
        if review["version"] > version:
            return ApplicableReview(review, False, "review-from-later-version")
        ref = self.references[review["reference"]]
        item_id = ref["item"]
        item_change = self.item_change_version(item_id)
        if review["defect_scope"] == "content":
            # 内容证据只认内容指纹：审核早于条目换版 → 针对旧指纹，失效；
            # 审核晚于换版 → 针对新指纹，适用。编号不变不构成继承理由。
            if item_change is not None and review["version"] < item_change <= version:
                return ApplicableReview(review, False, "item-changed")
        else:
            # 上下文证据同理：换版前的旧观察随素材/时序替换失效；
            # 失效时点之后产生的新审核是针对新条件的证据。
            if item_change is not None and review["version"] < item_change <= version:
                return ApplicableReview(review, False, "item-changed")
            ctx_change = self.context_invalidation_version(ref["context"])
            if ctx_change is not None and review["version"] < ctx_change <= version:
                return ApplicableReview(review, False, "context-invalidated")
        return ApplicableReview(review, True, "applicable")

    def applicable_reviews(self, version: int) -> list[ApplicableReview]:
        return [self.review_applicability(r, version) for r in self.reviews]

    def refs_for_item(self, item_id: str) -> list[str]:
        return [rid for rid, ref in self.references.items() if ref["item"] == item_id]

    def item_of_ref(self, ref_id: str) -> str:
        return self.references[ref_id]["item"]

    def language_of_ref(self, ref_id: str) -> str:
        return self.items[self.references[ref_id]["item"]]["language"]

    def kind_of_ref(self, ref_id: str) -> str:
        return self.items[self.references[ref_id]["item"]]["kind"]

    def author_of_ref(self, ref_id: str) -> str:
        return self.items[self.references[ref_id]["item"]]["author"]

    def groups_of_ref(self, ref_id: str) -> list[str]:
        return [gid for gid, group in self.groups.items() if ref_id in group["references"]]

    def content_set(self, version_id: str) -> dict:
        return self.versions[version_id]

    def content_sets(self, language: str, version: int) -> list[dict]:
        return [
            v
            for v in self.versions.values()
            if v["language"] == language and v["version"] == version
        ]

    def random_reviews(self, version: int) -> list[dict]:
        """目标版本下仍适用、且保留概率抽样身份的抽检记录。

        复查合格不会抹掉原 random 记录的抽样身份；complaint/recheck
        本身不具备概率样本资格，永远不进入发生率估计。
        """
        out = []
        for ar in self.applicable_reviews(version):
            review = ar.review
            if ar.applicable and review["purpose"] == PURPOSE_RANDOM:
                if review["inclusion_probability"] is None:
                    continue
                out.append(review)
        return out
