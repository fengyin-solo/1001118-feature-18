"""车辆出勤闭环业务规则。

状态链：待命 → 已派车(接单) → 作业中(上工) → 已回库(回库)，异常分支为已取消。

关键口径：
- 接单是一次事务：车辆台账占用、司机占用、任务回写要么全成、要么全不成；
- 车辆台账带 version 乐观锁，并发派车只有一单能成功（版本过期返回 409）；
- 年检过期 / 维修中 / 报废的车辆不得派单；已在出车的车辆按占用冲突处理；
- 车辆或司机被占用时，应急指挥任务可抢占非应急任务，其余一律冲突；
- 回库才累计里程，历史里程与实际驾驶员按出勤单保留；
- 离线回传按「车辆 + 任务」联合幂等，重复回传不重复累计里程。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.store import store

MODULE = "dispatch"
IDEMPOTENT_MODULE = "dispatch_idempotent"
VEHICLE_MODULE = "vehicle"

# 出勤状态链：待命是车辆台账的初始态，出勤单从「已派车」起单
DISPATCHED = "已派车"
WORKING = "作业中"
RETURNED = "已回库"
CANCELED = "已取消"
ACTIVE_STATUS = (DISPATCHED, WORKING)

# 三类可派任务的模块配置：台账模块、编号字段、地点字段、车辆回填字段以及状态口径
TASK_MODULES: dict[str, dict[str, str]] = {
    "winter": {
        "module": "winter", "label": "除雪防滑", "code": "作业编号", "place": "作业路段",
        "vehicle_field": "作业车辆", "active": "作业中", "done": "已完成",
    },
    "flood": {
        "module": "flood", "label": "防汛应急", "code": "记录编号", "place": "影响路段",
        "vehicle_field": "投入车辆", "active": "响应中", "done": "已处置",
    },
    "patrol": {
        "module": "patrol", "label": "日常巡查", "code": "巡查编号", "place": "巡查路段",
        "vehicle_field": "巡查车辆", "active": "巡查中", "done": "已完成",
    },
}
EMERGENCY_DEFAULT = {"winter": False, "flood": True, "patrol": False}


class DispatchConflict(Exception):
    """409：版本过期或调度冲突。"""


class DispatchRejected(Exception):
    """400：入参或业务前置条件不满足。"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _to_number(value: Any) -> float:
    """台账历史里程可能是字符串，统一容错解析。"""
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _gen_dispatch_no() -> str:
    today = datetime.now().strftime("%Y%m%d")
    prefix = f"DISP-{today}-"
    used = {row.get("出勤编号") for row in store.rows(MODULE)}
    seq = 1
    while f"{prefix}{seq:04d}" in used:
        seq += 1
    return f"{prefix}{seq:04d}"


def _active_orders() -> list[dict[str, Any]]:
    return [row for row in store.rows(MODULE) if row.get("status") in ACTIVE_STATUS]


def _inspection_expired(vehicle: dict[str, Any]) -> bool:
    raw = str(vehicle.get("年检日期") or "").strip()
    if not raw:
        return False
    try:
        due = datetime.strptime(raw[:10], "%Y-%m-%d").date()
    except ValueError:
        return False
    return due < date.today()


class DispatchService:
    def __init__(self) -> None:
        # 本次接单抢占掉的非应急出勤单编号，随接口返回给调度台提示
        self._last_preempted: list[str] = []

    # ---------- 查询 ----------
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
                row for row in rows
                if keyword in str(row.get("出勤编号", ""))
                or keyword in str(row.get("车牌号", ""))
                or keyword in str(row.get("驾驶员", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if task_type:
            rows = [row for row in rows if row.get("任务类型") == task_type]
        rows = sorted(rows, key=lambda row: int(row.get("id", 0)), reverse=True)
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def ledger(self) -> list[dict[str, Any]]:
        """车辆台账视图：在版本字段之外附上当前占用该车的出勤单。"""
        active_by_vehicle = {row.get("车辆ID"): row for row in _active_orders()}
        result = []
        for vehicle in store.rows(VEHICLE_MODULE):
            order = active_by_vehicle.get(vehicle.get("id"))
            reason = self._hard_block(vehicle)
            if reason is None and order is not None:
                reason = f"车辆已在出勤单 {order.get('出勤编号')} 作业中"
            result.append({
                **vehicle,
                "可派单": reason is None,
                "不可派单原因": reason,
                "当前出勤编号": order.get("出勤编号") if order else None,
                "当前任务": order.get("任务名称") if order else None,
            })
        return result

    def stats(self) -> dict[str, Any]:
        orders = store.rows(MODULE)
        vehicles = store.rows(VEHICLE_MODULE)
        return {
            "待命车辆": sum(1 for v in vehicles if v.get("status") == "在库"),
            "出车车辆": sum(1 for v in vehicles if v.get("status") == "出车作业"),
            "维修车辆": sum(1 for v in vehicles if v.get("status") == "维修"),
            "待派车出勤单": sum(1 for o in orders if o.get("status") == DISPATCHED),
            "作业中出勤单": sum(1 for o in orders if o.get("status") == WORKING),
            "今日已回库": sum(1 for o in orders if o.get("status") == RETURNED),
            "占用司机数": len({o.get("实际驾驶员") or o.get("驾驶员") for o in _active_orders()}),
        }

    # ---------- 派车接单（事务核心） ----------
    def accept_dispatch(
        self,
        *,
        vehicle_id: int,
        task_type: str,
        task_id: int,
        driver: str | None = None,
        emergency: bool | None = None,
        expected_version: int | None = None,
        client_token: str | None = None,
        remark: str | None = None,
    ) -> tuple[dict[str, Any], list[str]]:
        task_type = (task_type or "").strip().lower()
        config = TASK_MODULES.get(task_type)
        if config is None:
            raise DispatchRejected(f"任务类型「{task_type}」不支持派车，仅支持除雪/防汛/巡查")
        driver = (driver or "").strip()

        with store.lock:
            # 防重复提交：同一令牌只成一次单
            if client_token:
                dup = self._find_token(client_token)
                if dup is not None:
                    return dup, ["该派车请求已提交过，直接返回原出勤单"]

            # 接单、占用司机、更新任务在同一事务里完成
            with store.transaction(MODULE, VEHICLE_MODULE, config["module"]):
                vehicle = store.find(VEHICLE_MODULE, vehicle_id)
                if vehicle is None:
                    raise DispatchRejected(f"车辆 {vehicle_id} 不存在或已报废注销")
                task = store.find(config["module"], task_id)
                if task is None:
                    raise DispatchRejected(f"{config['label']}任务 {task_id} 不存在或已归档")
                if task.get("status") == config["done"]:
                    raise DispatchRejected(f"{config['label']}任务 {task_id} 已完成，不能重复派车")

                is_emergency = EMERGENCY_DEFAULT[task_type] if emergency is None else emergency

                # 版本锁：并发派车携带同一版本时，只允许第一单成功
                if expected_version is not None and int(vehicle.get("version", 0)) != expected_version:
                    raise DispatchConflict(
                        f"车辆台账版本已变更（当前 v{vehicle.get('version', 0)}），并发派车冲突，请刷新后重试"
                    )

                # 硬门槛：年检过期 / 维修中 / 报废不得派单（占用情况交给冲突处理）
                reason = self._hard_block(vehicle)
                if reason:
                    raise DispatchRejected(reason)

                if not driver:
                    driver = str(vehicle.get("驾驶员") or "").strip()
                if not driver:
                    raise DispatchRejected("车辆台账未登记驾驶员，派车前请先指定司机")

                # 冲突处理：应急指挥任务优先，可抢占非应急占用；其余冲突直接拒绝
                self._resolve_occupation(vehicle_id, driver, task_id, task_type, is_emergency)

                start_mileage = _to_number(vehicle.get("当前里程"))
                order = {
                    "id": _next_id(store.rows(MODULE)),
                    "出勤编号": _gen_dispatch_no(),
                    "任务类型": task_type,
                    "任务名称": f"{config['label']} {task.get(config['code'])}",
                    "任务模块": config["module"],
                    "任务ID": task_id,
                    "车辆ID": vehicle_id,
                    "车辆编号": vehicle.get("车辆编号"),
                    "车牌号": vehicle.get("车牌号"),
                    "驾驶员": driver,
                    "实际驾驶员": driver,
                    "应急任务": is_emergency,
                    "status": DISPATCHED,
                    "接单版本": int(vehicle.get("version", 0)),
                    "出车里程": start_mileage,
                    "回库里程": None,
                    "出勤里程": None,
                    "离线回传": False,
                    "已抢占": False,
                    "备注": remark or "",
                    "pending": True,
                    "abnormal": False,
                    "派车时间": _now(),
                    "上工时间": None,
                    "回库时间": None,
                    "任务接单前状态": task.get("status"),
                    "任务接单前车辆": task.get(config["vehicle_field"]),
                    "client_token": client_token,
                }
                store.rows(MODULE).append(order)

                # 车辆台账：占用车辆 + 版本号递增
                vehicle["status"] = "出车作业"
                vehicle["车辆状态"] = "出车作业"
                vehicle["驾驶员"] = driver
                vehicle["version"] = int(vehicle.get("version", 0)) + 1

                # 任务回写：进入作业/响应态并登记出勤编号与派车车辆
                task["status"] = config["active"]
                task[config["vehicle_field"]] = vehicle.get("车辆编号")
                task["出勤编号"] = order["出勤编号"]

        preempted = self._last_preempted
        self._last_preempted = []
        return order, preempted

    def _find_token(self, token: str) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if row.get("client_token") == token:
                return row
        return None

    def _hard_block(self, vehicle: dict[str, Any]) -> str | None:
        """硬门槛：年检、维修、报废。出车占用不属于硬门槛，走冲突/抢占逻辑。"""
        if vehicle.get("status") == "维修" or str(vehicle.get("车辆状态") or "").startswith("维修"):
            return f"车辆 {vehicle.get('车辆编号')} 维修中，不得派单"
        if str(vehicle.get("车辆状态") or "").startswith("年检") or _inspection_expired(vehicle):
            return f"车辆 {vehicle.get('车辆编号')} 年检未通过或已过期（{vehicle.get('年检日期')}），不得派单"
        if vehicle.get("status") == "报废":
            return f"车辆 {vehicle.get('车辆编号')} 已报废，不得派单"
        return None

    def _resolve_occupation(
        self,
        vehicle_id: int,
        driver: str,
        task_id: int,
        task_type: str,
        is_emergency: bool,
    ) -> None:
        """车辆、司机、任务三维度查占用；应急任务抢占非应急占用单。"""
        for order in _active_orders():
            same_vehicle = order.get("车辆ID") == vehicle_id
            same_driver = str(order.get("实际驾驶员") or order.get("驾驶员")) == driver
            same_task = order.get("任务类型") == task_type and order.get("任务ID") == task_id
            if not (same_vehicle or same_driver or same_task):
                continue
            target = []
            if same_vehicle:
                target.append(f"车辆被出勤单 {order.get('出勤编号')}（{order.get('任务名称')}）占用")
            if same_driver:
                target.append(f"司机 {driver} 已有出勤单 {order.get('出勤编号')}")
            if same_task:
                target.append(f"任务已派出给出勤单 {order.get('出勤编号')}")
            if is_emergency and not order.get("应急任务"):
                self._preempt(order, reason="；".join(target))
                continue
            level = "应急指挥任务" if is_emergency else "当前任务"
            raise DispatchConflict(f"{'；'.join(target)}，{level}不能抢占该占用")

    def _preempt(self, order: dict[str, Any], *, reason: str) -> None:
        """应急任务抢占：释放被占车辆与司机、任务回退、原单置为已取消。"""
        vehicle = store.find(VEHICLE_MODULE, int(order.get("车辆ID", 0)))
        task = store.find(str(order.get("任务模块")), int(order.get("任务ID", 0)))
        config = TASK_MODULES[str(order.get("任务类型"))]

        order["status"] = CANCELED
        order["pending"] = False
        order["abnormal"] = True
        order["已抢占"] = True
        order["回库时间"] = _now()
        order["备注"] = (str(order.get("备注") or "") + f"｜应急指挥任务抢占：{reason}").strip("｜")

        if vehicle is not None:
            vehicle["status"] = "在库"
            vehicle["车辆状态"] = "在库待命"
            vehicle["version"] = int(vehicle.get("version", 0)) + 1
        if task is not None:
            task["status"] = order.get("任务接单前状态") or config["active"]
            if order.get("任务接单前车辆") is not None:
                task[config["vehicle_field"]] = order.get("任务接单前车辆")
            task["出勤编号"] = None
        self._last_preempted.append(order["出勤编号"])

    # ---------- 上工 / 回库 / 取消 ----------
    def start_work(self, entry_id: int) -> dict[str, Any]:
        with store.lock, store.transaction(MODULE):
            order = self._require_order(entry_id)
            if order.get("status") != DISPATCHED:
                raise DispatchRejected(f"出勤单 {order.get('出勤编号')} 当前为「{order.get('status')}」，仅已派车单可上工")
            order["status"] = WORKING
            order["上工时间"] = _now()
        return order

    def return_depot(
        self,
        entry_id: int,
        *,
        end_mileage: float | None = None,
        actual_driver: str | None = None,
        remark: str | None = None,
    ) -> dict[str, Any]:
        with store.lock:
            order = self._require_order(entry_id)
            config = TASK_MODULES[str(order.get("任务类型"))]
            with store.transaction(MODULE, VEHICLE_MODULE, config["module"]):
                if order.get("status") not in ACTIVE_STATUS:
                    raise DispatchRejected(
                        f"出勤单 {order.get('出勤编号')} 当前为「{order.get('status')}」，不能重复回库"
                    )
                vehicle = store.find(VEHICLE_MODULE, int(order.get("车辆ID", 0)))
                start_mileage = _to_number(order.get("出车里程"))
                if end_mileage is None:
                    end_mileage = start_mileage
                end_mileage = float(end_mileage)
                if end_mileage + 1e-6 < start_mileage:
                    raise DispatchRejected(
                        f"回库里程 {end_mileage:g} 小于出车里程 {start_mileage:g}，里程读数异常"
                    )
                delta = round(end_mileage - start_mileage, 2)
                driver = (actual_driver or "").strip() or str(order.get("实际驾驶员") or order.get("驾驶员"))

                # 出勤单：实际驾驶员与历史里程按实际出勤保留
                order["status"] = RETURNED
                order["pending"] = False
                order["实际驾驶员"] = driver
                order["回库里程"] = end_mileage
                order["出勤里程"] = delta
                order["回库时间"] = _now()
                if remark:
                    order["备注"] = (str(order.get("备注") or "") + f"｜{remark}").strip("｜")

                # 车辆台账：累计里程、留用实际驾驶员、释放车辆
                if vehicle is not None:
                    vehicle["当前里程"] = end_mileage
                    vehicle["驾驶员"] = driver
                    vehicle["status"] = "在库"
                    vehicle["车辆状态"] = "在库待命"
                    vehicle["version"] = int(vehicle.get("version", 0)) + 1

                # 任务回写：作业完成
                task = store.find(config["module"], int(order.get("任务ID", 0)))
                if task is not None:
                    task["status"] = config["done"]
                    task["pending"] = False
        return order

    def cancel_dispatch(self, entry_id: int, *, reason: str | None = None) -> dict[str, Any]:
        with store.lock:
            order = self._require_order(entry_id)
            config = TASK_MODULES[str(order.get("任务类型"))]
            with store.transaction(MODULE, VEHICLE_MODULE, config["module"]):
                if order.get("status") not in ACTIVE_STATUS:
                    raise DispatchRejected(f"出勤单 {order.get('出勤编号')} 已结单，不能取消")
                order["status"] = CANCELED
                order["pending"] = False
                order["abnormal"] = True
                order["回库时间"] = _now()
                if reason:
                    order["备注"] = (str(order.get("备注") or "") + f"｜取消原因：{reason}").strip("｜")

                vehicle = store.find(VEHICLE_MODULE, int(order.get("车辆ID", 0)))
                if vehicle is not None:
                    vehicle["status"] = "在库"
                    vehicle["车辆状态"] = "在库待命"
                    vehicle["version"] = int(vehicle.get("version", 0)) + 1
                task = store.find(config["module"], int(order.get("任务ID", 0)))
                if task is not None:
                    task["status"] = order.get("任务接单前状态") or config["active"]
                    if order.get("任务接单前车辆") is not None:
                        task[config["vehicle_field"]] = order.get("任务接单前车辆")
                    task["出勤编号"] = None
        return order

    # ---------- 离线回传（联合幂等） ----------
    def offline_report(
        self,
        *,
        vehicle_id: int,
        task_type: str,
        task_id: int,
        end_mileage: float,
        driver: str | None = None,
        client_token: str | None = None,
        reported_at: str | None = None,
        remark: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """返回 (出勤单, 是否重复回传)。重复回传原样返回，绝不再累计里程。"""
        task_type = (task_type or "").strip().lower()
        config = TASK_MODULES.get(task_type)
        if config is None:
            raise DispatchRejected(f"任务类型「{task_type}」不支持回传")
        idem_key = f"{vehicle_id}:{task_type}:{task_id}"

        with store.lock:
            # 先按幂等键与客户端令牌查重
            existing = self._find_idempotent(idem_key)
            if existing is not None:
                order = store.find(MODULE, int(existing.get("dispatch_id", 0)))
                if order is not None:
                    return order, True
            if client_token:
                order = self._find_token(client_token)
                if order is not None:
                    return order, True

            with store.transaction(MODULE, IDEMPOTENT_MODULE, VEHICLE_MODULE, config["module"]):
                vehicle = store.find(VEHICLE_MODULE, vehicle_id)
                if vehicle is None:
                    raise DispatchRejected(f"车辆 {vehicle_id} 不存在，无法回传")
                task = store.find(config["module"], task_id)

                # 同一车辆+任务若已在线回库，同样视为已回传，杜绝离/在线混报重复累计
                settled = self._find_returned(vehicle_id, task_type, task_id)
                if settled is not None:
                    return settled, True

                end_mileage = float(end_mileage)
                current_mileage = _to_number(vehicle.get("当前里程"))
                if end_mileage + 1e-6 < current_mileage:
                    raise DispatchRejected(
                        f"回传里程 {end_mileage:g} 小于台账里程 {current_mileage:g}，读数异常"
                    )

                active = self._find_active(vehicle_id, task_type, task_id)
                if active is not None:
                    # 已有在途出勤单：以台账当前里程为出车基线，完成回库闭环
                    start_mileage = _to_number(active.get("出车里程"))
                    order = active
                else:
                    # 终端在离线下开的单：以台账里程补登起点，闭环时只补这一次差量
                    start_mileage = current_mileage
                    order = {
                        "id": _next_id(store.rows(MODULE)),
                        "出勤编号": _gen_dispatch_no(),
                        "任务类型": task_type,
                        "任务名称": (
                            f"{config['label']} {task.get(config['code'])}" if task else f"{config['label']}任务 {task_id}"
                        ),
                        "任务模块": config["module"],
                        "任务ID": task_id,
                        "车辆ID": vehicle_id,
                        "车辆编号": vehicle.get("车辆编号"),
                        "车牌号": vehicle.get("车牌号"),
                        "驾驶员": (driver or vehicle.get("驾驶员") or "").strip(),
                        "接单版本": int(vehicle.get("version", 0)),
                        "出车里程": start_mileage,
                        "应急任务": EMERGENCY_DEFAULT[task_type],
                        "已抢占": False,
                        "备注": "",
                        "client_token": client_token,
                        "任务接单前状态": task.get("status") if task else None,
                        "任务接单前车辆": task.get(config["vehicle_field"]) if task else None,
                    }
                    store.rows(MODULE).append(order)

                delta = round(end_mileage - start_mileage, 2)
                actual_driver = (driver or "").strip() or str(order.get("实际驾驶员") or order.get("驾驶员"))
                order.update({
                    "实际驾驶员": actual_driver,
                    "status": RETURNED,
                    "pending": False,
                    "abnormal": False,
                    "回库里程": end_mileage,
                    "出勤里程": delta,
                    "离线回传": True,
                    "回库时间": reported_at or _now(),
                })
                if order.get("上工时间") is None and order.get("派车时间"):
                    order.setdefault("上工时间", order.get("派车时间"))
                order.setdefault("派车时间", reported_at or _now())
                if remark:
                    order["备注"] = (str(order.get("备注") or "") + f"｜离线回传：{remark}").strip("｜")

                # 里程只累计这一次；实际驾驶员随台账保留
                vehicle["当前里程"] = end_mileage
                vehicle["驾驶员"] = actual_driver
                vehicle["status"] = "在库"
                vehicle["车辆状态"] = "在库待命"
                vehicle["version"] = int(vehicle.get("version", 0)) + 1

                if task is not None:
                    task["status"] = config["done"]
                    task["pending"] = False
                    task[config["vehicle_field"]] = vehicle.get("车辆编号")
                    task["出勤编号"] = order["出勤编号"]

                store.rows(IDEMPOTENT_MODULE).append({
                    "id": _next_id(store.rows(IDEMPOTENT_MODULE)),
                    "idem_key": idem_key,
                    "dispatch_id": order["id"],
                    "车辆ID": vehicle_id,
                    "任务类型": task_type,
                    "任务ID": task_id,
                    "回库里程": end_mileage,
                    "出勤里程": delta,
                    "client_token": client_token,
                    "回传时间": _now(),
                })
            return order, False

    # ---------- 内部工具 ----------
    def _require_order(self, entry_id: int) -> dict[str, Any]:
        order = store.find(MODULE, entry_id)
        if order is None:
            raise DispatchRejected(f"出勤单 {entry_id} 不存在或已归档")
        return order

    def _find_idempotent(self, key: str) -> dict[str, Any] | None:
        for row in store.rows(IDEMPOTENT_MODULE):
            if row.get("idem_key") == key:
                return row
        return None

    def _find_active(
        self, vehicle_id: int, task_type: str, task_id: int
    ) -> dict[str, Any] | None:
        for row in _active_orders():
            if (
                row.get("车辆ID") == vehicle_id
                and row.get("任务类型") == task_type
                and row.get("任务ID") == task_id
            ):
                return row
        return None

    def _find_returned(
        self, vehicle_id: int, task_type: str, task_id: int
    ) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if (
                row.get("status") == RETURNED
                and row.get("车辆ID") == vehicle_id
                and row.get("任务类型") == task_type
                and row.get("任务ID") == task_id
            ):
                return row
        return None


dispatch_service = DispatchService()
