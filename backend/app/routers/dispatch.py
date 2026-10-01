"""车辆出勤调度接口：待命 → 派车 → 上工 → 回库的完整状态链与回写。

规则落点：
- 派车事务（接单/占用司机/更新任务）原子提交，失败回 ActionResult(ok=False)；
- 并发派车冲突通过车辆 version 版本锁返回 VERSION_CONFLICT，只允许一单成功；
- 批量调度按应急指挥优先级排序，应急任务可抢占低级在途单；
- 离线回传按车辆 + 任务联合幂等，重复报文不重复累计里程。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, DispatchBatchPayload, EntryPayload, PageResult
from app.services.dispatch import DispatchError, dispatch_service

router = APIRouter(prefix="/api/dispatch", tags=["车辆出勤调度"])

LIST_FIELDS = [
    "出勤单号",
    "状态",
    "任务类型",
    "任务编号",
    "车辆编号",
    "车牌号",
    "驾驶员",
    "出车里程",
    "回库里程",
    "出勤里程",
]


@router.get("/options")
def options() -> dict:
    """派车表单选项：可派任务、车辆可派状态与拦截原因。"""
    return {"tasks": dispatch_service.task_options(), "vehicles": dispatch_service.vehicle_options()}


@router.get("/stats")
def stats() -> dict[str, int]:
    """出勤看板：各状态数量与在途合计。"""
    return dispatch_service.stats()


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按单号/车牌/驾驶员/任务检索"),
    status: str | None = Query(default=None, description="待命、已派车、上工作业、已回库、已中断"),
    task_type: str | None = Query(default=None, description="winter / flood / patrol"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = dispatch_service.list_entries(
        keyword=keyword, status=status, task_type=task_type, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    entry = dispatch_service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"出勤单 {entry_id} 不存在或已归档")
    return entry


@router.post("/plan", response_model=ActionResult)
def plan_order(payload: EntryPayload) -> ActionResult:
    """建立待命排班单，此阶段不占用车辆与司机。"""
    try:
        entry, message = dispatch_service.plan_order(payload.values)
    except DispatchError as exc:
        return ActionResult(ok=False, message=exc.message, code=exc.code)
    return ActionResult(ok=True, message=message, entry=entry, code="OK")


@router.post("/{entry_id}/dispatch", response_model=ActionResult)
def dispatch_order(entry_id: int, payload: EntryPayload) -> ActionResult:
    """派车接单：车辆放行、司机占用、任务进作业态在同一事务提交；可带 version 做乐观锁。"""
    try:
        entry, message = dispatch_service.dispatch_order(entry_id, payload.values)
    except DispatchError as exc:
        return ActionResult(ok=False, message=exc.message, code=exc.code)
    return ActionResult(ok=True, message=message, entry=entry, code="OK")


@router.post("/{entry_id}/start", response_model=ActionResult)
def start_work(entry_id: int, payload: EntryPayload | None = None) -> ActionResult:
    """已派车 → 上工作业。"""
    values = payload.values if payload is not None else {}
    try:
        entry, message = dispatch_service.start_work(entry_id, values)
    except DispatchError as exc:
        return ActionResult(ok=False, message=exc.message, code=exc.code)
    return ActionResult(ok=True, message=message, entry=entry, code="OK")


@router.post("/{entry_id}/return", response_model=ActionResult)
def return_garage(entry_id: int, payload: EntryPayload) -> ActionResult:
    """回库：里程回写车辆台账、任务推进完成态；需提供回库里程表读数。"""
    try:
        entry, message = dispatch_service.return_garage(entry_id, payload.values)
    except DispatchError as exc:
        return ActionResult(ok=False, message=exc.message, code=exc.code)
    return ActionResult(ok=True, message=message, entry=entry, code="OK")


@router.post("/batch", response_model=dict)
def dispatch_batch(payload: DispatchBatchPayload) -> dict:
    """并发调度一批派车请求：应急指挥优先、逐单事务提交，返回成功/拒绝明细。"""
    if not payload.items:
        raise HTTPException(status_code=400, detail="批量调度列表不能为空")
    return dispatch_service.dispatch_many(payload.items)


@router.post("/report", response_model=ActionResult)
def report_return(payload: EntryPayload) -> ActionResult:
    """离线回传：按 vehicle_id + task_type + task_id 联合幂等，重复报文不重复累计里程。"""
    try:
        entry, message, duplicate = dispatch_service.report_return(payload.values)
    except DispatchError as exc:
        return ActionResult(ok=False, message=exc.message, code=exc.code)
    code = "DUPLICATE_REPORT" if duplicate else "OK"
    return ActionResult(ok=True, message=message, entry=entry, code=code)
