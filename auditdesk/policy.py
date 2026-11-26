"""许可链、最小披露覆盖与可披露字段判定。

核心规则（对应业务约定）：

* 许可可以分版本下发：基础许可带 ``origin/group/purposes/...``，
  修订记录用 ``for`` 指向基础许可，``effective`` 之后替换相应条款。
* 能否看字段，要同时核对：接收组、用途、数据产生期适用的许可版本、
  当前（审阅/续取时）有效的许可版本。
* 设备数低于该县最小聚合成员数的区间，不暴露县级明细，
  改为返回 ``coverage_insufficient`` 与可用的更粗范围。
* 转派记录只是事实，不会把原接收组的许可继承给新组。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .records import (
    DEFAULT_MIN_MEMBERS,
    interval_contains,
    interval_key,
)

PURPOSE_OUTAGE_REVIEW = "outage_review"

# 稳定的机器状态码 + 中文说明（界面与报告直接引用）。
STATUS_LABELS_ZH = {
    "coverage_insufficient": "覆盖不足：该区间设备过少，已暂缓暴露明细",
    "no_grant": "没有对该接收组的有效许可",
    "purpose_not_authorized": "用途不在授权职责内",
    "field_not_disclosed": "字段在当前许可中已不可披露",
    "ok": "符合披露条件",
}


# ---------------------------------------------------------------------------
# 许可链
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GrantTerms:
    grant_id: str
    origin: str | None
    group: str | None
    purposes: tuple[str, ...]
    fields: tuple[str, ...]
    min_aggregate_members: int | None
    effective: int
    revision_ids: tuple[str, ...] = ()
    purpose_fields: dict[str, tuple[str, ...]] | None = None
    expires: int | None = None
    # 仅对常态约定有意义：该约定覆盖哪些接收组；None 表示不限制。
    groups: tuple[str, ...] | None = None
    # 事件许可若点名了排查员，则只有这些主体可取阅；None/空表示组内均可。
    allowed_subjects: tuple[str, ...] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "grant_id": self.grant_id,
            "origin": self.origin,
            "group": self.group,
            "purposes": list(self.purposes),
            "fields": list(self.fields),
            "min_aggregate_members": self.min_aggregate_members,
            "effective": self.effective,
            "revision_ids": list(self.revision_ids),
            "expires": self.expires,
            "allowed_subjects": list(self.allowed_subjects) if self.allowed_subjects else None,
        }


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(value)


def build_grant_chains(grant_rows: Iterable[dict]) -> dict[str, list[dict]]:
    """返回 base grant id -> [base, revisions...]（按生效时间排序）。"""
    rows = list(grant_rows)
    bases: dict[str, dict] = {}
    revisions: dict[str, list[dict]] = {}
    for row in rows:
        if row.get("for"):
            revisions.setdefault(row["for"], []).append(row)
        elif row.get("origin") and row.get("group"):
            bases[row["id"]] = row
    chains: dict[str, list[dict]] = {}
    for gid, base in bases.items():
        revs = sorted(
            revisions.get(gid, []),
            key=lambda r: (r.get("effective", 0), r.get("id", "")),
        )
        chains[gid] = [base, *revs]
    # 指向缺失 base 的修订不会凭空产生许可；保留在孤立列表中由调用方体检。
    return chains


def orphan_revisions(grant_rows: Iterable[dict]) -> list[dict]:
    rows = list(grant_rows)
    base_ids = {r["id"] for r in rows if not r.get("for") and r.get("origin")}
    return [r for r in rows if r.get("for") and r["for"] not in base_ids]


def resolve_terms(chain: list[dict], at: int) -> GrantTerms | None:
    """解析 ``at`` 时刻有效的许可条款；已过期或尚未生效返回 None。"""
    if not chain:
        return None
    base = chain[0]
    if base.get("effective", 0) > at:
        return None
    terms: dict[str, Any] = {
        "grant_id": base["id"],
        "origin": base.get("origin"),
        "group": base.get("group"),
        "purposes": list(base.get("purposes", [])),
        "fields": list(base.get("fields", [])),
        "min_aggregate_members": base.get("min_aggregate_members"),
        "effective": base.get("effective", 0),
        "expires": base.get("expires"),
        "purpose_fields": {
            k: tuple(v) for k, v in (base.get("purpose_fields") or {}).items()
        } or None,
        "allowed_subjects": tuple(base.get("investigators") or base.get("subjects") or ()),
    }
    applied: list[str] = []
    for rev in chain[1:]:
        if rev.get("effective", 0) > at:
            break
        applied.append(rev["id"])
        # 修订记录中出现的键整体替换对应条款（许可机关对自己的文件负责）。
        for key in ("purposes", "fields", "min_aggregate_members", "expires"):
            if key in rev:
                terms[key] = rev[key]
        if "purpose_fields" in rev:
            terms["purpose_fields"] = rev["purpose_fields"]
        if "investigators" in rev or "subjects" in rev:
            terms["allowed_subjects"] = tuple(rev.get("investigators") or rev.get("subjects") or ())
    if terms.get("expires") is not None and terms["expires"] <= at:
        return None
    return GrantTerms(
        grant_id=terms["grant_id"],
        origin=terms["origin"],
        group=terms["group"],
        purposes=_as_tuple(terms["purposes"]),
        fields=_as_tuple(terms["fields"]),
        min_aggregate_members=terms["min_aggregate_members"],
        effective=terms["effective"],
        revision_ids=tuple(applied),
        purpose_fields=terms["purpose_fields"],
        expires=terms["expires"],
        allowed_subjects=terms["allowed_subjects"] or None,
    )


def find_chain(
    grant_rows: Iterable[dict],
    origin: str | None,
    group: str | None,
) -> list[dict] | None:
    """找到某县发给某组的基础许可链。"""
    chains = build_grant_chains(grant_rows)
    for chain in chains.values():
        base = chain[0]
        if base.get("origin") == origin and base.get("group") == group:
            return chain
    return None


def terms_for(
    grant_rows: Iterable[dict],
    origin: str | None,
    group: str | None,
    at: int,
) -> GrantTerms | None:
    chain = find_chain(grant_rows, origin, group)
    return resolve_terms(chain, at) if chain else None


def terms_for_period(
    grant_rows: Iterable[dict],
    origin: str | None,
    group: str | None,
    minutes: list[int] | tuple[int, int],
) -> GrantTerms | None:
    """按数据产生期（区间起点的分钟编号）确定当时适用的许可条款。"""
    return terms_for(grant_rows, origin, group, int(minutes[0]))


def standing_policy(min_members: int = DEFAULT_MIN_MEMBERS,
                    groups: tuple[str, ...] = ("joint-team",)) -> GrantTerms:
    """早期约定：五分钟级 ``count`` 连接计数在联合排查用途下常态交换。

    这是“仍有效的汇总订阅”的规范来源；设备代号等明细不在其中。
    各县若下发显式许可，以显式许可（含其最小成员数）为准。
    常态约定只覆盖成员接收组（默认 joint-team），转派目标不在其中。
    """
    return GrantTerms(
        grant_id="standing-five-minute-counts",
        origin=None,
        group=None,
        purposes=(PURPOSE_OUTAGE_REVIEW,),
        fields=("count",),
        min_aggregate_members=min_members,
        effective=0,
        groups=groups,
    )


def effective_terms(
    grant_rows: Iterable[dict],
    origin: str | None,
    group: str | None,
    at: int,
    standing: GrantTerms | None = None,
) -> tuple[GrantTerms | None, str]:
    """返回 (条款, 依据)；无显式许可时可回落至早期常态约定。"""
    explicit = terms_for(grant_rows, origin, group, at)
    if explicit is not None:
        return explicit, "explicit_grant"
    if standing is not None and (not standing.groups or group in standing.groups):
        return standing, "standing_baseline"
    return None, "none"


# ---------------------------------------------------------------------------
# 用途 -> 字段
# ---------------------------------------------------------------------------

def fields_for_purpose(terms: GrantTerms, purpose: str) -> list[str]:
    """按用途过滤可披露字段。

    许可可用 ``purpose_fields`` 做更细的用途-字段映射；
    否则授权用途共享同一份 ``fields`` 清单。
    """
    if purpose not in terms.purposes and "*" not in terms.purposes:
        return []
    if terms.purpose_fields:
        if purpose in terms.purpose_fields:
            return list(terms.purpose_fields[purpose])
        if "*" in terms.purpose_fields:
            return list(terms.purpose_fields["*"])
        return []
    return list(terms.fields)


# ---------------------------------------------------------------------------
# 最小披露覆盖与更粗范围
# ---------------------------------------------------------------------------

def minimum_for(terms: GrantTerms | None, override: int | None = None) -> int:
    if override is not None:
        return override
    if terms is not None and isinstance(terms.min_aggregate_members, int):
        return terms.min_aggregate_members
    return DEFAULT_MIN_MEMBERS


def assess_coverage(
    entry: dict,
    minimum: int,
) -> dict:
    devices = entry.get("devices")
    if devices is None:
        # 没有设备数就无法证明满足最小披露约定：防御性视为不足。
        sufficient = False
        reason = "devices_unknown"
    else:
        sufficient = devices >= minimum
        reason = "ok" if sufficient else "too_few_devices"
    return {
        "sufficient": sufficient,
        "devices": devices,
        "minimum": minimum,
        "reason": reason,
    }


def find_coarser(
    entries: Iterable[dict],
    entry: dict,
    minimum: int,
) -> dict | None:
    """为覆盖不足的明细寻找同来源、包含本区间且覆盖达标的最窄粗范围。"""
    candidates: list[tuple[int, int, dict]] = []
    start, end = entry["minutes"]
    for other in entries:
        if other["id"] == entry["id"]:
            continue
        if other.get("origin") != entry.get("origin"):
            continue
        if not interval_contains(other["minutes"], (start, end)):
            continue
        coverage = assess_coverage(other, minimum)
        if not coverage["sufficient"]:
            continue
        oa, ob = other["minutes"]
        # 宽度越小越贴近原区间，优先推荐。
        candidates.append((ob - oa, other["id"], other))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], x[1]))
    chosen = candidates[0][2]
    return {
        "entry": chosen["id"],
        "origin": chosen.get("origin"),
        "minutes": list(chosen["minutes"]),
        "granularity": chosen.get("granularity"),
        "devices": chosen.get("devices"),
        "fields_available": chosen.get("fields_available", []),
        "coverage": assess_coverage(chosen, minimum),
    }


# ---------------------------------------------------------------------------
# 完整披露判定
# ---------------------------------------------------------------------------

def disclose(
    *,
    entries: Iterable[dict],
    grant_rows: Iterable[dict],
    entry_id: str | None = None,
    entry: dict | None = None,
    group: str,
    purpose: str,
    at: int,
    minimum_override: int | None = None,
    require_coverage: bool = True,
    standing: GrantTerms | None = None,
    subject: str | None = None,
) -> dict:
    """判断某接收组在 ``at`` 时刻能否取得某条来源记录。

    返回结构化决定，绝不抛“不允许”类异常，便于审核台展示与留痕。
    ``released_fields`` 是当前真正可离开审核台的字段；
    覆盖不足时即便字段许可存在，也不发布该县级区间。
    """
    entries = list(entries)
    grant_rows = list(grant_rows)
    if entry is None:
        entry = next((e for e in entries if e["id"] == entry_id), None)
    if entry is None:
        raise ValueError(f"来源记录不存在: {entry_id}")

    origin = entry.get("origin")
    current, basis = effective_terms(grant_rows, origin, group, at, standing)
    period, period_basis = effective_terms(
        grant_rows, origin, group, int(entry["minutes"][0]), standing
    )
    minimum = minimum_override if minimum_override is not None else minimum_for(current)
    coverage = assess_coverage(entry, minimum)
    coarser = None if coverage["sufficient"] else find_coarser(entries, entry, minimum)

    violations: list[dict] = []
    if current is None:
        violations.append({"code": "no_grant", "message_zh": STATUS_LABELS_ZH["no_grant"]})
    elif purpose not in current.purposes and "*" not in current.purposes:
        violations.append({
            "code": "purpose_not_authorized",
            "message_zh": STATUS_LABELS_ZH["purpose_not_authorized"],
        })

    if current is not None and current.allowed_subjects:
        if subject is None or subject not in current.allowed_subjects:
            violations.append({
                "code": "subject_not_designated",
                "message_zh": "该事件明细只允许许可点名的排查员查看。",
            })

    permitted_fields = fields_for_purpose(current, purpose) if current else []
    # 只可能发布来源里实际存在的字段。
    released_fields = [f for f in permitted_fields if f in entry.get("fields_available", [])]
    # 未逐项列出字段的聚合记录，按“五分钟连接计数汇总”的常态约定视为含 count。
    implicit_aggregate = (
        entry.get("granularity") == "aggregate"
        and not entry.get("fields_available")
        and "count" in permitted_fields
    )
    if implicit_aggregate and "count" not in released_fields:
        released_fields.append("count")
    unavailable = [f for f in permitted_fields if f not in entry.get("fields_available", [])]

    if require_coverage and not coverage["sufficient"]:
        violations.append({
            "code": "coverage_insufficient",
            "message_zh": STATUS_LABELS_ZH["coverage_insufficient"],
        })

    allowed = not violations
    status = "ok" if allowed else violations[0]["code"]
    return {
        "entry": entry["id"],
        "origin": origin,
        "group": group,
        "purpose": purpose,
        "subject": subject,
        "at": at,
        "interval": list(entry["minutes"]),
        "sampling_note": entry.get("sampling_note"),
        "allowed": allowed,
        "status": status,
        "basis": basis,
        "period_basis": period_basis,
        "violations": violations,
        "current_terms": current.to_dict() if current else None,
        "period_terms": period.to_dict() if period else None,
        "permitted_fields": permitted_fields,
        "released_fields": released_fields if allowed else [],
        "fields_available": entry.get("fields_available", []),
        "unavailable_permitted_fields": unavailable,
        "coverage": coverage,
        "coarser": coarser,
    }


def continued_use(
    *,
    entries: Iterable[dict],
    grant_rows: Iterable[dict],
    access_fact: dict,
    at: int,
) -> dict:
    """对一条已经发生的访问，判断收窄后原用途是否仍合法、还能取什么。"""
    entry_id = access_fact.get("entry")
    group = access_fact.get("group", "joint-team")
    purpose = access_fact.get("purpose", PURPOSE_OUTAGE_REVIEW)
    accessed_at = access_fact.get("accessed_at", access_fact.get("at"))

    entry = next((e for e in entries if e["id"] == entry_id), None)
    then_terms = None
    if entry is not None and accessed_at is not None:
        then_terms = terms_for(grant_rows, entry.get("origin"), group, accessed_at)
    now_decision = disclose(
        entries=entries,
        grant_rows=grant_rows,
        entry=entry,
        group=group,
        purpose=purpose,
        at=at,
    ) if entry is not None else None

    then_fields = fields_for_purpose(then_terms, purpose) if then_terms else []
    current_fields = set(now_decision["permitted_fields"]) if now_decision else set()
    removed = [f for f in then_fields if f not in current_fields]
    purpose_survives = bool(
        now_decision and not any(v["code"] == "purpose_not_authorized"
                                 or v["code"] == "no_grant"
                                 for v in now_decision["violations"])
    )
    return {
        "access_id": access_fact.get("id"),
        "entry": entry_id,
        "group": group,
        "purpose": purpose,
        "accessed_at": accessed_at,
        "purpose_still_authorized": purpose_survives,
        "original_use_still_lawful": purpose_survives,
        "fields_at_access": then_fields,
        "fields_now": now_decision["permitted_fields"] if now_decision else [],
        "fields_removed_by_narrowing": removed,
        "retrievable_now": bool(now_decision and now_decision["allowed"]),
        "current_decision": now_decision,
    }


# ---------------------------------------------------------------------------
# 转派不继承
# ---------------------------------------------------------------------------

def reassignment_effect(grant_rows: Iterable[dict], reassignment: dict, at: int | None = None) -> dict:
    source_group = reassignment.get("from")
    target_group = reassignment.get("to")
    day = reassignment.get("day", reassignment.get("effective", 0))
    active = at is None or day <= at
    inherited = False  # 业务铁律：转派不继承原接收人的许可。
    target_terms = None
    if at is not None:
        # 转派本身不带 origin，无法定位某一县条款；这里只确认目标组没有
        # 由该转派产生的任何许可。逐县判定请用 recipient_view。
        target_terms = None
    return {
        "reassignment_id": reassignment.get("id"),
        "from": source_group,
        "to": target_group,
        "day": day,
        "active": active,
        "permission_inherited": inherited,
        "target_grant_created": False,
        "target_terms": target_terms,
        "message_zh": "转派不继承原接收组许可；新组必须取得自己的授权。",
    }


# ---------------------------------------------------------------------------
# 两县合成流量视图
# ---------------------------------------------------------------------------

def combined_traffic_view(
    *,
    entries: Iterable[dict],
    grant_rows: Iterable[dict],
    interval: list[int] | tuple[int, int],
    group: str = "joint-team",
    purpose: str = PURPOSE_OUTAGE_REVIEW,
    at: int,
    baseline_min_members: int = DEFAULT_MIN_MEMBERS,
) -> dict:
    """把同一五分钟区间内各县来源合成一个披露视图。

    汇总是否可出，按“各县自己的最小披露约定”分别判断；
    任一县覆盖不足，只暂缓该县分量并给出更粗范围，不影响其他县。
    """
    entries = list(entries)
    wanted = interval_key(interval)
    matched = [e for e in entries if interval_key(e["minutes"]) == wanted]
    standing = standing_policy(baseline_min_members)

    county_components: list[dict] = []
    merged_fields: set[str] = set()
    any_blocked = False
    for entry in sorted(matched, key=lambda e: (e.get("origin") or "", e["id"])):
        terms, basis = effective_terms(
            grant_rows, entry.get("origin"), group, at, standing
        )
        minimum = minimum_for(terms, baseline_min_members if basis == "standing_baseline" else None)
        decision = disclose(
            entries=entries,
            grant_rows=grant_rows,
            entry=entry,
            group=group,
            purpose=purpose,
            at=at,
            minimum_override=minimum,
            standing=standing,
        )
        coverage = decision["coverage"]
        component = {
            "origin": entry.get("origin"),
            "entry": entry["id"],
            "granularity": entry["granularity"],
            "devices": entry.get("devices"),
            "minimum": minimum,
            "coverage_status": "sufficient" if coverage["sufficient"] else "coverage_insufficient",
            "sampling_note": entry.get("sampling_note"),
            "basis": basis,
            "grant_id": terms.grant_id if terms else None,
            "current_fields": decision["permitted_fields"],
            "released_fields": decision["released_fields"],
            "disclosed": decision["allowed"],
            "status": decision["status"],
            "violations": decision["violations"],
            "coarser": decision["coarser"],
        }
        if decision["allowed"]:
            merged_fields.update(decision["released_fields"])
        else:
            any_blocked = True
        county_components.append(component)

    return {
        "interval": list(interval),
        "bucket_starts": [interval[0]],
        "at": at,
        "group": group,
        "purpose": purpose,
        "counties": county_components,
        "merged_disclosed": not any_blocked and bool(county_components),
        "merged_fields": sorted(merged_fields),
        "blocked_components": [c["entry"] for c in county_components if not c["disclosed"]],
    }
