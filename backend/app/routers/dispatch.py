"""车辆出勤闭环接口。

状态链：待命 → 已派车 → 作业中 → 已回库（异常：已取消）。
派车接单走乐观版本锁，冲突返回 409；离线回传按车辆与任务联合幂等。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import (
    ActionResult,
    DispatchAccept,
    DispatchTransition,
    OfflineReport,
    PageResult,
)
from app.services.dispatch import (
    DispatchConflict,
    DispatchRejected,
    dispatch_service,
)

router = APIRouter(prefix="/api/dispatch", tags=["车辆出勤调度"])


@router.get("/stats", response_model=dict)
def stats() -> dict[str, Any]:
    """调度台概览：待命/出车/维修车辆与各阶段出勤单数量。"""
    return dispatch_service.stats()


@router.get("/ledger", response_model=list[dict])
def ledger() -> list[dict[str, Any]]:
    """车辆台账视图：含版本号、当前占用出勤单与不可派单原因。"""
    return dispatch_service.ledger()


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="出勤编号、车牌或驾驶员"),
    status: str | None = Query(default=None, description="已派车、作业中、已回库、已取消"),
    task_type: str | None = Query(default=None, description="winter 除雪 / flood 防汛 / patrol 巡查"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """查询出勤单列表，按时间倒序分页。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = dispatch_service.list_entries(
        keyword=keyword, status=status, task_type=task_type, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict[str, Any]:
    entry = dispatch_service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"出勤单 {entry_id} 不存在或已归档")
    return entry


@router.post("/accept", response_model=ActionResult)
def accept_dispatch(payload: DispatchAccept) -> ActionResult:
    """派车接单：事务内完成占用车辆、占用司机、更新任务；并发版本冲突返回 409。"""
    try:
        entry, preempted = dispatch_service.accept_dispatch(
            vehicle_id=payload.vehicle_id,
            task_type=payload.task_type,
            task_id=payload.task_id,
            driver=payload.driver,
            emergency=payload.emergency,
            expected_version=payload.expected_version,
            client_token=payload.client_token,
            remark=payload.remark,
        )
    except DispatchRejected as exc:
        return ActionResult(ok=False, message=str(exc))
    except DispatchConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    message = f"出勤单 {entry['出勤编号']} 已派车，车辆与司机占用成功，任务已更新"
    if preempted:
        message += f"；应急指挥任务已优先抢占非应急出勤单：{'、'.join(preempted)}"
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/start", response_model=ActionResult)
def start_work(entry_id: int, payload: DispatchTransition | None = None) -> ActionResult:
    """上工：已派车 → 作业中。"""
    try:
        entry = dispatch_service.start_work(entry_id)
    except DispatchRejected as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(ok=True, message=f"出勤单 {entry['出勤编号']} 已上工作业", entry=entry)


@router.post("/{entry_id}/return", response_model=ActionResult)
def return_depot(entry_id: int, payload: DispatchTransition) -> ActionResult:
    """回库：作业中 → 已回库；里程累计进台账，实际驾驶员按本次出勤保留。"""
    try:
        entry = dispatch_service.return_depot(
            entry_id,
            end_mileage=payload.end_mileage,
            actual_driver=payload.actual_driver,
            remark=payload.remark,
        )
    except DispatchRejected as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(
        ok=True,
        message=f"出勤单 {entry['出勤编号']} 已回库，本次出勤里程 {entry['出勤里程']:g} km 已写入台账",
        entry=entry,
    )


@router.post("/{entry_id}/cancel", response_model=ActionResult)
def cancel_dispatch(entry_id: int, payload: DispatchTransition | None = None) -> ActionResult:
    """取消未结单的派车：释放车辆与司机、任务回退到接单前状态。"""
    reason = payload.remark if payload else None
    try:
        entry = dispatch_service.cancel_dispatch(entry_id, reason=reason)
    except DispatchRejected as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(ok=True, message=f"出勤单 {entry['出勤编号']} 已取消并释放车辆司机", entry=entry)


@router.post("/offline-report", response_model=ActionResult)
def offline_report(payload: OfflineReport) -> ActionResult:
    """离线回传：按「车辆 + 任务」联合幂等，重复回传不重复累计里程。"""
    try:
        entry, duplicated = dispatch_service.offline_report(
            vehicle_id=payload.vehicle_id,
            task_type=payload.task_type,
            task_id=payload.task_id,
            end_mileage=payload.end_mileage,
            driver=payload.driver,
            client_token=payload.client_token,
            reported_at=payload.reported_at,
            remark=payload.remark,
        )
    except DispatchRejected as exc:
        return ActionResult(ok=False, message=str(exc))
    if duplicated:
        return ActionResult(
            ok=True,
            message=f"车辆 {entry.get('车辆编号')} 该任务的回传已处理过，幂等返回原出勤单，里程未重复累计",
            entry=entry,
        )
    return ActionResult(
        ok=True,
        message=f"离线回传已接收：出勤单 {entry['出勤编号']}，补计里程 {entry['出勤里程']:g} km",
        entry=entry,
    )
