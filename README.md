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
| 养护车辆 | `vehicle` | 养护车辆 | 车辆编号、车辆类型、车牌号 |
| 车辆出勤调度 | `dispatch` | 出勤单 | 出勤编号、任务、车辆、驾驶员、状态链、出车/回库里程、版本锁 |
| 养护材料 | `material` | 养护材料 | 材料编号、材料名称、材料类别 |

## 车辆出勤闭环

`dispatch` 模块把养护车辆从待命到回库串成状态链：`待命 → 已派车 → 作业中 → 已回库`
（异常分支为 `已取消`），并在每次状态流转时同步回写车辆台账、除雪/防汛任务与巡查排班。

- **接口**：`POST /api/dispatch/accept`（派车接单）、`/{id}/start`（上工）、
  `/{id}/return`（回库）、`/{id}/cancel`（取消）、`POST /api/dispatch/offline-report`
  （离线回传），以及 `GET /api/dispatch`、`/ledger`、`/stats`。
- **事务一致**：接单、占用司机、更新任务在 `Store.transaction` 快照事务内完成，
  任一步失败整体回滚；全流程经进程内可重入锁串行化。
- **并发版本锁**：车辆台账带 `version`，接单时做乐观校验，版本过期返回 `409`，
  并发派车只有一单成功。
- **调度优先级**：车辆/司机/任务被占用时，应急指挥任务（防汛默认应急，除雪可标记）
  可抢占非应急出勤单（原单取消、资源释放、任务回退），其余冲突直接拒绝。
- **派车门槛**：维修中、年检中或年检过期、报废的车辆一律不得派单。
- **里程与司机**：只在回库时按「回库读数 − 出车读数」累计一次台账里程，
  出勤单永久保留实际驾驶员与历史里程。
- **离线幂等**：按 `车辆ID + 任务类型 + 任务ID` 联合幂等，重复回传（含在线已回库后
  的滞后补报）原样返回原出勤单，不重复累计里程。

后端规则可一键自检：

```bash
cd backend && python3 scripts/verify_dispatch.py
```

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。
