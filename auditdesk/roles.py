"""不同角色的可见范围。

* 县级管理员：只能看本县来源覆盖与本县相关的撤回进度。
* 收件人（排查组）：只在授权用途与字段内可见；转派目标不继承许可。
* 联合审计：按任务取最小凭据集合，不提供全量原始元数据。
"""
from __future__ import annotations

from typing import Any, Iterable

from .delivery import reconcile
from .policy import (
    combined_traffic_view,
    continued_use,
    disclose,
    reassignment_effect,
    terms_for,
)

AUDIT_TASK_KINDS = {
    "withdrawal_audit": {
        "description_zh": "核对投递、收件回执与撤回回执",
        "case_fields": (
            "package_id", "entry", "origin", "status", "status_zh",
            "recipient_receipt", "outstanding_since", "recalls",
            "withdrawal_confirmation",
        ),
    },
    "access_audit": {
        "description_zh": "核对已发生访问的许可依据与收窄后的续取资格",
        "fact_fields": ("id", "entry", "grant", "group", "purpose", "accessed_at", "decision"),
    },
    "coverage_audit": {
        "description_zh": "核对最小披露覆盖与更粗范围",
        "entry_fields": ("id", "origin", "minutes", "devices", "coverage", "coarser"),
    },
}


# ---------------------------------------------------------------------------
# 县级管理员
# ---------------------------------------------------------------------------

def county_admin_view(
    *,
    county: str,
    entries: list[dict],
    grant_rows: list[dict],
    delivery_rows: list[dict],
    at: int,
) -> dict:
    own_entries = [e for e in entries if e.get("origin") == county]
    coverage = []
    for entry in own_entries:
        terms = terms_for(grant_rows, county, "joint-team", at)
        minimum = terms.min_aggregate_members if terms and terms.min_aggregate_members else 5
        decision = disclose(
            entries=entries,
            grant_rows=grant_rows,
            entry=entry,
            group="joint-team",
            purpose="outage_review",
            at=at,
        )
        coverage.append({
            "entry": entry["id"],
            "interval": list(entry["minutes"]),
            "granularity": entry["granularity"],
            "devices": entry.get("devices"),
            "minimum": minimum,
            "coverage": decision["coverage"],
            "sampling_note": entry.get("sampling_note"),
            "coarser": decision["coarser"],
        })

    report = reconcile(delivery_rows, entries, at)
    own_cases = [c for c in report["cases"] if c.get("origin") == county]
    own_followups = [f for f in report["open_followups"] if f.get("origin") == county]
    status_counts: dict[str, int] = {}
    for case in own_cases:
        status_counts[case["status"]] = status_counts.get(case["status"], 0) + 1

    return {
        "role": "county_admin",
        "county": county,
        "at": at,
        "source_coverage": coverage,
        "withdrawal_progress": {
            "status_counts": status_counts,
            "cases": [_public_case(c) for c in own_cases],
            "open_followups": own_followups,
            "complete": not own_followups,
        },
    }


def _public_case(case: dict) -> dict:
    return {k: v for k, v in case.items() if k != "raw"}


# ---------------------------------------------------------------------------
# 收件人 / 排查组
# ---------------------------------------------------------------------------

def recipient_view(
    *,
    group: str,
    entries: list[dict],
    grant_rows: list[dict],
    reassignments: list[dict],
    at: int,
    purpose: str = "outage_review",
    standing: Any = None,
) -> dict:
    decisions = []
    for entry in entries:
        decisions.append(disclose(
            entries=entries,
            grant_rows=grant_rows,
            entry=entry,
            group=group,
            purpose=purpose,
            at=at,
            standing=standing,
        ))
    incoming = [r for r in reassignments if r.get("to") == group]
    outgoing = [r for r in reassignments if r.get("from") == group]
    reassignment_notes = [
        reassignment_effect(grant_rows, r, at) for r in incoming + outgoing
    ]
    effective_grants = []
    from .policy import build_grant_chains, resolve_terms
    for chain in build_grant_chains(grant_rows).values():
        terms = resolve_terms(chain, at)
        if terms and terms.group == group:
            effective_grants.append(terms.to_dict())

    return {
        "role": "recipient",
        "group": group,
        "purpose": purpose,
        "at": at,
        "effective_grants": effective_grants,
        "currently_disclosable": [
            {
                "entry": d["entry"],
                "origin": d["origin"],
                "interval": d["interval"],
                "allowed": d["allowed"],
                "status": d["status"],
                "released_fields": d["released_fields"],
                "coarser": d["coarser"],
            }
            for d in decisions
        ],
        "reassignments": reassignment_notes,
        "note_zh": "转派不继承原接收组许可；仅显式授权与常态五分钟计数约定可作为依据。",
    }


# ---------------------------------------------------------------------------
# 联合审计：按任务取最小凭据
# ---------------------------------------------------------------------------

def joint_audit_bundle(
    *,
    task: dict,
    entries: list[dict],
    grant_rows: list[dict],
    delivery_rows: list[dict],
    audit_facts: list[dict],
    at: int,
) -> dict:
    kind = task.get("kind")
    if kind not in AUDIT_TASK_KINDS:
        return {
            "role": "joint_audit",
            "task": task,
            "authorized": False,
            "reason_zh": "审计任务类型未登记；联合审计只能执行明确授权的核对任务。",
            "supported_kinds": sorted(AUDIT_TASK_KINDS),
        }

    scope = set(task.get("scope_ids") or [])
    spec = AUDIT_TASK_KINDS[kind]
    bundle: dict[str, Any] = {
        "role": "joint_audit",
        "task_id": task.get("task_id"),
        "kind": kind,
        "description_zh": spec["description_zh"],
        "at": at,
        "authorized": True,
        "minimization_zh": "仅返回本任务所需凭据字段，不含报文内容与任务范围外记录。",
    }

    if kind == "withdrawal_audit":
        report = reconcile(delivery_rows, entries, at)
        cases = report["cases"]
        if scope:
            cases = [c for c in cases if c["package_id"] in scope
                     or any(r["id"] in scope for r in c["recalls"])]
        fields = spec["case_fields"]
        bundle["cases"] = [{k: c.get(k) for k in fields} for c in cases]
        bundle["balanced"] = report["balanced"]
        bundle["orphan_refs"] = report["orphan_refs"]

    elif kind == "access_audit":
        facts = list(audit_facts)
        # 种子里的 view-raw 也是访问事实来源。
        seed_accesses = reconcile(delivery_rows, entries, at)["access_facts"]
        known_ids = {f.get("id") for f in facts}
        facts = facts + [a for a in seed_accesses if a.get("id") not in known_ids]
        if scope:
            facts = [f for f in facts if f.get("id") in scope or f.get("entry") in scope]
        item_fields = spec["fact_fields"]
        items = []
        for fact in facts:
            enriched = dict(fact)
            # 种子访问事实只记了 entry/grant/accessed_at；按联合排查默认职责补齐。
            enriched.setdefault("group", task.get("group", "joint-team"))
            enriched.setdefault("purpose", task.get("purpose", "outage_review"))
            decision = continued_use(
                entries=entries,
                grant_rows=grant_rows,
                access_fact=enriched,
                at=at,
            )
            enriched["decision"] = {
                "accessed_at": decision["accessed_at"],
                "purpose_still_authorized": decision["purpose_still_authorized"],
                "fields_at_access": decision["fields_at_access"],
                "fields_now": decision["fields_now"],
                "fields_removed_by_narrowing": decision["fields_removed_by_narrowing"],
                "retrievable_now": decision["retrievable_now"],
            }
            items.append({k: enriched.get(k) for k in item_fields})
        bundle["facts"] = items

    elif kind == "coverage_audit":
        wanted_entries = entries
        if scope:
            wanted_entries = [e for e in entries if e["id"] in scope]
        items = []
        for entry in wanted_entries:
            d = disclose(
                entries=entries,
                grant_rows=grant_rows,
                entry=entry,
                group=task.get("group", "joint-team"),
                purpose=task.get("purpose", "outage_review"),
                at=at,
            )
            items.append({
                "id": entry["id"],
                "origin": entry["origin"],
                "minutes": entry["minutes"],
                "devices": entry.get("devices"),
                "coverage": d["coverage"],
                "coarser": d["coarser"],
            })
        bundle["entries"] = items

    return bundle


def combined_view_for_task(**kwargs) -> dict:
    """转发到 policy.combined_traffic_view，方便按角色模块统一引用。"""
    return combined_traffic_view(**kwargs)
