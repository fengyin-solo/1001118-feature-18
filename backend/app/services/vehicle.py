"""养护车辆业务规则：状态流转、字段校验与筛选口径都收在这里。

车辆台账同时是出勤闭环的落点：派车/上工/回库会由调度服务联动改写台账的
status、驾驶员、当前里程与 version；本模块自己的动作只管送修、送检、归库
这类台账维护，且正在出勤的车辆不允许直接改台账。
"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "vehicle"
REQUIRED_FIELDS = ["车辆编号", "车辆类型", "车牌号"]
OPTIONAL_FIELDS = ["所属单位", "年检日期", "驾驶员", "当前里程"]

# 出勤闭环状态链：待命 -> 已派车 -> 上工作业 -> 回库；维修、年检中、报废为旁路状态
STATUS_STANDBY = "在库"
STATUS_DISPATCHED = "已派车"
STATUS_WORKING = "上工作业"
STATUS_REPAIR = "维修"
STATUS_INSPECTION = "年检中"
STATUS_SCRAPPED = "报废"
STATUS_ORDER = [
    STATUS_STANDBY,
    STATUS_DISPATCHED,
    STATUS_WORKING,
    STATUS_REPAIR,
    STATUS_INSPECTION,
    STATUS_SCRAPPED,
]
# 台账维护动作（派车、上工、回库由出勤调度接口驱动，不在此列）
ACTION_RULES = {
    "送修车辆": STATUS_REPAIR,
    "送检车辆": STATUS_INSPECTION,
    "维修归库": STATUS_STANDBY,
    "年检归库": STATUS_STANDBY,
    "报废车辆": STATUS_SCRAPPED,
}
# 出勤链上的状态：处于这些状态时台账动作一律拦下
ATTENDANCE_STATUSES = {STATUS_DISPATCHED, STATUS_WORKING}
UNAVAILABLE_STATUSES = {STATUS_REPAIR, STATUS_INSPECTION, STATUS_SCRAPPED}


def _to_number(value: Any) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0


class VehicleService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [
                row
                for row in rows
                if keyword in str(row.get("车辆编号", ""))
                or keyword in str(row.get("车牌号", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with store.lock:
            rows = store.rows(MODULE)
            entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            for field in OPTIONAL_FIELDS:
                if values.get(field) not in (None, ""):
                    entry[field] = values.get(field)
            if "当前里程" in entry:
                entry["当前里程"] = _to_number(entry["当前里程"])
            entry["status"] = STATUS_STANDBY
            entry["pending"] = True
            entry["abnormal"] = False
            entry["version"] = 0  # 乐观版本锁，调度并发时只允许一单改写成功
            entry["车辆状态"] = "在库待命"
            rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于养护车辆可执行范围，派车请走出勤调度"
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"养护车辆 {entry_id} 不存在或已归档"
            target = ACTION_RULES[action]
            current = entry.get("status")
            if current in ATTENDANCE_STATUSES:
                return None, f"车辆正在出勤（{current}），请先回库再{action}"
            if action in {"送修车辆", "送检车辆"} and current != STATUS_STANDBY:
                return None, f"车辆当前为「{current}」，仅在库待命车辆可{action}"
            if action == "维修归库" and current != STATUS_REPAIR:
                return None, f"车辆当前为「{current}」，未在维修中"
            if action == "年检归库" and current != STATUS_INSPECTION:
                return None, f"车辆当前为「{current}」，未在年检中"
            entry["status"] = target
            entry["pending"] = target != STATUS_SCRAPPED
            entry["abnormal"] = target == STATUS_REPAIR
            entry["version"] = int(entry.get("version", 0)) + 1
            entry["车辆状态"] = {
                STATUS_STANDBY: "在库待命",
                STATUS_REPAIR: "进厂维修",
                STATUS_INSPECTION: "年检中",
                STATUS_SCRAPPED: "已报废",
            }[target]
            return entry, f"养护车辆已{action}"
