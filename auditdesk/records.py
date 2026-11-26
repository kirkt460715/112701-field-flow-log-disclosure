"""本地合成元数据的读取、规范化与时间工具。

本模块不依赖第三方库，也不修改 ``records/`` 中的原始材料；
所有运行期变化（新许可、回执、审计事实）都保存在独立状态文件中。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from records_view import load_sources

# 五分钟级连接计数的区间步长（分钟编号）。
BUCKET_MINUTES = 5

# 没有显式约定最小聚合成员数时采用的防御性缺省值。
# 种子材料中 county-b 的许可明确给出 5；12/2/19 的设备数也围绕 5 构造。
DEFAULT_MIN_MEMBERS = 5

TIME_KEYS = (
    "effective", "at", "day", "accessed_at", "created", "receipt_at",
    "requested_at", "acked_at", "observed_at", "timestamp",
)


class AuditDeskError(ValueError):
    """所有可预期的审核台业务错误。"""


# ---------------------------------------------------------------------------
# 读取与路径
# ---------------------------------------------------------------------------

def default_records_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "records"


def load_seed_sources(records_dir: str | Path | None = None) -> dict[str, list[dict]]:
    return load_sources(Path(records_dir) if records_dir else default_records_dir())


def read_json_records(path: str | Path) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("records"), list):
        data = data["records"]
    if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
        raise AuditDeskError(f"{path} 需要对象数组（或含 records 数组的对象）")
    return data


# ---------------------------------------------------------------------------
# 区间
# ---------------------------------------------------------------------------

def normalize_minutes(value: Any) -> list[int]:
    if not (isinstance(value, (list, tuple)) and len(value) == 2):
        raise AuditDeskError(f"分钟区间必须是两个端点: {value!r}")
    start, end = value
    if not all(isinstance(x, int) and not isinstance(x, bool) for x in (start, end)):
        raise AuditDeskError(f"分钟编号必须为整数: {value!r}")
    if start > end:
        raise AuditDeskError(f"分钟区间起点不能晚于终点: {value!r}")
    return [start, end]


def interval_key(minutes: Iterable[int]) -> tuple[int, int]:
    a, b = tuple(minutes)  # type: ignore[assignment]
    return int(a), int(b)


def bucket_starts(minutes: Iterable[int], step: int = BUCKET_MINUTES) -> list[int]:
    """返回半开区间 [start, end) 覆盖的五分钟桶起点。"""
    start, end = tuple(minutes)  # type: ignore[misc]
    if end == start:
        return [start]
    return list(range(start, end, step))


def interval_contains(outer: Iterable[int], inner: Iterable[int]) -> bool:
    oa, ob = tuple(outer)  # type: ignore[misc]
    ia, ib = tuple(inner)  # type: ignore[misc]
    return oa <= ia and ib <= ob


# ---------------------------------------------------------------------------
# 来源（registry）规范化
# ---------------------------------------------------------------------------

def sampling_note_of(row: dict) -> str | None:
    note = row.get("sampling_note")
    if note is None:
        change = row.get("sampling_change")
        if isinstance(change, dict):
            note = change.get("note") or change.get("description")
        elif change is not None:
            note = str(change)
    return None if note is None else str(note)


def normalize_entry(row: dict) -> dict:
    if not isinstance(row.get("id"), str) or not row["id"]:
        raise AuditDeskError("来源记录缺少字符串 id")
    minutes = normalize_minutes(row.get("minutes"))
    fields = list(row.get("fields") or [])
    devices = row.get("devices")
    if devices is not None and (not isinstance(devices, int) or isinstance(devices, bool)):
        raise AuditDeskError(f"{row['id']} 的 devices 必须是整数")
    if "site_token" in fields:
        granularity = "device_detail"
    elif fields:
        granularity = "listed_fields"
    else:
        granularity = "aggregate"
    return {
        "id": row["id"],
        "origin": row.get("origin"),
        "minutes": minutes,
        "fields_available": fields,
        "devices": devices,
        "sampling_note": sampling_note_of(row),
        "granularity": granularity,
        "raw": row,
    }


def normalize_entries(rows: Iterable[dict]) -> list[dict]:
    return [normalize_entry(row) for row in rows]


# ---------------------------------------------------------------------------
# 时间
# ---------------------------------------------------------------------------

def record_time(row: dict) -> int | None:
    """提取一条记录用于默认时钟的时间戳（取已知字段中的最大值）。"""
    values: list[int] = []
    for key in TIME_KEYS:
        value = row.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            values.append(value)
    minutes = row.get("minutes")
    if isinstance(minutes, list) and minutes and all(isinstance(x, int) for x in minutes):
        values.extend(minutes)
    ack = row.get("acknowledgment")
    if isinstance(ack, dict):
        for key in TIME_KEYS:
            value = ack.get(key)
            if isinstance(value, int):
                values.append(value)
    return max(values) if values else None


def infer_clock(seed_sources: dict[str, list[dict]], extra: Iterable[dict] = ()) -> int:
    values: list[int] = []
    for rows in seed_sources.values():
        for row in rows:
            t = record_time(row)
            if t is not None:
                values.append(t)
    for row in extra:
        t = record_time(row)
        if t is not None:
            values.append(t)
    return max(values) if values else 0
