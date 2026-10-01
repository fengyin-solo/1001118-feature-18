# 市政道路桥梁养护管理平台

覆盖道路巡查、桥隧定检、路面病害、交安设施、绿化管养、除雪防汛及养护工程管理的市政道桥全要素养护后台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 路段管理 | `road_section` | 管养路段 | 路段编号、路段名称、起止桩号 |
| 日常巡查 | `patrol` | 巡查记录 | 巡查编号、巡查路段、巡查日期 |
| 路面病害 | `pavement` | 病害记录 | 病害编号、所属路段、病害类型 |
| 桥梁定检 | `bridge` | 检测记录 | 检测编号、桥梁名称、检测类型 |
| 桥梁档案 | `bridge_info` | 桥梁 | 桥梁编号、桥梁名称、桥型结构 |
| 隧道管养 | `tunnel` | 隧道 | 隧道编号、隧道名称、隧道长度 |
| 交安设施 | `traffic_facility` | 交安设施 | 设施编号、设施类型、所属路段 |
| 排水设施 | `drainage` | 排水设施 | 设施编号、设施类型、所属路段 |
| 绿化管养 | `green` | 绿化区域 | 区域编号、区域名称、植物品种 |
| 路灯照明 | `lighting` | 路灯设施 | 灯具编号、灯具类型、功率 |
| 除雪防滑 | `winter` | 除雪作业 | 作业编号、作业路段、作业日期 |
| 防汛应急 | `flood` | 防汛记录 | 记录编号、预警级别、影响路段 |
| 边坡防护 | `slope` | 边坡 | 边坡编号、所属路段、边坡类型 |
| 伸缩缝管理 | `expansion` | 伸缩缝 | 缝编号、所属桥梁、缝类型 |
| 支座维护 | `bearing` | 桥梁支座 | 支座编号、所属桥梁、支座类型 |
| 养护工程 | `project` | 养护工程 | 工程编号、工程名称、工程类型 |
| 养护车辆 | `vehicle` | 养护车辆 | 车辆编号、车辆类型、车牌号、年检日期、当前里程、version |
| 出勤调度 | `dispatch` | 车辆出勤单 | 出勤单号、任务类型、车辆/司机、出车与回库里程 |
| 养护材料 | `material` | 养护材料 | 材料编号、材料名称、材料类别 |

## 车辆出勤闭环

出勤调度（`backend/app/services/dispatch.py`）把车辆从待命到回库串成一条状态链：

```text
待命 ──派车──▶ 已派车 ──上工──▶ 上工作业 ──回库──▶ 已回库
                   应急指挥抢占低级任务 ──▶ 已中断（任务回退待办）
```

- **回写**：回库时一次性把里程表读数/实际驾驶员写回车辆台账，把除雪防滑、
  防汛应急、巡查排班对应任务推进到完成态并记录出勤单号与本次里程；历史里程与
  实际驾驶员按当次出勤保留在出勤单上。
- **派单硬卡**：车辆处于维修、年检中、报废，或年检日期已过期，一律不得派单；
  出勤链上的车辆也不能直接在台账页送修/送检。
- **事务相符**：接单、占用司机、更新任务在 `store.lock` 同一把锁内完成，
  任一步校验失败整体不写入。
- **版本锁**：车辆台账带 `version`，派车可带 `version` 做乐观锁，并发调度时
  只允许一单成功，其余返回 `VERSION_CONFLICT` / `RESOURCE_CONFLICT`。
- **应急优先**：防汛应急为「应急指挥」优先级，可抢占日常/保障级在途单；
  `/api/dispatch/batch` 按优先级排序逐单事务提交。
- **离线幂等**：`/api/dispatch/report` 按「车辆 + 任务」联合幂等，重复回传
  返回 `DUPLICATE_REPORT`，不重复累计里程。

出勤相关接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/dispatch/options` | 可派任务与车辆（含不可派原因、版本号） |
| GET | `/api/dispatch/stats` | 各状态出勤单数量 |
| POST | `/api/dispatch/plan` | 排入待命（不占车辆/司机） |
| POST | `/api/dispatch/{id}/dispatch` | 派车接单（可带 `version`） |
| POST | `/api/dispatch/{id}/start` | 上工 |
| POST | `/api/dispatch/{id}/return` | 回库并回写台账与任务 |
| POST | `/api/dispatch/batch` | 并发批量调度（应急优先） |
| POST | `/api/dispatch/report` | 离线回传（车辆+任务幂等） |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。
