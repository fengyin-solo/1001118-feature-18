<template>
  <section class="page" data-module="dispatch">
    <header class="page-head">
      <div>
        <h2>车辆出勤调度</h2>
        <p class="page-desc">
          待命 → 派车 → 上工 → 回库的出勤闭环。回库自动回写车辆台账里程/实际驾驶员、
          除雪防汛任务与巡查排班；维修或年检逾期车辆不得派单，并发调度以版本锁保证只成功一单，
          调度冲突时应急指挥任务优先。
        </p>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in statCards" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <div class="dispatch-panels">
      <form class="dispatch-form" @submit.prevent="submitPlan">
        <h3>新建出勤排班（待命）</h3>
        <label class="form-item">
          <span>关联任务</span>
          <select v-model="form.taskKey">
            <option value="" disabled>请选择待办任务</option>
            <option v-for="task in taskOptions" :key="`${task.task_type}-${task.task_id}`" :value="`${task.task_type}:${task.task_id}`">
              [{{ task.priority }}] {{ task.label }}
            </option>
          </select>
        </label>
        <label class="form-item">
          <span>派用车辆</span>
          <select v-model="form.vehicleId" @change="onVehicleChange">
            <option value="" disabled>请选择车辆</option>
            <option v-for="v in vehicleOptions" :key="v.vehicle_id" :value="String(v.vehicle_id)" :disabled="!v.dispatchable">
              {{ v.label }} · {{ v['驾驶员'] }} · {{ v.status }}{{ v.dispatchable ? '' : `（${v.block_reason}）` }}
            </option>
          </select>
        </label>
        <p v-if="selectedVehicle && !selectedVehicle.dispatchable" class="error-text">
          该车辆不可派单：{{ selectedVehicle.block_reason }}
        </p>
        <label class="form-item">
          <span>计划驾驶员</span>
          <input v-model="form.driver" placeholder="留空则取车辆登记驾驶员" />
        </label>
        <button class="btn primary" type="submit">排入待命</button>
      </form>

      <form class="dispatch-form" @submit.prevent="submitReport">
        <h3>离线回传（车辆+任务幂等）</h3>
        <label class="form-item">
          <span>车辆</span>
          <select v-model="report.vehicleId">
            <option value="" disabled>请选择车辆</option>
            <option v-for="v in vehicleOptions" :key="v.vehicle_id" :value="String(v.vehicle_id)">
              {{ v.label }}
            </option>
          </select>
        </label>
        <label class="form-item">
          <span>任务</span>
          <select v-model="report.taskKey">
            <option value="" disabled>请选择任务</option>
            <option v-for="task in allTaskOptions" :key="`r-${task.task_type}-${task.task_id}`" :value="`${task.task_type}:${task.task_id}`">
              [{{ task.priority }}] {{ task.label }}（{{ task.status }}）
            </option>
          </select>
        </label>
        <label class="form-item">
          <span>回库里程表读数</span>
          <input v-model.number="report.endMileage" type="number" min="0" step="0.1" placeholder="如 58500" />
        </label>
        <label class="form-item">
          <span>实际驾驶员</span>
          <input v-model="report.driver" placeholder="按实际出勤填写" />
        </label>
        <button class="btn" type="submit">回传闭环</button>
      </form>

      <form class="dispatch-form" @submit.prevent="submitBatch">
        <h3>并发调度（应急优先）</h3>
        <p class="form-hint">
          把两台车分别排给防汛与日常任务后一起提交：防汛（应急指挥）自动排在前面，
          被挤掉的单只返回失败、不会半占用。
        </p>
        <div v-for="(row, index) in batch" :key="index" class="batch-row">
          <select v-model="row.taskKey">
            <option value="" disabled>任务</option>
            <option v-for="task in taskOptions" :key="`b-${index}-${task.task_type}-${task.task_id}`" :value="`${task.task_type}:${task.task_id}`">
              {{ task.label }}
            </option>
          </select>
          <select v-model="row.vehicleId">
            <option value="" disabled>车辆</option>
            <option v-for="v in vehicleOptions" :key="`bv-${index}-${v.vehicle_id}`" :value="String(v.vehicle_id)" :disabled="!v.dispatchable">
              {{ v.label }}
            </option>
          </select>
          <input v-model="row.driver" placeholder="驾驶员" />
        </div>
        <button class="btn primary" type="submit">批量派车</button>
      </form>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>单号/车牌/司机</span>
        <input v-model="filters.keyword" placeholder="按出勤单号等检索" />
      </label>
      <label class="filter-item">
        <span>状态</span>
        <select v-model="filters.status">
          <option value="">全部</option>
          <option v-for="s in states" :key="s" :value="s">{{ s }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>调度动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">
            <template v-if="column === '任务优先级'">
              <span :class="['priority-tag', row.priority === 1 ? 'p-emergency' : row.priority === 2 ? 'p-support' : 'p-routine']">
                {{ row[column] }}
              </span>
            </template>
            <template v-else>{{ formatCell(row, column) }}</template>
          </td>
          <td class="row-actions">
            <button
              v-for="action in actionsFor(row)"
              :key="action.name"
              class="link"
              type="button"
              @click="runChainAction(action, row)"
            >
              {{ action.name }}
            </button>
            <span v-if="row.status === '已中断'" class="error-text">已被应急抢占：{{ row['中断原因'] }}</span>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无出勤单，先在上方排入待命排班</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span v-if="message" :class="messageOk ? 'ok-text' : 'error-text'">{{ message }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>
interface Option {
  vehicle_id: number
  label: string
  dispatchable: boolean
  block_reason: string
  status: string
  version: number
  '当前里程': number
  '驾驶员': string
}
interface TaskOption {
  task_type: string
  task_id: number
  label: string
  priority: string
  status: string
}

const ENDPOINT = '/api/dispatch'
const columns = ['出勤单号', '状态', '任务优先级', '任务类型', '任务编号', '车辆编号', '车牌号', '驾驶员', '实际驾驶员', '出车里程', '回库里程', '出勤里程', '回库时间']
const states = ['待命', '已派车', '上工作业', '已回库', '已中断']

const rows = ref<Row[]>([])
const stats = ref<Record<string, number>>({})
const taskOptions = ref<TaskOption[]>([])
const allTaskOptions = ref<TaskOption[]>([])
const vehicleOptions = ref<Option[]>([])
const message = ref('')
const messageOk = ref(true)
const filters = reactive({ keyword: '', status: '' })

const form = reactive({ taskKey: '', vehicleId: '', driver: '' })
const report = reactive({ vehicleId: '', taskKey: '', endMileage: '' as number | '', driver: '' })
const batch = ref([
  { taskKey: '', vehicleId: '', driver: '' },
  { taskKey: '', vehicleId: '', driver: '' },
])

const statCards = computed(() => [
  { label: '待命', value: stats.value['待命'] ?? 0 },
  { label: '已派车', value: stats.value['已派车'] ?? 0 },
  { label: '上工作业', value: stats.value['上工作业'] ?? 0 },
  { label: '在途合计', value: stats.value['在途合计'] ?? 0 },
  { label: '已回库', value: stats.value['已回库'] ?? 0 },
  { label: '应急中断', value: stats.value['已中断'] ?? 0 },
])

const selectedVehicle = computed(() =>
  vehicleOptions.value.find(v => String(v.vehicle_id) === form.vehicleId) ?? null,
)

function splitKey(key: string): { task_type: string; task_id: number } {
  const [task_type, id] = key.split(':')
  return { task_type, task_id: Number(id) }
}

function onVehicleChange() {
  if (!form.driver) {
    form.driver = selectedVehicle.value?.['驾驶员'] ?? ''
  }
}

function formatCell(row: Row, column: string): string {
  const value = row[column]
  if (column === '驾驶员' && !value) return '（待命未定）'
  if (column === '状态' && row['离线回传']) return `${value}·离线`
  return value ?? '—'
}

function actionsFor(row: Row) {
  switch (row.status) {
    case '待命':
      return [{ name: '派车', path: 'dispatch', needMileage: false }]
    case '已派车':
      return [{ name: '上工', path: 'start', needMileage: false }]
    case '上工作业':
      return [{ name: '回库', path: 'return', needMileage: true }]
    default:
      return []
  }
}

async function post(path: string, body: Record<string, unknown>) {
  const response = await request(`${ENDPOINT}${path}`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  return response.json()
}

function notify(msg: string, ok = true) {
  message.value = msg
  messageOk.value = ok
}

async function submitPlan() {
  if (!form.taskKey || !form.vehicleId) {
    notify('请先选择任务和车辆', false)
    return
  }
  const { task_type, task_id } = splitKey(form.taskKey)
  const payload = await post('/plan', {
    values: { vehicle_id: Number(form.vehicleId), task_type, task_id, 计划驾驶员: form.driver },
  })
  notify(payload.message, payload.ok)
  if (payload.ok) {
    form.taskKey = ''
    form.vehicleId = ''
    form.driver = ''
  }
  await Promise.all([reload(), loadOptions()])
}

async function runChainAction(action: { name: string; path: string; needMileage: boolean }, row: Row) {
  const values: Record<string, unknown> = {}
  if (action.path === 'dispatch') {
    const driver = window.prompt('请确认接单驾驶员', row['计划驾驶员'] || '')
    if (driver === null) return
    values['驾驶员'] = driver
    values.version = rowVersion(row)
  }
  if (action.needMileage) {
    const endMileage = window.prompt('请输入回库里程表读数', String(row['出车里程'] ?? ''))
    if (endMileage === null) return
    values['回库里程'] = endMileage
    const actualDriver = window.prompt('实际驾驶员（留空取派车驾驶员）', row['驾驶员'] || '')
    if (actualDriver) values['实际驾驶员'] = actualDriver
  }
  const payload = await post(`/${row.id}/${action.path}`, { values })
  notify(`${payload.ok ? '✓ ' : '✗ '}${payload.message}${payload.code === 'VERSION_CONFLICT' ? '（版本冲突，请刷新重试）' : ''}`, payload.ok)
  await Promise.all([reload(), loadOptions()])
}

function rowVersion(row: Row): number | undefined {
  return vehicleOptions.value.find(v => v.vehicle_id === row.vehicle_id)?.version
}

async function submitReport() {
  if (!report.vehicleId || !report.taskKey || report.endMileage === '') {
    notify('请填齐车辆、任务与回库里程', false)
    return
  }
  const { task_type, task_id } = splitKey(report.taskKey)
  const payload = await post('/report', {
    values: {
      vehicle_id: Number(report.vehicleId),
      task_type,
      task_id,
      回库里程: report.endMileage,
      实际驾驶员: report.driver,
    },
  })
  const suffix = payload.code === 'DUPLICATE_REPORT' ? '（重复报文，未重复累计里程）' : ''
  notify(`${payload.message}${suffix}`, payload.ok)
  if (payload.ok) {
    report.endMileage = ''
    report.driver = ''
  }
  await Promise.all([reload(), loadOptions()])
}

async function submitBatch() {
  const items = batch.value
    .filter(r => r.taskKey && r.vehicleId)
    .map((r) => {
      const { task_type, task_id } = splitKey(r.taskKey)
      return { vehicle_id: Number(r.vehicleId), task_type, task_id, 驾驶员: r.driver }
    })
  if (!items.length) {
    notify('至少完整填写一条批量调度', false)
    return
  }
  const response = await request(`${ENDPOINT}/batch`, {
    method: 'POST',
    body: JSON.stringify({ items }),
  })
  const result = await response.json()
  notify(`成功 ${result.succeeded.length} 单，拒绝 ${result.rejected.length} 单：${result.rejected.map((r: any) => r.message).join('；') || '无'}`, result.rejected.length === 0)
  await Promise.all([reload(), loadOptions()])
}

async function loadOptions() {
  const response = await request(`${ENDPOINT}/options`)
  if (response.ok) {
    const payload = await response.json()
    taskOptions.value = payload.tasks
    allTaskOptions.value = payload.tasks
    vehicleOptions.value = payload.vehicles
  }
}

async function reload() {
  const query = new URLSearchParams()
  if (filters.keyword) query.set('keyword', filters.keyword)
  if (filters.status) query.set('status', filters.status)
  const [listResp, statsResp] = await Promise.all([
    request(`${ENDPOINT}?${query.toString()}`),
    request(`${ENDPOINT}/stats`),
  ])
  if (listResp.ok) {
    const payload = await listResp.json()
    rows.value = payload.items ?? []
  }
  if (statsResp.ok) {
    stats.value = await statsResp.json()
  }
}

onMounted(() => {
  void loadOptions()
  void reload()
})
</script>

<style scoped>
.dispatch-panels {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 16px;
}
.dispatch-form {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 14px;
  border: 1px solid var(--border-color, #e3e7ee);
  border-radius: 8px;
  background: #fff;
}
.dispatch-form h3 {
  margin: 0 0 4px;
  font-size: 15px;
}
.form-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 13px;
}
.form-item select,
.form-item input,
.batch-row select,
.batch-row input {
  padding: 6px 8px;
  border: 1px solid var(--border-color, #d8dde6);
  border-radius: 6px;
}
.form-hint {
  font-size: 12px;
  color: #7a828f;
  margin: 0;
}
.batch-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px;
}
.batch-row input {
  grid-column: 1 / -1;
}
.priority-tag {
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 12px;
}
.p-emergency {
  background: #fdecec;
  color: #c63b3b;
}
.p-support {
  background: #fff4e0;
  color: #b9770e;
}
.p-routine {
  background: #eef3ff;
  color: #3b62c6;
}
.ok-text {
  color: #1f8f55;
}
@media (max-width: 1080px) {
  .dispatch-panels {
    grid-template-columns: 1fr;
  }
}
</style>
