"""出勤闭环端到端校验：覆盖状态链、事务一致性、版本锁、应急抢占与离线幂等。

直接跑：python backend/scripts/verify_dispatch.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.seed import SEED_ROWS  # noqa: E402
from app.services.dispatch import (  # noqa: E402
    DISPATCHED,
    RETURNED,
    WORKING,
    CANCELED,
    DispatchConflict,
    DispatchRejected,
)
from app import store as store_module  # noqa: E402


def reset_store() -> object:
    store_module.store = store_module.Store()
    import app.services.dispatch as dispatch_mod

    dispatch_mod.store = store_module.store
    return dispatch_mod.DispatchService()


svc = reset_store()
passed = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if not condition:
        raise AssertionError(f"{name} 失败：{detail}")
    passed += 1
    print(f"  ✓ {name}")


# 1. 完整闭环：待命 -> 已派车 -> 作业中 -> 已回库
print("1) 完整状态链与回写")
vehicle1 = store_module.store.find("vehicle", 1)
v_before = float(vehicle1["当前里程"])
order, preempted = svc.accept_dispatch(vehicle_id=1, task_type="patrol", task_id=1)
check("接单后状态为已派车", order["status"] == DISPATCHED, order["status"])
v1 = store_module.store.find("vehicle", 1)
check("接单后车辆台账置为出车作业", v1["status"] == "出车作业", v1["status"])
check("接单版本号 +1", v1["version"] == 4, v1["version"])
check("任务回写为巡查中", store_module.store.find("patrol", 1)["status"] == "巡查中")
check("任务回写出勤编号", store_module.store.find("patrol", 1)["出勤编号"] == order["出勤编号"])
check("任务回写派车车辆", store_module.store.find("patrol", 1)["巡查车辆"] == "VEHI-0001")
svc.start_work(order["id"])
check("上工后作业中", svc.get_entry(order["id"])["status"] == WORKING)
finished = svc.return_depot(order["id"], end_mileage=v_before + 60)
check("回库后已回库", finished["status"] == RETURNED)
check("出勤里程 60", finished["出勤里程"] == 60, finished["出勤里程"])
v1 = store_module.store.find("vehicle", 1)
check("台账累计里程 +60", float(v1["当前里程"]) == v_before + 60, v1["当前里程"])
check("台账回到在库", v1["status"] == "在库")
check("巡查任务回写已完成", store_module.store.find("patrol", 1)["status"] == "已完成")

# 2. 维修 / 年检车辆不得派单
print("2) 年检与维修硬门槛")
try:
    svc.accept_dispatch(vehicle_id=3, task_type="patrol", task_id=2)
    raise AssertionError("维修车辆竟然派单成功")
except DispatchRejected as exc:
    check("维修车辆拒单", "维修中" in str(exc), str(exc))
try:
    svc.accept_dispatch(vehicle_id=5, task_type="patrol", task_id=2)
    raise AssertionError("年检车辆竟然派单成功")
except DispatchRejected as exc:
    check("年检车辆拒单", "年检" in str(exc), str(exc))

# 3. 并发版本锁：同版本两单只有一单成功
print("3) 并发调度版本锁")
o1, _ = svc.accept_dispatch(vehicle_id=4, task_type="winter", task_id=1, expected_version=1)
check("第一单派车成功", o1["status"] == DISPATCHED)
try:
    svc.accept_dispatch(vehicle_id=4, task_type="patrol", task_id=2, expected_version=1)
    raise AssertionError("旧版本派单竟然成功")
except DispatchConflict as exc:
    check("旧版本第二单 409", "版本" in str(exc), str(exc))
active = [r for r in store_module.store.rows("dispatch") if r["车辆ID"] == 4 and r["status"] != CANCELED]
check("车辆 4 只有一单生效", len(active) == 1, len(active))

# 4. 普通任务不能抢占占用；应急防汛任务可抢占非应急任务
print("4) 调度冲突与应急优先")
try:
    svc.accept_dispatch(vehicle_id=4, task_type="flood", task_id=2, emergency=False)
    raise AssertionError("普通任务竟然抢占了占用车辆")
except DispatchConflict as exc:
    check("普通任务冲突拒绝", "不能抢占" in str(exc), str(exc))
o_flood, pre = svc.accept_dispatch(vehicle_id=4, task_type="flood", task_id=1)
check("应急防汛单抢占成功", o_flood["应急任务"] is True and o_flood["status"] == DISPATCHED)
check("返回被抢占单号", o1["出勤编号"] in pre, pre)
check("原非应急单已取消", svc.get_entry(o1["id"])["status"] == CANCELED)
check("被抢单标记已抢占", svc.get_entry(o1["id"])["已抢占"] is True)
check("除雪任务回退待作业", store_module.store.find("winter", 1)["status"] == "待作业")
check("被抢任务出勤编号已清空", not store_module.store.find("winter", 1).get("出勤编号"))
v4 = store_module.store.find("vehicle", 4)
check("应急单占用车辆", v4["status"] == "出车作业")
# 应急对应急不可抢占
try:
    svc.accept_dispatch(vehicle_id=4, task_type="flood", task_id=2)
    raise AssertionError("应急任务竟然抢占了应急占用")
except DispatchConflict as exc:
    check("应急任务互不抢占", "不能抢占" in str(exc), str(exc))
# 司机占用同样生效
try:
    svc.accept_dispatch(vehicle_id=6, task_type="patrol", task_id=2, driver="陈志远")
    raise AssertionError("占用司机竟然被第二单复用")
except DispatchConflict as exc:
    check("占用司机冲突拒绝", "司机" in str(exc), str(exc))

# 4b. 取消派车：释放车辆司机、任务回退
print("4b) 取消派车")
o_cancel, _ = svc.accept_dispatch(vehicle_id=6, task_type="flood", task_id=2, driver="周明辉")
canceled = svc.cancel_dispatch(o_cancel["id"], reason="积水点解除，任务撤销")
check("出勤单已取消", canceled["status"] == CANCELED)
check("车辆释放回库", store_module.store.find("vehicle", 6)["status"] == "在库")
check("防汛任务回退响应前", store_module.store.find("flood", 2)["status"] == "响应中")

# 应急单回库后，除雪任务可以再次派车
svc.return_depot(o_flood["id"], end_mileage=31300)

# 5. 事务回滚：回库里程异常时，台账与任务都不应被改动
print("5) 事务一致性（异常整体回滚）")
o5, _ = svc.accept_dispatch(vehicle_id=6, task_type="flood", task_id=2)
v6_before = store_module.store.find("vehicle", 6)["version"]
f2_status_before = store_module.store.find("flood", 2)["status"]
try:
    svc.return_depot(o5["id"], end_mileage=1)  # 小于出车里程
    raise AssertionError("异常里程竟然回库成功")
except DispatchRejected:
    pass
v6 = store_module.store.find("vehicle", 6)
check("回滚后车辆仍出车", v6["status"] == "出车作业", v6["status"])
check("回滚后版本号不变", v6["version"] == v6_before, v6["version"])
check("回滚后任务状态不变", store_module.store.find("flood", 2)["status"] == f2_status_before)
check("出勤单仍已派车", svc.get_entry(o5["id"])["status"] == DISPATCHED)
svc.return_depot(o5["id"], end_mileage=18950)
check("正常回库里程 150", svc.get_entry(o5["id"])["出勤里程"] == 150)

# 6. 离线回传：联合幂等，不重复累计
print("6) 离线回传幂等")
mile_before = float(store_module.store.find("vehicle", 1)["当前里程"])
rep1, dup1 = svc.offline_report(vehicle_id=1, task_type="winter", task_id=1, end_mileage=mile_before + 88)
check("首次离线回传成功", dup1 is False and rep1["离线回传"] is True)
check("首传补计里程 88", float(store_module.store.find("vehicle", 1)["当前里程"]) == mile_before + 88)
rep2, dup2 = svc.offline_report(vehicle_id=1, task_type="winter", task_id=1, end_mileage=mile_before + 500)
check("二次回传判定重复", dup2 is True)
check("重复回传不累计里程", float(store_module.store.find("vehicle", 1)["当前里程"]) == mile_before + 88)
check("幂等返回原出勤单", rep2["id"] == rep1["id"])
check("幂等表只有一条", len(store_module.store.rows("dispatch_idempotent")) == 1)
check("任务回写已完成", store_module.store.find("winter", 1)["status"] == "已完成")

# 6b. 在线回库后再离线补报同一车辆+任务：联合幂等，不能重复累计
print("6b) 在线/离线跨渠道联合幂等")
store_module.store.rows("winter").append({"id": 99, "status": "待作业", "作业编号": "WINT-0099", "作业路段": "幂等专项路段"})
oc, _ = svc.accept_dispatch(vehicle_id=6, task_type="winter", task_id=99)
mile6 = float(store_module.store.find("vehicle", 6)["当前里程"])
svc.return_depot(oc["id"], end_mileage=mile6 + 40)
again, dup_again = svc.offline_report(
    vehicle_id=6, task_type="winter", task_id=99, end_mileage=mile6 + 999
)
check("在线回库后离线补报判重", dup_again is True)
check("补报不重复累计里程", float(store_module.store.find("vehicle", 6)["当前里程"]) == mile6 + 40)

# 7. 历史里程与实际驾驶员按实际出勤保留
print("7) 实际驾驶员保留")
o7, _ = svc.accept_dispatch(vehicle_id=4, task_type="patrol", task_id=2, driver="代班司机小刘")
o7d = svc.return_depot(o7["id"], end_mileage=31360, actual_driver="代班司机小刘")
check("出勤单保留实际驾驶员", o7d["实际驾驶员"] == "代班司机小刘")
check("台账驾驶员保留实际出勤人", store_module.store.find("vehicle", 4)["驾驶员"] == "代班司机小刘")

print(f"\n全部 {passed} 项校验通过 ✅")
