"""接口出入参模型：列表分页、动作结果与各模块的明细结构。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PageResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int = 1
    size: int = 20


class ActionResult(BaseModel):
    ok: bool
    message: str
    entry: dict[str, Any] | None = None


class EntryPayload(BaseModel):
    """登记或修改一条业务记录时提交的字段集合。"""

    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None



class RoadSectionEntry(BaseModel):
    """管养路段明细结构。"""

    field_0: str | None = None  # 路段编号
    field_1: str | None = None  # 路段名称
    field_2: str | None = None  # 起止桩号
    field_3: str | None = None  # 道路等级
    field_4: str | None = None  # 车道数
    field_5: str | None = None  # 路面类型
    field_6: str | None = None  # 管养单位
    field_7: str | None = None  # 路段状态

class PatrolEntry(BaseModel):
    """巡查记录明细结构。"""

    field_0: str | None = None  # 巡查编号
    field_1: str | None = None  # 巡查路段
    field_2: str | None = None  # 巡查日期
    field_3: str | None = None  # 巡查人员
    field_4: str | None = None  # 巡查车辆
    field_5: str | None = None  # 发现问题
    field_6: str | None = None  # 处置措施
    field_7: str | None = None  # 巡查状态

class PavementEntry(BaseModel):
    """病害记录明细结构。"""

    field_0: str | None = None  # 病害编号
    field_1: str | None = None  # 所属路段
    field_2: str | None = None  # 病害类型
    field_3: str | None = None  # 严重程度
    field_4: str | None = None  # 起止桩号
    field_5: str | None = None  # 面积
    field_6: str | None = None  # 发现日期
    field_7: str | None = None  # 病害状态

class BridgeEntry(BaseModel):
    """检测记录明细结构。"""

    field_0: str | None = None  # 检测编号
    field_1: str | None = None  # 桥梁名称
    field_2: str | None = None  # 检测类型
    field_3: str | None = None  # 检测日期
    field_4: str | None = None  # 技术状况评分
    field_5: str | None = None  # 主要病害
    field_6: str | None = None  # 检测单位
    field_7: str | None = None  # 检测状态

class BridgeInfoEntry(BaseModel):
    """桥梁明细结构。"""

    field_0: str | None = None  # 桥梁编号
    field_1: str | None = None  # 桥梁名称
    field_2: str | None = None  # 桥型结构
    field_3: str | None = None  # 跨径组合
    field_4: str | None = None  # 设计荷载
    field_5: str | None = None  # 建成年份
    field_6: str | None = None  # 上次评定等级
    field_7: str | None = None  # 桥梁状态

class TunnelEntry(BaseModel):
    """隧道明细结构。"""

    field_0: str | None = None  # 隧道编号
    field_1: str | None = None  # 隧道名称
    field_2: str | None = None  # 隧道长度
    field_3: str | None = None  # 通风方式
    field_4: str | None = None  # 照明方式
    field_5: str | None = None  # 消防设施
    field_6: str | None = None  # 最近定检
    field_7: str | None = None  # 隧道状态

class TrafficFacilityEntry(BaseModel):
    """交安设施明细结构。"""

    field_0: str | None = None  # 设施编号
    field_1: str | None = None  # 设施类型
    field_2: str | None = None  # 所属路段
    field_3: str | None = None  # 桩号位置
    field_4: str | None = None  # 设置日期
    field_5: str | None = None  # 反光等级
    field_6: str | None = None  # 完好程度
    field_7: str | None = None  # 设施状态

class DrainageEntry(BaseModel):
    """排水设施明细结构。"""

    field_0: str | None = None  # 设施编号
    field_1: str | None = None  # 设施类型
    field_2: str | None = None  # 所属路段
    field_3: str | None = None  # 桩号位置
    field_4: str | None = None  # 清理日期
    field_5: str | None = None  # 淤积程度
    field_6: str | None = None  # 管养班组
    field_7: str | None = None  # 设施状态

class GreenEntry(BaseModel):
    """绿化区域明细结构。"""

    field_0: str | None = None  # 区域编号
    field_1: str | None = None  # 区域名称
    field_2: str | None = None  # 植物品种
    field_3: str | None = None  # 面积
    field_4: str | None = None  # 上次修剪
    field_5: str | None = None  # 上次浇水
    field_6: str | None = None  # 管养班组
    field_7: str | None = None  # 管养状态

class LightingEntry(BaseModel):
    """路灯设施明细结构。"""

    field_0: str | None = None  # 灯具编号
    field_1: str | None = None  # 灯具类型
    field_2: str | None = None  # 功率
    field_3: str | None = None  # 所属路段
    field_4: str | None = None  # 安装日期
    field_5: str | None = None  # 杆号
    field_6: str | None = None  # 不亮原因
    field_7: str | None = None  # 设施状态

class WinterEntry(BaseModel):
    """除雪作业明细结构。"""

    field_0: str | None = None  # 作业编号
    field_1: str | None = None  # 作业路段
    field_2: str | None = None  # 作业日期
    field_3: str | None = None  # 融雪剂用量
    field_4: str | None = None  # 作业车辆
    field_5: str | None = None  # 作业班组
    field_6: str | None = None  # 路面状况
    field_7: str | None = None  # 作业状态

class FloodEntry(BaseModel):
    """防汛记录明细结构。"""

    field_0: str | None = None  # 记录编号
    field_1: str | None = None  # 预警级别
    field_2: str | None = None  # 影响路段
    field_3: str | None = None  # 积水深度
    field_4: str | None = None  # 应急措施
    field_5: str | None = None  # 投入人员
    field_6: str | None = None  # 恢复时间
    field_7: str | None = None  # 防汛状态

class SlopeEntry(BaseModel):
    """边坡明细结构。"""

    field_0: str | None = None  # 边坡编号
    field_1: str | None = None  # 所属路段
    field_2: str | None = None  # 边坡类型
    field_3: str | None = None  # 坡高
    field_4: str | None = None  # 防护形式
    field_5: str | None = None  # 稳定性评级
    field_6: str | None = None  # 最近巡检
    field_7: str | None = None  # 边坡状态

class ExpansionEntry(BaseModel):
    """伸缩缝明细结构。"""

    field_0: str | None = None  # 缝编号
    field_1: str | None = None  # 所属桥梁
    field_2: str | None = None  # 缝类型
    field_3: str | None = None  # 设计伸缩量
    field_4: str | None = None  # 当前缝宽
    field_5: str | None = None  # 堵塞情况
    field_6: str | None = None  # 锚固状态
    field_7: str | None = None  # 缝状态

class BearingEntry(BaseModel):
    """桥梁支座明细结构。"""

    field_0: str | None = None  # 支座编号
    field_1: str | None = None  # 所属桥梁
    field_2: str | None = None  # 支座类型
    field_3: str | None = None  # 设计承载力
    field_4: str | None = None  # 位移量
    field_5: str | None = None  # 锈蚀程度
    field_6: str | None = None  # 最近检查
    field_7: str | None = None  # 支座状态

class ProjectEntry(BaseModel):
    """养护工程明细结构。"""

    field_0: str | None = None  # 工程编号
    field_1: str | None = None  # 工程名称
    field_2: str | None = None  # 工程类型
    field_3: str | None = None  # 施工路段
    field_4: str | None = None  # 承建单位
    field_5: str | None = None  # 开工日期
    field_6: str | None = None  # 竣工日期
    field_7: str | None = None  # 工程状态

class VehicleEntry(BaseModel):
    """养护车辆明细结构。"""

    field_0: str | None = None  # 车辆编号
    field_1: str | None = None  # 车辆类型
    field_2: str | None = None  # 车牌号
    field_3: str | None = None  # 所属单位
    field_4: str | None = None  # 年检日期
    field_5: str | None = None  # 驾驶员
    field_6: str | None = None  # 当前里程
    field_7: str | None = None  # 车辆状态

class MaterialEntry(BaseModel):
    """养护材料明细结构。"""

    field_0: str | None = None  # 材料编号
    field_1: str | None = None  # 材料名称
    field_2: str | None = None  # 材料类别
    field_3: str | None = None  # 规格型号
    field_4: str | None = None  # 供应商
    field_5: str | None = None  # 进场日期
    field_6: str | None = None  # 存放地点
    field_7: str | None = None  # 材料状态


class DispatchAccept(BaseModel):
    """派车接单入参：车辆 + 任务 + 乐观版本号。"""

    vehicle_id: int = Field(..., description="车辆台账 ID")
    task_type: str = Field(..., description="任务类型：winter 除雪 / flood 防汛 / patrol 巡查")
    task_id: int = Field(..., description="任务在各自模块中的 ID")
    driver: str | None = Field(default=None, description="指定驾驶员；为空时沿用车辆台账驾驶员")
    emergency: bool | None = Field(default=None, description="是否应急指挥任务；缺省按任务类型/级别推断")
    expected_version: int | None = Field(default=None, description="车辆台账版本号，做乐观锁校验")
    client_token: str | None = Field(default=None, description="前端防重复提交令牌")
    remark: str | None = None


class DispatchTransition(BaseModel):
    """出勤单状态流转入参：上工 / 回库 / 取消。"""

    end_mileage: float | None = Field(default=None, description="回库时的车辆总里程读数（km）")
    actual_driver: str | None = Field(default=None, description="实际出勤驾驶员，缺省沿用接单驾驶员")
    remark: str | None = None


class OfflineReport(BaseModel):
    """离线回传入参：按 车辆+任务 联合幂等。"""

    vehicle_id: int
    task_type: str
    task_id: int
    end_mileage: float = Field(..., description="回库里程读数，里程只按一次累计")
    driver: str | None = None
    client_token: str | None = None
    reported_at: str | None = Field(default=None, description="车载终端实际回库时间")
    remark: str | None = None
