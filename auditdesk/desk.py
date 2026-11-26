"""有状态的防御性审核台门面：接收许可、审阅队列、跟进回执、留审计事实。

状态分两层：

* ``records/`` 中的种子合成材料始终只读；
* 运行期接收的新许可、投递事件、审计事实写入独立 state 文件，
  事件采用追加方式，不覆写既有事实。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Iterable

from .delivery import build_cases, reconcile, review_package
from .policy import (
    combined_traffic_view,
    continued_use,
    disclose,
    standing_policy,
)
from .records import (
    AuditDeskError,
    default_records_dir,
    infer_clock,
    normalize_entries,
)
from .roles import county_admin_view, joint_audit_bundle, recipient_view

DEFAULT_STATE_PATH = Path(__file__).resolve().parent.parent / "runtime" / "state.json"


class AuditDesk:
    def __init__(
        self,
        records_dir: str | Path | None = None,
        state_path: str | Path | None = None,
        state: dict | None = None,
        persist: bool = False,
    ):
        self.records_dir = Path(records_dir) if records_dir else default_records_dir()
        # 延迟导入避免 records_view 依赖本包。
        from records_view import load_sources
        self.sources = load_sources(self.records_dir)
        if "registry" not in self.sources or "grants" not in self.sources or "delivery" not in self.sources:
            raise AuditDeskError("资料目录需要 registry、grants、delivery 三个来源")
        self._seed_entries = normalize_entries(self.sources["registry"])
        self.seed_grants = list(self.sources["grants"])
        self.seed_delivery = list(self.sources["delivery"])
        # 库默认内存态，避免污染仓库；CLI/Web 会显式给出 state_path。
        self.state_path = Path(state_path) if state_path else (DEFAULT_STATE_PATH if persist else None)
        self._lock = threading.RLock()
        if state is not None:
            self.state = self._valid_state(state)
        elif self.state_path and self.state_path.exists():
            self.state = self._valid_state(json.loads(self.state_path.read_text("utf-8")))
        else:
            self.state = self._empty_state()

    # ------------------------------------------------------------------ state
    @staticmethod
    def _empty_state() -> dict:
        return {"grants": [], "entries": [], "delivery": [], "events": [],
                "audit_facts": [], "clock": None}

    @classmethod
    def _valid_state(cls, state: dict) -> dict:
        base = cls._empty_state()
        if not isinstance(state, dict):
            raise AuditDeskError("状态文件必须是 JSON 对象")
        for key in ("grants", "entries", "delivery", "events", "audit_facts"):
            value = state.get(key, [])
            if not isinstance(value, list):
                raise AuditDeskError(f"state.{key} 必须是数组")
            base[key] = value
        if state.get("clock") is not None and not isinstance(state["clock"], int):
            raise AuditDeskError("state.clock 必须是整数分钟")
        base["clock"] = state.get("clock")
        return base

    def save(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(self.state_path)

    def reset_runtime(self) -> None:
        with self._lock:
            self.state = self._empty_state()
            self.save()

    # ------------------------------------------------------------- aggregate
    @property
    def entries(self) -> list[dict]:
        """种子 registry + 运行期接收的下一期来源（每次规范化，状态保持原样）。"""
        return [*self._seed_entries, *normalize_entries(self.state.get("entries", []))]

    @property
    def grant_rows(self) -> list[dict]:
        return [*self.seed_grants, *self.state["grants"]]

    @property
    def delivery_rows(self) -> list[dict]:
        return [*self.seed_delivery, *self.state["delivery"], *self.state["events"]]

    @property
    def audit_facts(self) -> list[dict]:
        return list(self.state["audit_facts"])

    @property
    def reassignments(self) -> list[dict]:
        return [r for r in self.grant_rows if r.get("from") and r.get("to")]

    def now(self, at: int | None = None) -> int:
        if at is not None:
            return at
        if self.state.get("clock") is not None:
            return int(self.state["clock"])
        return infer_clock(self.sources, [*self.state["grants"], *self.state["delivery"],
                                          *self.state["events"], *self.state["audit_facts"]])

    def set_clock(self, at: int) -> int:
        with self._lock:
            self.state["clock"] = int(at)
            self.save()
        return self.state["clock"]

    # -------------------------------------------------------------- intake
    def _existing_ids(self) -> set[str]:
        ids = set()
        for rows in (self.grant_rows, self.delivery_rows, self.audit_facts):
            for row in rows:
                if isinstance(row.get("id"), str):
                    ids.add(row["id"])
        return ids

    def admit_grant(self, record: dict) -> dict:
        """持续接收一份新的许可/修订/转派文件。"""
        if not isinstance(record, dict):
            raise AuditDeskError("许可记录必须是对象")
        rid = record.get("id")
        if not isinstance(rid, str) or not rid:
            raise AuditDeskError("许可记录缺少字符串 id")
        with self._lock:
            if any(g.get("id") == rid for g in self.grant_rows):
                raise AuditDeskError(f"许可 id 重复: {rid}")
            is_revision = bool(record.get("for"))
            is_base = bool(record.get("origin") and record.get("group"))
            is_reassign = bool(record.get("from") and record.get("to"))
            if not (is_revision or is_base or is_reassign):
                raise AuditDeskError("许可记录需是基础许可(origin/group)、修订(for)或转派(from/to)")
            self.state["grants"].append(record)
            self.save()
        return record

    def admit_many(self, records: Iterable[dict]) -> list[dict]:
        return [self.admit_grant(r) for r in records]

    def admit_entries(self, records: Iterable[dict]) -> list[dict]:
        """接收采集部门提供的下一期日志区间元数据。"""
        records = list(records)
        normalized = normalize_entries(records)  # 先整体校验
        existing = {e["id"] for e in self.entries}
        with self._lock:
            for row, norm in zip(records, normalized):
                if norm["id"] in existing:
                    raise AuditDeskError(f"来源记录 id 重复: {norm['id']}")
                existing.add(norm["id"])
                self.state["entries"].append(row)
            self.save()
        return records

    def ingest(self, record: dict) -> dict:
        """通用接收入口：按形状把下一期记录放入正确的流。"""
        if not isinstance(record, dict):
            raise AuditDeskError("记录必须是对象")
        kind = record.get("kind")
        if kind in ("receipt", "ack"):
            self.state["events"].append(record)
            self.save()
            return record
        if kind == "access" or "accessed_at" in record:
            self.state["audit_facts"].append(record)
            self.save()
            return record
        if "minutes" in record and record.get("origin") and (
                "devices" in record or "fields" in record):
            return self.admit_entries([record])[0]
        if record.get("from") and record.get("to") or (record.get("origin") and record.get("group")) \
                or record.get("for"):
            return self.admit_grant(record)
        if record.get("entry") or (record.get("for") and "state" in record):
            self.state["delivery"].append(record)
            self.save()
            return record
        raise AuditDeskError("无法识别的记录类型")

    # --------------------------------------------------------- workflow
    def _new_id(self, prefix: str) -> str:
        existing = self._existing_ids()
        max_n = 0
        for rid in existing:
            if rid.startswith(f"{prefix}-"):
                suffix = rid[len(prefix) + 1:]
                if suffix.isdigit():
                    max_n = max(max_n, int(suffix))
        n = max_n + 1
        while f"{prefix}-{n}" in existing:
            n += 1
        return f"{prefix}-{n}"

    def issue_recall(self, package_id: str, at: int | None = None,
                    recall_id: str | None = None, reason: str | None = None) -> dict:
        at = self.now(at)
        with self._lock:
            model = build_cases(self.delivery_rows, self.entries)
            if not any(c["package_id"] == package_id for c in model["cases"]):
                raise AuditDeskError(f"投递包裹不存在: {package_id}")
            rid = recall_id or self._new_id("recall")
            if any(r.get("id") == rid for r in model["recalls"]):
                raise AuditDeskError(f"撤回单 id 重复: {rid}")
            row = {
                "id": rid,
                "for": package_id,
                "state": "requested",
                "requested_at": at,
                "acknowledgment": None,
            }
            if reason:
                row["reason"] = reason
            self.state["delivery"].append(row)
            self.save()
            return row

    def record_receipt(self, package_id: str, at: int | None = None,
                       by: str | None = None, event_id: str | None = None) -> dict:
        at = self.now(at)
        with self._lock:
            model = build_cases(self.delivery_rows, self.entries)
            if not any(c["package_id"] == package_id for c in model["cases"]):
                raise AuditDeskError(f"投递包裹不存在: {package_id}")
            event = {
                "id": event_id or self._new_id("receipt-evt"),
                "kind": "receipt",
                "for": package_id,
                "at": at,
                "by": by or "recipient",
            }
            self.state["events"].append(event)
            self.save()
            return event

    def acknowledge_recall(self, recall_id: str, at: int | None = None,
                           outcome: str = "deleted", by: str | None = None,
                           event_id: str | None = None) -> dict:
        at = self.now(at)
        with self._lock:
            parts = build_cases(self.delivery_rows, self.entries)
            if not any(r.get("id") == recall_id for r in parts["recalls"]):
                raise AuditDeskError(f"撤回单不存在: {recall_id}")
            event = {
                "id": event_id or self._new_id("ack-evt"),
                "kind": "ack",
                "for": recall_id,
                "at": at,
                "outcome": outcome,
                "by": by or "recipient",
            }
            self.state["events"].append(event)
            self.save()
            return event

    def withdraw_access(self, fact_id: str, at: int | None = None,
                        recall_id: str | None = None) -> dict:
        """对“已经交给排查组的明细”发起删除/撤回。

        访问事实本身不是投递包裹；这里为它构造一个“对方已收到的副本”
        包裹（id 固定为 copy-<fact_id>，幂等），再走标准撤回状态机：
        received_pending_deletion -> acknowledgment -> withdrawal_confirmed。
        """
        at = self.now(at)
        fact = next((f for f in self.audit_facts if f.get("id") == fact_id), None)
        if fact is None:
            fact = next((r for r in self.seed_delivery
                         if r.get("id") == fact_id and "accessed_at" in r), None)
        if fact is None:
            raise AuditDeskError(f"访问事实不存在: {fact_id}")
        package_id = f"copy-{fact_id}"
        with self._lock:
            model = build_cases(self.delivery_rows, self.entries)
            existing_case = next((c for c in model["cases"]
                                  if c["package_id"] == package_id), None)
            if existing_case is None:
                self.state["delivery"].append({
                    "id": package_id,
                    "entry": fact.get("entry"),
                    "state": "received",
                    "receipt_at": fact.get("accessed_at", at),
                    "source_access": fact_id,
                })
                self.save()
                recall = self.issue_recall(package_id, at=at, recall_id=recall_id)
            else:
                open_recall = next((r for r in existing_case["recalls"]
                                    if not r["confirmed"]), None)
                if open_recall is not None:
                    recall = {"id": open_recall["id"], "for": package_id,
                              "state": open_recall["state"], "idempotent": True}
                else:
                    recall = self.issue_recall(package_id, at=at, recall_id=recall_id)
        return {"package_id": package_id, "recall": recall}

    def record_access(self, *, entry_id: str, group: str = "joint-team",
                      purpose: str = "outage_review", at: int | None = None,
                      accessor: str | None = None, fact_id: str | None = None,
                      standing_baseline: bool = True) -> dict:
        at = self.now(at)
        standing = standing_policy() if standing_baseline else None
        decision = disclose(
            entries=self.entries,
            grant_rows=self.grant_rows,
            entry_id=entry_id,
            group=group,
            purpose=purpose,
            at=at,
            standing=standing,
            subject=accessor,
        )
        fact = {
            "id": fact_id or self._new_id("access"),
            "kind": "access",
            "entry": entry_id,
            "group": group,
            "purpose": purpose,
            "accessed_at": at,
            "accessor": accessor,
            "outcome": "fulfilled" if decision["allowed"] else "denied",
            "status": decision["status"],
            "released_fields": decision["released_fields"],
        }
        with self._lock:
            self.state["audit_facts"].append(fact)
            self.save()
        return {"fact": fact, "decision": decision}

    # ------------------------------------------------------------- queries
    def reconcile(self, at: int | None = None) -> dict:
        return reconcile(self.delivery_rows, self.entries, self.now(at))

    def review_queue(self, at: int | None = None, group: str = "joint-team",
                     purpose: str = "outage_review") -> dict:
        at = self.now(at)
        report = self.reconcile(at)
        reviews = []
        for case in report["cases"]:
            # “待交”只含尚未取得收件回执的包裹；已收件的进入撤回/订阅核对。
            if case["received"] or case["status"] == "withdrawal_confirmed":
                continue
            reviews.append(review_package(
                case,
                entries=self.entries,
                grant_rows=self.grant_rows,
                at=at,
                group=group,
                purpose=purpose,
                standing=standing_policy(),
            ))
        return {"at": at, "reviews": reviews}

    def traffic_view(self, interval, at: int | None = None, group: str = "joint-team",
                     purpose: str = "outage_review") -> dict:
        return combined_traffic_view(
            entries=self.entries,
            grant_rows=self.grant_rows,
            interval=interval,
            group=group,
            purpose=purpose,
            at=self.now(at),
        )

    def disclose_entry(self, entry_id: str, group: str = "joint-team",
                       purpose: str = "outage_review", at: int | None = None,
                       standing_baseline: bool = False) -> dict:
        return disclose(
            entries=self.entries,
            grant_rows=self.grant_rows,
            entry_id=entry_id,
            group=group,
            purpose=purpose,
            at=self.now(at),
            standing=standing_policy() if standing_baseline else None,
        )

    def county_view(self, county: str, at: int | None = None) -> dict:
        return county_admin_view(
            county=county,
            entries=self.entries,
            grant_rows=self.grant_rows,
            delivery_rows=self.delivery_rows,
            at=self.now(at),
        )

    def recipient_view(self, group: str = "joint-team", at: int | None = None) -> dict:
        return recipient_view(
            group=group,
            entries=self.entries,
            grant_rows=self.grant_rows,
            reassignments=self.reassignments,
            at=self.now(at),
            standing=standing_policy(),
        )

    def audit_bundle(self, task: dict, at: int | None = None) -> dict:
        return joint_audit_bundle(
            task=task,
            entries=self.entries,
            grant_rows=self.grant_rows,
            delivery_rows=self.delivery_rows,
            audit_facts=self.audit_facts,
            at=self.now(at),
        )

    def workbench(self, at: int | None = None) -> dict:
        """按业务开头要求把工作分成三类分别处理。

        1. ``handed_details``：已经交给联合排查组/已被取阅的明细，
           事实留审计，并给出收窄后原用途是否仍合法的判断；
        2. ``pending_delivery``：尚待送达、仍需审阅和跟进的队列；
        3. ``active_summaries``：仍有效、可继续使用的汇总订阅。
        """
        at = self.now(at)
        report = self.reconcile(at)

        known_fact_ids = {f.get("id") for f in self.audit_facts}
        facts = list(self.audit_facts) + [
            a for a in report["access_facts"] if a.get("id") not in known_fact_ids
        ]
        handed = []
        for fact in facts:
            entry = next((e for e in self.entries if e["id"] == fact.get("entry")), None)
            cu = continued_use(entries=self.entries, grant_rows=self.grant_rows,
                               access_fact=fact, at=at)
            handed.append({
                "fact": fact,
                "entry_granularity": entry.get("granularity") if entry else None,
                "origin": entry.get("origin") if entry else fact.get("origin"),
                "continued_use": cu,
            })

        pending = self.review_queue(at)["reviews"]
        summaries = [
            {k: v for k, v in case.items() if k != "raw"}
            for case in report["cases"] if case["status"] == "usable_summary"
        ]
        return {
            "at": at,
            "handed_details": handed,
            "pending_delivery": pending,
            "active_summaries": summaries,
        }

    def console(self, at: int | None = None) -> dict:
        """审核台首屏所需的完整只读模型。"""
        at = self.now(at)
        report = self.reconcile(at)
        queue = self.review_queue(at)
        grant_stream = []
        for row in self.grant_rows:
            grant_stream.append(row)
        return {
            "at": at,
            "records_dir": str(self.records_dir),
            "state_path": str(self.state_path) if self.state_path else None,
            "grant_stream": grant_stream,
            "entries": [{k: v for k, v in e.items() if k != "raw"} for e in self.entries],
            "reassignments": self.reassignments,
            "review_queue": queue["reviews"],
            "reconciliation": {
                "at": at,
                "status_counts": report["status_counts"],
                "open_followups": report["open_followups"],
                "balanced": report["balanced"],
                "orphan_refs": report["orphan_refs"],
                "cases": report["cases"],
            },
            "traffic_view_20_25": self.traffic_view([20, 25], at),
            "workbench": self.workbench(at),
            "county_a": self.county_view("county-a", at),
            "county_b": self.county_view("county-b", at),
            "joint_team": self.recipient_view("joint-team", at),
            "vendor_desk": self.recipient_view("vendor-desk", at),
            "audit_facts": self.audit_facts,
        }
