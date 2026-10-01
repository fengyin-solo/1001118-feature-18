"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

出勤调度涉及多表联改（车辆台账、任务、出勤单、幂等记录），因此仓库额外提供：
- ``lock``：进程内可重入锁，串行化并发调度；
- ``transaction``：按表做快照，事务内任一步骤抛错都会整体回滚，
  保证接单、占用司机、更新任务要么全成、要么全不成。
"""
from __future__ import annotations

import copy
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._lock = threading.RLock()

    @property
    def lock(self) -> threading.RLock:
        """调度专用互斥锁；配合 transaction 使用，也可用于读改写临界区。"""
        return self._lock

    @contextmanager
    def transaction(self, *tables: str) -> Iterator[None]:
        """对给定表开启事务：异常时按快照整体回滚。

        服务层在进入时已持有可重入锁，事务期间所有读写都在同一把锁内完成。
        """
        with self._lock:
            snapshot = {
                name: copy.deepcopy(self._tables.get(name, [])) for name in tables
            }
            try:
                yield
            except Exception:
                for name, data in snapshot.items():
                    self._tables[name] = data
                raise

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
