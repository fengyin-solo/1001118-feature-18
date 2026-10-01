"""出勤闭环业务测试：状态链、事务、版本锁、应急抢占与离线幂等。

直接跑：python -m unittest discover -s tests -v（在 backend 目录下）
"""
from __future__ import annotations

import threading
import unittest

from app.services.dispatch import (
    STATE_DISPATCHED,
    STATE_INTERRUPTED,
    STATE_RETURNED,
    STATE_STANDBY,
    STATE_WORKING,
    DispatchError,
    dispatch_service,
)
from app.services.vehicle import (
    STATUS_INSPECTION,
    STATUS_REPAIR,
    STATUS_STANDBY as VEHICLE_STANDBY,
    VehicleService,
)
from app.store import store


def pick(rows, **criteria):
    for row in rows:
        if all(row.get(key) == value for key, value in criteria.items()):
            return row
    raise AssertionError(f"未找到满足 {criteria} 的记录")


class DispatchTestCase(unittest.TestCase):
    def setUp(self) -> None:
        store.reset()
        self.svc = dispatch_service
        self.vehicles = VehicleService()

    def plan(self, vehicle_id: int, task_type: str, task_id: int, **extra):
        return self.svc.plan_order({"vehicle_id": vehicle_id, "task_type": task_type, "task_id": task_id, **extra})[0]

    # ---- 基础闭环 ------------------------------------------------------------

    def test_full_chain_writes_back_vehicle_and_task(self):
        """待命 → 派车 → 上工 → 回库，台账/任务/里程全部回写一次。"""
        order = self.plan(1, "winter", 1, 计划驾驶员="张建国")
        self.assertEqual(order["status"], STATE_STANDBY)

        order, msg = self.svc.dispatch_order(order["id"], {"驾驶员": "张建国"})
        self.assertEqual(order["status"], STATE_DISPATCHED)
        vehicle = store.find("vehicle", 1)
        self.assertEqual(vehicle["status"], "已派车")
        self.assertEqual(vehicle["version"], 1)
        task = store.find("winter", 1)
        self.assertEqual(task["status"], "作业中")
        self.assertEqual(task["派用出勤单"], order["出勤单号"])

        order, _ = self.svc.start_work(order["id"], {})
        self.assertEqual(order["status"], STATE_WORKING)
        self.assertEqual(store.find("vehicle", 1)["status"], "上工作业")

        order, _ = self.svc.return_garage(order["id"], {"回库里程": 58320, "实际驾驶员": "张建国"})
        self.assertEqual(order["status"], STATE_RETURNED)
        self.assertEqual(order["出勤里程"], 120)
        vehicle = store.find("vehicle", 1)
        self.assertEqual(vehicle["当前里程"], 58320)
        self.assertEqual(vehicle["驾驶员"], "张建国")
        self.assertEqual(vehicle["status"], VEHICLE_STANDBY)
        task = store.find("winter", 1)
        self.assertEqual(task["status"], "已完成")
        self.assertEqual(task["本次出勤里程"], 120)
        self.assertEqual(task["回写出勤单"], order["出勤单号"])
        # 历史里程与实际驾驶员按实际出勤保留
        self.assertEqual(order["出车里程"], 58200)
        self.assertEqual(order["实际驾驶员"], "张建国")

    def test_return_garage_rejects_mileage_regression(self):
        order = self.plan(1, "patrol", 1)
        self.svc.dispatch_order(order["id"], {"驾驶员": "张建国"})
        with self.assertRaises(DispatchError) as ctx:
            self.svc.return_garage(order["id"], {"回库里程": 100, "实际驾驶员": "张建国"})
        self.assertEqual(ctx.exception.code, "MILEAGE_INVALID")
        # 事务一致：失败后车辆仍占用、任务仍在作业中
        self.assertEqual(store.find("vehicle", 1)["status"], "已派车")
        self.assertEqual(store.find("patrol", 1)["status"], "巡查中")

    # ---- 年检/维修硬卡 -------------------------------------------------------

    def test_repairing_and_overdue_vehicles_cannot_dispatch(self):
        # id=3 在维修
        order = self.plan(3, "winter", 1)
        with self.assertRaises(DispatchError) as ctx:
            self.svc.dispatch_order(order["id"], {"驾驶员": "王海涛"})
        self.assertEqual(ctx.exception.code, "VEHICLE_UNAVAILABLE")
        self.assertIn("维修", ctx.exception.message)

        # id=4 年检 2026-08-31 已过期（当前日期 2026-10-01）
        order = self.plan(4, "patrol", 1)
        with self.assertRaises(DispatchError) as ctx:
            self.svc.dispatch_order(order["id"], {"驾驶员": "赵立军"})
        self.assertEqual(ctx.exception.code, "VEHICLE_UNAVAILABLE")
        self.assertIn("年检", ctx.exception.message)

        # 年检中的车辆同样拒派（用待办的 winter-1）
        entry, _ = self.vehicles.run_action(1, "送检车辆")
        self.assertEqual(entry["status"], STATUS_INSPECTION)
        order = self.plan(1, "winter", 1)
        with self.assertRaises(DispatchError) as ctx:
            self.svc.dispatch_order(order["id"], {"驾驶员": "张建国"})
        self.assertEqual(ctx.exception.code, "VEHICLE_UNAVAILABLE")
        # 年检归库后恢复可派：同一张待命单继续派车
        self.vehicles.run_action(1, "年检归库")
        order, _ = self.svc.dispatch_order(order["id"], {"驾驶员": "张建国"})
        self.assertEqual(order["status"], STATE_DISPATCHED)

    def test_vehicle_ledger_action_blocked_during_attendance(self):
        order = self.plan(1, "winter", 1)
        self.svc.dispatch_order(order["id"], {"驾驶员": "张建国"})
        entry, message = self.vehicles.run_action(1, "送修车辆")
        self.assertIsNone(entry)
        self.assertIn("正在出勤", message)
        self.assertEqual(store.find("vehicle", 1)["status"], "已派车")

    # ---- 司机/车辆冲突 -------------------------------------------------------

    def test_driver_occupied_rejects_second_order(self):
        first = self.plan(1, "winter", 1)
        self.svc.dispatch_order(first["id"], {"驾驶员": "张建国"})
        # 另一台可派车、同一司机
        second = self.plan(2, "patrol", 1)
        with self.assertRaises(DispatchError) as ctx:
            self.svc.dispatch_order(second["id"], {"驾驶员": "张建国"})
        self.assertEqual(ctx.exception.code, "RESOURCE_CONFLICT")

    def test_task_already_linked_rejects_duplicate_dispatch(self):
        first = self.plan(1, "winter", 1)
        self.svc.dispatch_order(first["id"], {"驾驶员": "张建国"})
        second = self.plan(2, "winter", 1)
        with self.assertRaises(DispatchError) as ctx:
            self.svc.dispatch_order(second["id"], {"驾驶员": "李卫东"})
        self.assertEqual(ctx.exception.code, "TASK_OCCUPIED")

    # ---- 版本锁 --------------------------------------------------------------

    def test_version_lock_only_one_concurrent_order_wins(self):
        # 两个并发线程拿同一版本给同一车派两个不同巡查/除雪任务
        outcomes: list[str] = []

        def fire(task_type: str, task_id: int) -> None:
            try:
                order = self.plan(1, task_type, task_id)
                self.svc.dispatch_order(order["id"], {"驾驶员": "张建国", "version": 0})
                outcomes.append("ok")
            except DispatchError as exc:
                outcomes.append(exc.code)

        t1 = threading.Thread(target=fire, args=("winter", 1))
        t2 = threading.Thread(target=fire, args=("patrol", 1))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(sorted(outcomes), ["RESOURCE_CONFLICT", "ok"])
        active = [r for r in store.rows("dispatch") if r["status"] in (STATE_DISPATCHED, STATE_WORKING)]
        self.assertEqual(len(active), 1)

    def test_stale_version_rejected_without_real_conflict(self):
        order = self.plan(1, "winter", 1)
        # 版本先被台账动作推进
        self.vehicles.run_action(1, "送检车辆")
        self.vehicles.run_action(1, "年检归库")
        with self.assertRaises(DispatchError) as ctx:
            self.svc.dispatch_order(order["id"], {"驾驶员": "张建国", "version": 0})
        self.assertEqual(ctx.exception.code, "VERSION_CONFLICT")
        # 用新版本可成功
        current_version = store.find("vehicle", 1)["version"]
        order, _ = self.svc.dispatch_order(order["id"], {"驾驶员": "张建国", "version": current_version})
        self.assertEqual(order["status"], STATE_DISPATCHED)

    # ---- 应急指挥优先 --------------------------------------------------------

    def test_emergency_flood_preempts_routine_order(self):
        routine = self.plan(1, "patrol", 1)
        self.svc.dispatch_order(routine["id"], {"驾驶员": "张建国"})

        # 防汛应急任务抢占同一车辆/司机
        emergency = self.plan(1, "flood", 1, 计划驾驶员="张建国")
        emergency, msg = self.svc.dispatch_order(emergency["id"], {"驾驶员": "张建国"})
        self.assertIn("应急抢占", msg)
        self.assertEqual(store.find("dispatch", routine["id"])["status"], STATE_INTERRUPTED)
        self.assertTrue(store.find("dispatch", routine["id"])["中断原因"])
        # 被抢巡查任务回退待办、解除车辆关联
        patrol_task = store.find("patrol", 1)
        self.assertEqual(patrol_task["status"], "待巡查")
        self.assertNotIn("派用出勤单", patrol_task)
        # 防汛任务进入响应中
        flood_task = store.find("flood", 1)
        self.assertEqual(flood_task["status"], "响应中")
        self.assertEqual(flood_task["派用出勤单"], emergency["出勤单号"])

    def test_emergency_does_not_preempt_another_emergency(self):
        first = self.plan(1, "flood", 1)
        self.svc.dispatch_order(first["id"], {"驾驶员": "张建国"})
        # 同为应急指挥的第二单想抢同一司机：同级不互抢
        second = self.plan(1, "flood", 2, 计划驾驶员="张建国")
        with self.assertRaises(DispatchError) as ctx:
            self.svc.dispatch_order(second["id"], {"驾驶员": "张建国"})
        self.assertEqual(ctx.exception.code, "RESOURCE_CONFLICT")

    def test_batch_schedule_emergency_sorted_first(self):
        # 两台车但都报同一司机张建国：防汛（应急指挥）排序在前成功，
        # 除雪随后发现司机已被应急单占用而拒绝，只允许一单成功。
        result = self.svc.dispatch_many([
            {"vehicle_id": 2, "task_type": "winter", "task_id": 1, "驾驶员": "张建国"},
            {"vehicle_id": 1, "task_type": "flood", "task_id": 1, "驾驶员": "张建国"},
        ])
        self.assertEqual(len(result["succeeded"]), 1)
        self.assertEqual(len(result["rejected"]), 1)
        self.assertEqual(result["rejected"][0]["code"], "RESOURCE_CONFLICT")
        flood = store.find("flood", 1)
        self.assertEqual(flood["status"], "响应中")
        # 成功的是防汛单
        active = [r for r in store.rows("dispatch") if r["status"] == STATE_DISPATCHED]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["task_type"], "flood")
        # 除雪任务保持待作业，没有被半推进
        self.assertEqual(store.find("winter", 1)["status"], "待作业")

    # ---- 离线回传幂等 --------------------------------------------------------

    def test_offline_report_idempotent_by_vehicle_and_task(self):
        payload = {
            "vehicle_id": 1,
            "task_type": "winter",
            "task_id": 1,
            "回库里程": 58500,
            "实际驾驶员": "张建国",
        }
        order, msg, duplicate = self.svc.report_return(payload)
        self.assertFalse(duplicate)
        self.assertEqual(order["出勤里程"], 300)
        self.assertEqual(store.find("vehicle", 1)["当前里程"], 58500)

        # 完全相同的报文再传两次：里程不再累计
        again, _, duplicate = self.svc.report_return(payload)
        self.assertTrue(duplicate)
        self.assertEqual(again["id"], order["id"])
        again2, _, duplicate = self.svc.report_return({**payload, "回库里程": 99999})
        self.assertTrue(duplicate)
        self.assertEqual(store.find("vehicle", 1)["当前里程"], 58500)
        self.assertEqual(store.find("winter", 1)["本次出勤里程"], 300)

    def test_offline_report_closes_existing_in_flight_order(self):
        order = self.plan(1, "patrol", 1)
        self.svc.dispatch_order(order["id"], {"驾驶员": "张建国"})
        self.svc.start_work(order["id"], {})
        returned, _, duplicate = self.svc.report_return({
            "vehicle_id": 1,
            "task_type": "patrol",
            "task_id": 1,
            "回库里程": 58350,
        })
        self.assertFalse(duplicate)
        self.assertEqual(returned["status"], STATE_RETURNED)
        self.assertTrue(returned["离线回传"])
        self.assertEqual(store.find("patrol", 1)["status"], "已完成")
        self.assertEqual(store.find("vehicle", 1)["当前里程"], 58350)

    def test_offline_report_respects_repair_ban(self):
        with self.assertRaises(DispatchError) as ctx:
            self.svc.report_return({
                "vehicle_id": 3,
                "task_type": "winter",
                "task_id": 1,
                "回库里程": 92000,
                "实际驾驶员": "王海涛",
            })
        self.assertEqual(ctx.exception.code, "VEHICLE_UNAVAILABLE")
        self.assertEqual(store.find("vehicle", 3)["status"], STATUS_REPAIR)

    # ---- 事务相符 ------------------------------------------------------------

    def test_dispatch_is_atomic_when_driver_invalid(self):
        """占用司机/更新任务环节失败时，车辆与任务都不能被半改。"""
        order = self.plan(1, "winter", 1, 计划驾驶员="")
        with self.assertRaises(DispatchError) as ctx:
            self.svc.dispatch_order(order["id"], {"驾驶员": "  "})  # 明确空司机=拒单
        self.assertEqual(ctx.exception.code, "DRIVER_REQUIRED")
        self.assertEqual(store.find("vehicle", 1)["status"], VEHICLE_STANDBY)
        self.assertEqual(store.find("vehicle", 1)["version"], 0)
        self.assertEqual(store.find("winter", 1)["status"], "待作业")
        self.assertEqual(store.find("dispatch", order["id"])["status"], STATE_STANDBY)

    def test_standby_plan_does_not_occupy_resource(self):
        self.plan(1, "winter", 1)
        # 待命单不占车也不占司机：台账动作可用
        entry, _ = self.vehicles.run_action(1, "送修车辆")
        self.assertEqual(entry["status"], STATUS_REPAIR)


if __name__ == "__main__":
    unittest.main()
