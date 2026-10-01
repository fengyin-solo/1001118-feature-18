"""车辆出勤调度闭环：待命 → 派车 → 上工 → 回库。

设计要点：
- 接单（派车）、占用司机、推进任务状态在 store.lock 同一把锁内完成，中途任一校验
  失败整体不写入，保证事务相符；
- 车辆台账带 version 版本锁，并发派单时只有一单 CAS 成功，其余返回版本冲突；
- 调度冲突时应急指挥（防汛）任务可抢占其他在途出勤单，同级别应急不互抢；
- 回库里程只在状态首次翻到「已回库」时累计一次；离线回传按「车辆 + 任务」联合
  幂等，重复报文直接返回首条出勤单，不重复累计里程。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.store import store
from app.services import vehicle as vehicle_service
from app.services.flood import STATUS_ORDER as FLOOD_STATUSES
from app.services.patrol import STATUS_ORDER as PATROL_STATUSES
from app.services.vehicle import (
    STATUS_INSPECTION,
    STATUS_REPAIR,
    STATUS_SCRAPPED,
    STATUS_STANDBY,
)
from app.services.winter import STATUS_ORDER as WINTER_STATUSES

MODULE = "dispatch"

# 出勤闭环状态链
STATE_STANDBY = "待命"
STATE_DISPATCHED = "已派车"
STATE_WORKING = "上工作业"
STATE_RETURNED = "已回库"
STATE_INTERRUPTED = "已中断"
STATE_ORDER = [STATE_STANDBY, STATE_DISPATCHED, STATE_WORKING, STATE_RETURNED, STATE_INTERRUPTED]
# 真正占用车辆与司机的在途状态
OCCUPYING_STATES = {STATE_DISPATCHED, STATE_WORKING}
ACTIVE_STATES = {STATE_STANDBY, STATE_DISPATCHED, STATE_WORKING}
CLOSED_STATES = {STATE_RETURNED, STATE_INTERRUPTED}

# 任务类型 → 台账模块、编号/名称字段、车辆回写字段、状态序列与调度优先级
PRIORITY_EMERGENCY = 1
PRIORITY_SUPPORT = 2
PRIORITY_ROUTINE = 3
TASK_TYPES: dict[str, dict[str, Any]] = {
    "winter": {
        "label": "除雪防滑",
        "module": "winter",
        "code_field": "作业编号",
        "name_field": "作业路段",
        "vehicle_field": "作业车辆",
        "statuses": WINTER_STATUSES,
        "priority": PRIORITY_SUPPORT,
        "priority_label": "应急保障",
    },
    "flood": {
        "label": "防汛应急",
        "module": "flood",
        "code_field": "记录编号",
        "name_field": "影响路段",
        "vehicle_field": "派用车辆",
        "statuses": FLOOD_STATUSES,
        "priority": PRIORITY_EMERGENCY,
        "priority_label": "应急指挥",
    },
    "patrol": {
        "label": "巡查排班",
        "module": "patrol",
        "code_field": "巡查编号",
        "name_field": "巡查路段",
        "vehicle_field": "巡查车辆",
        "statuses": PATROL_STATUSES,
        "priority": PRIORITY_ROUTINE,
        "priority_label": "日常作业",
    },
}

TASK_LINK_ORDER = "派用出勤单"
TASK_LINK_RETURN = "回写出勤单"
TASK_LINK_MILEAGE = "本次出勤里程"

BUSY_VEHICLE_STATES = {
    vehicle_service.STATUS_DISPATCHED,
    vehicle_service.STATUS_WORKING,
}
UNAVAILABLE_VEHICLE_STATES = {STATUS_REPAIR, STATUS_INSPECTION, STATUS_SCRAPPED}


class DispatchError(Exception):
    """调度规则被违反时抛出；code 供前端区分版本冲突等场景。"""

    def __init__(self, message: str, code: str = "DISPATCH_REJECTED") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _clean(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _mileage(value: Any, field: str) -> float:
    text = _clean(value)
    if not text:
        raise DispatchError(f"缺少{field}，无法形成出勤闭环", "VALIDATION_ERROR")
    try:
        amount = float(text)
    except ValueError:
        raise DispatchError(f"{field}不是有效数值：{text}", "VALIDATION_ERROR") from None
    if amount < 0:
        raise DispatchError(f"{field}不能为负数", "VALIDATION_ERROR")
    return amount


def _parse_annual_date(value: Any) -> date | None:
    text = _clean(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _task_config(task_type: str) -> dict[str, Any]:
    config = TASK_TYPES.get(task_type)
    if config is None:
        raise DispatchError(f"不支持的任务类型「{task_type}」，可选：{ '、'.join(TASK_TYPES)}", "VALIDATION_ERROR")
    return config


def _find_task(config: dict[str, Any], task_id: int) -> dict[str, Any]:
    task = store.find(config["module"], task_id)
    if task is None:
        raise DispatchError(f"{config['label']}任务 {task_id} 不存在或已归档", "TASK_NOT_FOUND")
    return task


def _set_task_status(task: dict[str, Any], statuses: list[str], index: int) -> None:
    target = statuses[index]
    task["status"] = target
    # 与各业务模块既有约定保持一致：最后一个状态才算处理完毕
    task["pending"] = target != statuses[-1]


def _vehicle_label(vehicle: dict[str, Any]) -> str:
    return f"{vehicle.get('车辆编号', '')}（{vehicle.get('车牌号', '')}）"


def _task_label(config: dict[str, Any], task: dict[str, Any]) -> str:
    return f"{config['label']}·{task.get(config['code_field'], '')} {task.get(config['name_field'], '')}".strip()


def _annual_inspection_block(vehicle: dict[str, Any]) -> str | None:
    """年检/维修/报废硬卡：返回拦截原因；放行返回 None。"""
    current = vehicle.get("status")
    if current in UNAVAILABLE_VEHICLE_STATES:
        return f"车辆当前「{current}」，不得派单"
    due = _parse_annual_date(vehicle.get("年检日期"))
    if due is None:
        return "车辆年检日期缺失或格式无法核验，不得派单"
    if due < date.today():
        return f"车辆年检已于 {due.isoformat()} 到期，不得派单"
    return None


class DispatchService:
    # ---- 查询 ----------------------------------------------------------------

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        task_type: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [
                row
                for row in rows
                if keyword in str(row.get("出勤单号", ""))
                or keyword in str(row.get("车牌号", ""))
                or keyword in str(row.get("驾驶员", ""))
                or keyword in str(row.get("任务名称", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if task_type:
            rows = [row for row in rows if row.get("task_type") == task_type]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def stats(self) -> dict[str, int]:
        rows = store.rows(MODULE)
        result = {state: 0 for state in STATE_ORDER}
        for row in rows:
            state = str(row.get("status", ""))
            if state in result:
                result[state] += 1
        result["在途合计"] = sum(result[state] for state in OCCUPYING_STATES)
        return result

    def task_options(self) -> list[dict[str, Any]]:
        """给派车表单提供可选任务清单：仅各任务台账的初始待办态可被新单关联。"""
        options: list[dict[str, Any]] = []
        for task_type, config in TASK_TYPES.items():
            statuses = config["statuses"]
            for task in store.rows(config["module"]):
                if task.get("status") == statuses[0] and not task.get(TASK_LINK_ORDER):
                    options.append({
                        "task_type": task_type,
                        "task_id": task.get("id"),
                        "label": _task_label(config, task),
                        "priority": config["priority_label"],
                        "status": task.get("status"),
                    })
        options.sort(key=lambda item: item["priority"])
        return options

    def vehicle_options(self) -> list[dict[str, Any]]:
        """给派车表单提供车辆清单，并直接标注能否派单（维修/年检到期一目了然）。"""
        options: list[dict[str, Any]] = []
        for vehicle in store.rows(vehicle_service.MODULE):
            reason = _annual_inspection_block(vehicle)
            options.append({
                "vehicle_id": vehicle.get("id"),
                "label": _vehicle_label(vehicle),
                "驾驶员": vehicle.get("驾驶员", ""),
                "当前里程": vehicle.get("当前里程", 0),
                "status": vehicle.get("status"),
                "version": vehicle.get("version", 0),
                "dispatchable": reason is None and vehicle.get("status") not in BUSY_VEHICLE_STATES,
                "block_reason": reason or ("车辆出勤中" if vehicle.get("status") in BUSY_VEHICLE_STATES else ""),
            })
        return options

    # ---- 状态链 --------------------------------------------------------------

    def plan_order(self, values: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """建立待命出勤单（排班计划），此阶段不占用车辆与司机。"""
        with store.lock:
            return self._plan_tx(values)

    def dispatch_order(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """派车接单：车辆放行、司机占用、任务进入作业态，一个事务内全部完成。"""
        with store.lock:
            return self._dispatch_tx(entry_id, values)

    def dispatch_many(self, items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        """批量并发调度：应急指挥优先排序后逐个事务内派单，只返回成败明细，不抛异常。"""
        def priority_of(item: dict[str, Any]) -> int:
            return _task_config(str(item.get("task_type", "")))["priority"]

        ordered = sorted(items, key=priority_of)
        succeeded: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        with store.lock:
            for item in ordered:
                try:
                    order, message = self._plan_tx(item)
                    order, message = self._dispatch_tx(int(order["id"]), item)
                    succeeded.append({"出勤单号": order["出勤单号"], "message": message})
                except DispatchError as exc:
                    rejected.append({
                        "vehicle_id": item.get("vehicle_id"),
                        "task_type": item.get("task_type"),
                        "task_id": item.get("task_id"),
                        "code": exc.code,
                        "message": exc.message,
                    })
        return {"succeeded": succeeded, "rejected": rejected}

    def start_work(self, entry_id: int, values: dict[str, Any] | None = None) -> tuple[dict[str, Any], str]:
        """上工：已派车 → 上工作业，车辆台账同步。"""
        with store.lock:
            order = self._require_order(entry_id)
            if order["status"] != STATE_DISPATCHED:
                raise DispatchError(f"出勤单当前为「{order['status']}」，仅已派车单可以上工")
            vehicle = store.find(vehicle_service.MODULE, int(order["vehicle_id"]))
            if vehicle is None:
                raise DispatchError("关联车辆台账不存在", "VEHICLE_NOT_FOUND")
            order["status"] = STATE_WORKING
            order["上工时间"] = _clean((values or {}).get("上工时间")) or _now()
            vehicle["status"] = vehicle_service.STATUS_WORKING
            vehicle["车辆状态"] = f"上工作业（{order['出勤单号']}）"
            vehicle["version"] = int(vehicle.get("version", 0)) + 1
            return order, f"{order['出勤单号']}已上工"

    def return_garage(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """回库：里程回写车辆台账、任务推进到完成态，里程只累计这一次。"""
        with store.lock:
            return self._return_tx(entry_id, values, offline=False)

    def report_return(self, values: dict[str, Any]) -> tuple[dict[str, Any], str, bool]:
        """离线回传：按车辆 + 任务联合幂等，返回（出勤单, 说明, 是否重复报文）。"""
        with store.lock:
            vehicle_id = self._require_id(values, "vehicle_id", "车辆台账ID")
            task_type = _clean(values.get("task_type"))
            task_id = self._require_id(values, "task_id", "任务ID")
            config = _task_config(task_type)
            task = _find_task(config, task_id)

            # 幂等口径：同一车辆 + 同一任务只认首条回库
            for order in store.rows(MODULE):
                if (
                    int(order.get("vehicle_id", 0)) == vehicle_id
                    and order.get("task_type") == task_type
                    and int(order.get("task_id", 0)) == task_id
                ):
                    if order["status"] == STATE_RETURNED:
                        return order, "该车辆对此任务的出勤已回传过，按幂等规则未重复累计里程", True
                    if order["status"] in OCCUPYING_STATES:
                        entry, message = self._return_tx(int(order["id"]), values, offline=True)
                        return entry, message, False
                    if order["status"] == STATE_STANDBY:
                        # 现场已完成但计划单还没派车：直接补齐派车再回库
                        self._dispatch_tx(int(order["id"]), values, allow_task_active=True)
                        entry, message = self._return_tx(int(order["id"]), values, offline=True)
                        return entry, message, False
                    # 已中断单没有累计过里程，落到下方按新单补建闭环
                    continue

            # 完全没有出勤单（或旧单已中断）：离线期间现场走完了闭环，这里一次性补建
            order = self._build_offline_order(vehicle_id, task_type, config, task, values)
            return order, "离线回传已补建出勤闭环", False

    # ---- 事务内实现（调用方必须持有 store.lock） --------------------------------

    def _plan_tx(self, values: dict[str, Any]) -> tuple[dict[str, Any], str]:
        vehicle_id = self._require_id(values, "vehicle_id", "车辆台账ID")
        task_type = _clean(values.get("task_type"))
        task_id = self._require_id(values, "task_id", "任务ID")
        config = _task_config(task_type)
        vehicle = store.find(vehicle_service.MODULE, vehicle_id)
        if vehicle is None:
            raise DispatchError(f"养护车辆 {vehicle_id} 不存在或已归档", "VEHICLE_NOT_FOUND")
        task = _find_task(config, task_id)

        for existing in store.rows(MODULE):
            if (
                int(existing.get("vehicle_id", 0)) == vehicle_id
                and existing.get("task_type") == task_type
                and int(existing.get("task_id", 0)) == task_id
                and existing.get("status") in ACTIVE_STATES
            ):
                raise DispatchError("该车对该任务已有在途出勤单，请勿重复排班", "DUPLICATE_PLAN")

        rows = store.rows(MODULE)
        entry_id = _next_id(rows)
        order = {
            "id": entry_id,
            "出勤单号": self._order_no(entry_id),
            "status": STATE_STANDBY,
            "pending": True,
            "abnormal": False,
            "task_type": task_type,
            "任务类型": config["label"],
            "task_id": task_id,
            "任务编号": task.get(config["code_field"], ""),
            "任务名称": task.get(config["name_field"], ""),
            "任务优先级": config["priority_label"],
            "priority": config["priority"],
            "vehicle_id": vehicle_id,
            "车辆编号": vehicle.get("车辆编号", ""),
            "车牌号": vehicle.get("车牌号", ""),
            "计划驾驶员": _clean(values.get("计划驾驶员")) or _clean(vehicle.get("驾驶员")),
            "驾驶员": "",
            "实际驾驶员": "",
            "出车里程": None,
            "回库里程": None,
            "出勤里程": None,
            "派车时间": None,
            "上工时间": None,
            "回库时间": None,
            "中断时间": None,
            "中断原因": "",
            "离线回传": False,
            "备注": _clean(values.get("备注")),
        }
        rows.append(order)
        return order, f"{order['出勤单号']}已进入待命排班"

    def _dispatch_tx(
        self,
        entry_id: int,
        values: dict[str, Any],
        *,
        allow_task_active: bool = False,
    ) -> tuple[dict[str, Any], str]:
        order = self._require_order(entry_id)
        if order["status"] != STATE_STANDBY:
            raise DispatchError(f"出勤单当前为「{order['status']}」，仅待命单可以派车")

        config = _task_config(str(order["task_type"]))
        task = store.find(config["module"], int(order["task_id"]))
        if task is None:
            raise DispatchError("关联任务已被删除，无法派车", "TASK_NOT_FOUND")

        vehicle = store.find(vehicle_service.MODULE, int(order["vehicle_id"]))
        if vehicle is None:
            raise DispatchError("关联车辆台账不存在", "VEHICLE_NOT_FOUND")

        driver = _clean(values.get("驾驶员")) or _clean(order.get("计划驾驶员"))
        if "驾驶员" in values and not _clean(values.get("驾驶员")):
            # 明确传了空驾驶员视为拒单，不能静默改挂别人
            raise DispatchError("派车必须指定实际接单驾驶员", "DRIVER_REQUIRED")
        if not driver:
            raise DispatchError("派车必须指定实际接单驾驶员", "DRIVER_REQUIRED")

        # 硬卡：维修 / 年检中 / 报废 / 年检逾期
        reason = _annual_inspection_block(vehicle)
        if reason:
            raise DispatchError(f"{_vehicle_label(vehicle)}：{reason}", "VEHICLE_UNAVAILABLE")

        expected_version = values.get("version")
        expected_version = int(expected_version) if _clean(expected_version) else None

        # 冲突检测优先于任务状态校验：车辆/司机被在途单占用本身就是硬冲突
        # （应急指挥可抢占低级任务，同级应急不互抢；版本锁在此一并判定）
        preempt_messages = self._resolve_conflicts(
            vehicle=vehicle,
            driver=driver,
            priority=int(order["priority"]),
            priority_label=str(order["任务优先级"]),
            keep_vehicle_id=int(vehicle["id"]),
            expected_version=expected_version,
        )

        # 过了冲突关，再核任务是否还能接车
        statuses = config["statuses"]
        if task.get(TASK_LINK_ORDER) and task.get(TASK_LINK_ORDER) != order["出勤单号"]:
            raise DispatchError("任务已关联其他在途出勤单", "TASK_OCCUPIED")
        if task.get("status") != statuses[0] and not allow_task_active:
            raise DispatchError(
                f"{config['label']}任务当前为「{task.get('status')}」，仅待办任务可派车",
                "TASK_NOT_PENDING",
            )

        # —— 事务写：接单、占司机、更新任务全部落地，要么全做要么全不做 ——
        start_mileage = _mileage(vehicle.get("当前里程"), "车辆当前里程")
        vehicle["status"] = vehicle_service.STATUS_DISPATCHED
        vehicle["车辆状态"] = f"已派车（{order['出勤单号']}）"
        vehicle["驾驶员"] = driver
        vehicle["version"] = int(vehicle.get("version", 0)) + 1
        vehicle["pending"] = True

        order["status"] = STATE_DISPATCHED
        order["驾驶员"] = driver
        order["出车里程"] = start_mileage
        order["派车时间"] = _clean(values.get("派车时间")) or _now()
        if _clean(values.get("计划驾驶员")):
            order["计划驾驶员"] = _clean(values["计划驾驶员"])

        if task.get("status") == statuses[0]:
            _set_task_status(task, statuses, 1)
        task[config["vehicle_field"]] = _vehicle_label(vehicle)
        task[TASK_LINK_ORDER] = order["出勤单号"]

        message = f"{order['出勤单号']}已派车，驾驶员 {driver} 接单"
        if preempt_messages:
            message += "；应急抢占：" + "、".join(preempt_messages)
        return order, message

    def _resolve_conflicts(
        self,
        *,
        vehicle: dict[str, Any],
        driver: str,
        priority: int,
        priority_label: str,
        keep_vehicle_id: int,
        expected_version: int | None,
    ) -> list[str]:
        """找出在途冲突单；应急指挥可抢占低级任务，其余冲突直接拒绝。返回抢占说明。"""
        conflicts: list[dict[str, Any]] = []
        for other in store.rows(MODULE):
            if other.get("status") not in OCCUPYING_STATES:
                continue
            hit_vehicle = int(other.get("vehicle_id", 0)) == int(vehicle["id"])
            hit_driver = _clean(other.get("驾驶员")) == driver
            if hit_vehicle or hit_driver:
                conflicts.append(other)

        # 台账状态与出勤单不一致时（理论上不应出现），保守拒绝，不允许版本误抢
        if vehicle.get("status") in BUSY_VEHICLE_STATES and not any(
            int(c.get("vehicle_id", 0)) == int(vehicle["id"]) for c in conflicts
        ):
            raise DispatchError("车辆台账显示出勤中但缺少在途出勤单，请先核账", "VEHICLE_STATE_CONFLICT")

        preempt_notes: list[str] = []
        for other in conflicts:
            other_priority = int(other.get("priority", PRIORITY_ROUTINE))
            if priority == PRIORITY_EMERGENCY and other_priority != PRIORITY_EMERGENCY:
                note = self._preempt_order(other, reason=f"{priority_label}任务应急抢占", keep_vehicle_id=keep_vehicle_id)
                preempt_notes.append(note)
                continue
            raise DispatchError(
                f"车辆/司机已被 {other['出勤单号']}（{other['任务类型']}·{other['任务编号']}，"
                f"{other.get('任务优先级')}）占用，当前任务不具备抢占优先级",
                "RESOURCE_CONFLICT",
            )

        # 无冲突时版本锁才作数：防止两单拿着同一旧版本并发派车
        if not conflicts and expected_version is not None:
            if int(vehicle.get("version", 0)) != expected_version:
                raise DispatchError(
                    "车辆台账版本已变化，可能有其他调度同时下单，请刷新后重试",
                    "VERSION_CONFLICT",
                )
        return preempt_notes

    def _preempt_order(self, order: dict[str, Any], *, reason: str, keep_vehicle_id: int) -> str:
        """把被抢占的在途单置为已中断，其任务回退待办并释放占用车辆。"""
        config = _task_config(str(order["task_type"]))
        task = store.find(config["module"], int(order["task_id"]))
        if task is not None:
            _set_task_status(task, config["statuses"], 0)
            task.pop(TASK_LINK_ORDER, None)
            if task.get(config["vehicle_field"], "").startswith(str(order.get("车辆编号", ""))):
                task[config["vehicle_field"]] = ""
        other_vehicle = store.find(vehicle_service.MODULE, int(order["vehicle_id"]))
        if other_vehicle is not None and int(other_vehicle["id"]) != keep_vehicle_id:
            other_vehicle["status"] = STATUS_STANDBY
            other_vehicle["车辆状态"] = f"在库待命（{reason}释放）"
            other_vehicle["version"] = int(other_vehicle.get("version", 0)) + 1
        order["status"] = STATE_INTERRUPTED
        order["pending"] = False
        order["abnormal"] = True
        order["中断时间"] = _now()
        order["中断原因"] = reason
        return f"{order['出勤单号']}（{order['任务类型']}·{order['任务编号']}）"

    def _return_tx(
        self,
        entry_id: int,
        values: dict[str, Any],
        *,
        offline: bool,
    ) -> tuple[dict[str, Any], str]:
        order = self._require_order(entry_id)
        if order["status"] not in OCCUPYING_STATES:
            raise DispatchError(f"出勤单当前为「{order['status']}」，仅在途出勤单可以回库")

        vehicle = store.find(vehicle_service.MODULE, int(order["vehicle_id"]))
        if vehicle is None:
            raise DispatchError("关联车辆台账不存在", "VEHICLE_NOT_FOUND")
        config = _task_config(str(order["task_type"]))
        task = store.find(config["module"], int(order["task_id"]))
        if task is None:
            raise DispatchError("关联任务已被删除，无法回写", "TASK_NOT_FOUND")

        start_mileage = order.get("出车里程")
        if start_mileage is None:
            start_mileage = _mileage(vehicle.get("当前里程"), "出车里程")
        start_mileage = float(start_mileage)
        end_mileage = _mileage(values.get("回库里程"), "回库里程表读数")
        if end_mileage < start_mileage:
            raise DispatchError(
                f"回库里程 {end_mileage:g} 小于出车里程 {start_mileage:g}，读数有误",
                "MILEAGE_INVALID",
            )
        trip_mileage = round(end_mileage - start_mileage, 1)
        actual_driver = _clean(values.get("实际驾驶员")) or _clean(order.get("驾驶员")) or _clean(values.get("驾驶员"))
        if not actual_driver:
            raise DispatchError("回库必须保留实际驾驶员", "DRIVER_REQUIRED")

        # —— 台账回写：历史里程与实际驾驶员按本次出勤保留 ——
        vehicle["status"] = STATUS_STANDBY
        vehicle["车辆状态"] = "在库待命"
        vehicle["当前里程"] = end_mileage
        vehicle["驾驶员"] = actual_driver
        vehicle["version"] = int(vehicle.get("version", 0)) + 1
        vehicle["pending"] = True
        vehicle["abnormal"] = False

        order["status"] = STATE_RETURNED
        order["pending"] = False
        order["回库里程"] = end_mileage
        order["出车里程"] = start_mileage
        order["出勤里程"] = trip_mileage
        order["实际驾驶员"] = actual_driver
        order["回库时间"] = _clean(values.get("回库时间")) or _now()
        order["离线回传"] = bool(offline)

        # 任务回写：推进到完成态（已完成/已处置），保留派用车辆与出勤里程
        _set_task_status(task, config["statuses"], 2)
        task[TASK_LINK_RETURN] = order["出勤单号"]
        task[TASK_LINK_MILEAGE] = trip_mileage

        scope = "离线回传" if offline else "回库"
        return order, (
            f"{order['出勤单号']}{scope}完成：{_vehicle_label(vehicle)} 行驶 {trip_mileage:g}km，"
            f"{config['label']}任务已回写"
        )

    def _build_offline_order(
        self,
        vehicle_id: int,
        task_type: str,
        config: dict[str, Any],
        task: dict[str, Any],
        values: dict[str, Any],
    ) -> dict[str, Any]:
        """离线期间无单作业：在一个事务内补建待命单 → 派车 → 回库整段闭环。"""
        vehicle = store.find(vehicle_service.MODULE, vehicle_id)
        if vehicle is None:
            raise DispatchError(f"养护车辆 {vehicle_id} 不存在或已归档", "VEHICLE_NOT_FOUND")
        reason = _annual_inspection_block(vehicle)
        if reason:
            raise DispatchError(f"{_vehicle_label(vehicle)}：{reason}", "VEHICLE_UNAVAILABLE")
        statuses = config["statuses"]
        if task.get(TASK_LINK_RETURN):
            raise DispatchError("任务已存在回库回写记录，拒绝重复累计里程", "TASK_CLOSED")
        if task.get("status") not in statuses[:2]:
            raise DispatchError(f"任务当前为「{task.get('status')}」，不在可出勤区间", "TASK_NOT_PENDING")

        driver = _clean(values.get("实际驾驶员")) or _clean(values.get("驾驶员")) or _clean(vehicle.get("驾驶员"))
        if not driver:
            raise DispatchError("离线回传必须提供实际驾驶员", "DRIVER_REQUIRED")

        order, _ = self._plan_tx({
            "vehicle_id": vehicle_id,
            "task_type": task_type,
            "task_id": task["id"],
            "计划驾驶员": driver,
            "备注": "离线补建",
        })
        order["离线回传"] = True
        dispatch_values = {
            "驾驶员": driver,
            "派车时间": values.get("派车时间"),
            "allow_bypass": True,
        }
        self._dispatch_tx(int(order["id"]), dispatch_values, allow_task_active=True)
        returned, _ = self._return_tx(int(order["id"]), values, offline=True)
        return returned

    # ---- 辅助 ----------------------------------------------------------------

    def _require_order(self, entry_id: int) -> dict[str, Any]:
        order = store.find(MODULE, entry_id)
        if order is None:
            raise DispatchError(f"出勤单 {entry_id} 不存在或已归档", "ORDER_NOT_FOUND")
        return order

    @staticmethod
    def _require_id(values: dict[str, Any], key: str, label: str) -> int:
        raw = values.get(key)
        try:
            return int(raw)
        except (TypeError, ValueError):
            raise DispatchError(f"缺少或非法的{label}：{raw!r}", "VALIDATION_ERROR") from None

    @staticmethod
    def _order_no(entry_id: int) -> str:
        return f"DISP-{date.today().strftime('%Y%m%d')}-{entry_id:04d}"


dispatch_service = DispatchService()
