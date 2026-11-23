"""本地应用状态：追加式 JSONL。

设计原则
--------
* 资料目录 ``data/`` 只读，所有应用产出写入 ``state/``（已被 Git 忽略）。
* 事件只追加、不就地改写；当前视图由事件回放得到，保留完整审计轨迹。
* 审核事件、任务事件、签发事件分文件存放，id 由调用方保证唯一并做重复校验。
* 签发一旦写入即不可变；更正后的统计以新事件追加，过去的签发记录保持原样。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

REVIEWS_FILE = "review_events.jsonl"
TASKS_FILE = "task_events.jsonl"
SIGNOFFS_FILE = "signoff_events.jsonl"
RECEIPTS_FILE = "receipt_events.jsonl"


class Store:
    def __init__(self, state_dir: str | Path):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        return self.dir / name

    def _append(self, name: str, event: dict, key: str = "id") -> None:
        path = self._path(name)
        existing = {row[key] for row in self._read(name)}
        if event[key] in existing:
            raise ValueError(f"{name} 中主键重复: {event[key]}")
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")

    def _read(self, name: str) -> list[dict]:
        path = self._path(name)
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    # ---- 审核事件 -------------------------------------------------------
    def add_review(self, review: dict) -> dict:
        required = {
            "id", "reference", "version", "reviewer", "purpose",
            "inclusion_probability", "defect_scope", "severity", "reviewed_on",
        }
        missing = required - set(review)
        if missing:
            raise ValueError(f"审核事件缺少字段: {sorted(missing)}")
        self._append(REVIEWS_FILE, review)
        return review

    def reviews(self) -> list[dict]:
        return self._read(REVIEWS_FILE)

    # ---- 任务事件 -------------------------------------------------------
    def add_task_event(self, event: dict) -> dict:
        required = {"id", "at", "action"}
        missing = required - set(event)
        if missing:
            raise ValueError(f"任务事件缺少字段: {sorted(missing)}")
        # 同一任务 id 的事件按追加顺序回放，主键在文件级允许重复（不同动作）。
        path = self._path(TASKS_FILE)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        return event

    def task_events(self) -> list[dict]:
        return self._read(TASKS_FILE)

    def tasks(self) -> dict[str, dict]:
        """回放任务事件得到当前任务视图。"""
        view: dict[str, dict] = {}
        for event in self.task_events():
            tid = event["id"]
            if event["action"] == "propose":
                view[tid] = {
                    "id": tid,
                    "reviewer": event.get("reviewer"),
                    "references": list(event.get("references", [])),
                    "version": event.get("version"),
                    "purpose": event.get("purpose"),
                    "hours": event.get("hours"),
                    "group": event.get("group"),
                    "inclusion_probability": event.get("inclusion_probability"),
                    "extrapolable": event.get("extrapolable"),
                    "state": "proposed",
                    "history": [event],
                }
                continue
            task = view.setdefault(tid, {"id": tid, "history": []})
            task["history"].append(event)
            if event["action"] == "accept":
                task["state"] = "accepted"
                task["accepted_on"] = event["at"]
                task["reviewer"] = event.get("reviewer", task.get("reviewer"))
            elif event["action"] == "complete":
                task["state"] = "completed"
                task["completed_on"] = event["at"]
            elif event["action"] == "return":
                task["state"] = "returned"
                task["return_reason"] = event.get("reason")
            elif event["action"] == "invalidate":
                task["state"] = "invalidated"
                task["invalidate_reason"] = event.get("reason")
        return view

    # ---- 签发事件（不可变） ---------------------------------------------
    def add_signoff(self, event: dict) -> dict:
        required = {
            "id", "at", "content_set", "channel", "language", "version",
            "decision", "references", "gate", "snapshot",
        }
        missing = required - set(event)
        if missing:
            raise ValueError(f"签发事件缺少字段: {sorted(missing)}")
        self._append(SIGNOFFS_FILE, event)
        return event

    def signoffs(self) -> list[dict]:
        return self._read(SIGNOFFS_FILE)

    # ---- 渠道接收回执（后续到达的渠道事实） -----------------------------
    def add_receipt(self, receipt: dict) -> dict:
        required = {"id", "content_set", "received_on", "state"}
        missing = required - set(receipt)
        if missing:
            raise ValueError(f"回执事件缺少字段: {sorted(missing)}")
        self._append(RECEIPTS_FILE, receipt)
        return receipt

    def delivery_receipts(self) -> list[dict]:
        return self._read(RECEIPTS_FILE)

    # ---- 导出 -----------------------------------------------------------
    def export_bundle(self, out_path: str | Path, meta: dict) -> Path:
        bundle = {
            "bundle_format": "releaseqa-bundle-v1",
            "exported_on": date.today().isoformat(),
            "meta": meta,
            "review_events": self.reviews(),
            "task_events": self.task_events(),
            "signoff_events": self.signoffs(),
            "receipt_events": self.delivery_receipts(),
        }
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
        return out

    def import_bundle(self, path: str | Path) -> dict:
        bundle = json.loads(Path(path).read_text(encoding="utf-8"))
        if bundle.get("bundle_format") != "releaseqa-bundle-v1":
            raise ValueError("不支持的资料包格式")
        counts = {"reviews": 0, "task_events": 0, "signoffs": 0, "receipts": 0}
        for review in bundle.get("review_events", []):
            if review["id"] not in {r["id"] for r in self.reviews()}:
                self.add_review(review)
                counts["reviews"] += 1
        existing_tasks = {json.dumps(e, sort_keys=True) for e in self.task_events()}
        for event in bundle.get("task_events", []):
            marker = json.dumps(event, sort_keys=True)
            if marker not in existing_tasks:
                self.add_task_event(event)
                counts["task_events"] += 1
        existing_signoffs = {s["id"] for s in self.signoffs()}
        for event in bundle.get("signoff_events", []):
            if event["id"] not in existing_signoffs:
                self.add_signoff(event)
                counts["signoffs"] += 1
        existing_receipts = {r["id"] for r in self.delivery_receipts()}
        for receipt in bundle.get("receipt_events", []):
            if receipt["id"] not in existing_receipts:
                self.add_receipt(receipt)
                counts["receipts"] += 1
        return counts
