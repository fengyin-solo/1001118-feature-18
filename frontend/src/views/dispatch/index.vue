<template>
  <section class="page" data-module="dispatch">
    <header class="page-head">
      <div>
        <h2>车辆出勤调度</h2>
        <p class="page-desc">
          养护车辆出勤闭环：待命 → 已派车 → 作业中 → 已回库。派车接单事务化占用车辆与司机，
          并发调度按版本锁只成一单；应急指挥任务冲突时优先，离线回传按车辆与任务联合幂等。
        </p>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in statCards" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <div class="dispatch-grid">
      <!-- 派车接单 -->
      <form class="panel" @submit.prevent="submitAccept">
        <h3 class="panel-title">派车接单</h3>
        <label class="form-item">
          <span>车辆台账</span>
          <select v-model.number="acceptForm.vehicle_id" @change="onVehicleChange">
            <option :value="0" disabled>选择待命车辆</option>
            <option v-for="v in ledger" :key="String(v.id)" :value="v.id" :disabled="!v.可派单">
              {{ v.车辆编号 }}｜{{ v.车牌号 }}｜{{ v.驾驶员 }}｜{{ Number(v.当前里程) }}km
              {{ v.可派单 ? '' : `（${v.不可派单原因}）` }}
            </option>
          </select>
        </label>
        <p v-if="selectedVehicle && !selectedVehicle.可派单" class="error-text">
          {{ selectedVehicle.不可派单原因 }}
        </p>
        <label class="form-item">
          <span>任务类型</span>
          <select v-model="acceptForm.task_type" @change="acceptForm.task_id = 0">
            <option value="winter">除雪防滑</option>
            <option value="flood">防汛应急（应急指挥）</option>
            <option value="patrol">日常巡查</option>
          </select>
        </label>
        <label class="form-item">
          <span>任务单</span>
          <select v-model.number="acceptForm.task_id">
            <option :value="0" disabled>选择待执行任务</option>
            <option v-for="t in availableTasks" :key="String(t.id)" :value="t.id">
              {{ taskCode(t) }}｜{{ taskPlace(t) }}｜{{ t.status }}
            </option>
          </select>
        </label>
        <label class="form-item">
          <span>驾驶员（留空用台账司机）</span>
          <input v-model="acceptForm.driver" placeholder="如：王建国" />
        </label>
        <label class="form-check">
          <input type="checkbox" v-model="acceptForm.emergency" />
          <span>按应急指挥任务派车（冲突时优先抢占非应急出勤）</span>
        </label>
        <p class="hint-text">
          当前车辆台账版本 <strong>{{ selectedVehicle ? selectedVehicle.version : '—' }}</strong>，
          提交时带乐观锁校验，重复点击不会重复成单。
        </p>
        <button class="btn primary" type="submit" :disabled="busy">派车并接单</button>
      </form>

      <!-- 离线回传 -->
      <form class="panel" @submit.prevent="submitOffline">
        <h3 class="panel-title">离线回传补报</h3>
        <p class="hint-text">车载终端断网回库后补报。同一车辆 + 同一任务只生效一次，重复提交不会重复累计里程。</p>
        <label class="form-item">
          <span>车辆</span>
          <select v-model.number="offlineForm.vehicle_id">
            <option :value="0" disabled>选择车辆</option>
            <option v-for="v in ledger" :key="String(v.id)" :value="v.id">
              {{ v.车辆编号 }}｜{{ v.车牌号 }}｜台账 {{ Number(v.当前里程) }}km
            </option>
          </select>
        </label>
        <label class="form-item">
          <span>任务类型</span>
          <select v-model="offlineForm.task_type">
            <option value="winter">除雪防滑</option>
            <option value="flood">防汛应急</option>
            <option value="patrol">日常巡查</option>
          </select>
        </label>
        <label class="form-item">
          <span>任务 ID</span>
          <input v-model.number="offlineForm.task_id" type="number" min="1" placeholder="对应任务模块中的 ID" />
        </label>
        <label class="form-item">
          <span>回库里程读数 (km)</span>
          <input v-model.number="offlineForm.end_mileage" type="number" step="0.1" min="0" placeholder="车辆总里程读数" />
        </label>
        <label class="form-item">
          <span>实际驾驶员（可选）</span>
          <input v-model="offlineForm.driver" placeholder="留空则保留原驾驶员" />
        </label>
        <button class="btn" type="submit" :disabled="busy">提交离线回传</button>
      </form>
    </div>

    <form class="filter-bar" @submit.prevent="reloadOrders">
      <label class="filter-item">
        <span>关键字</span>
        <input v-model="orderFilter.keyword" placeholder="出勤编号 / 车牌 / 司机" />
      </label>
      <label class="filter-item">
        <span>状态</span>
        <select v-model="orderFilter.status">
          <option value="">全部</option>
          <option v-for="s in orderStatuses" :key="s" :value="s">{{ s }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in orderColumns" :key="column">{{ column }}</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in orders" :key="String(row.id)">
          <td>{{ row.出勤编号 }}</td>
          <td>
            <span :class="{ emergency: row.应急任务 }">{{ taskLabel(row.任务类型) }}</span>
            <em v-if="row.应急任务" class="tag tag-danger">应急</em>
            <em v-if="row.离线回传" class="tag">离线</em>
            <em v-if="row.已抢占" class="tag tag-warn">被抢占</em>
            <br /><small>{{ row.任务名称 }}</small>
          </td>
          <td>{{ row.车辆编号 }}<br /><small>{{ row.车牌号 }}</small></td>
          <td>{{ row.实际驾驶员 || row.驾驶员 }}</td>
          <td><span class="status-pill">{{ row.status }}</span></td>
          <td>{{ fmt(row.出车里程) }}</td>
          <td>{{ fmt(row.回库里程) }}</td>
          <td>{{ row.出勤里程 == null ? '—' : fmt(row.出勤里程) }}</td>
          <td>{{ row.回库时间 || row.上工时间 || row.派车时间 }}</td>
          <td class="row-actions">
            <button v-if="row.status === '已派车'" class="link" type="button" @click="startWork(row)">上工</button>
            <button
              v-if="row.status === '已派车' || row.status === '作业中'"
              class="link"
              type="button"
              @click="openReturn(row)"
            >回库</button>
            <button
              v-if="row.status === '已派车' || row.status === '作业中'"
              class="link danger"
              type="button"
              @click="cancelOrder(row)"
            >取消</button>
            <span v-else class="hint-text">—</span>
          </td>
        </tr>
        <tr v-if="!orders.length">
          <td :colspan="orderColumns.length + 1" class="empty-state">暂无符合条件的出勤单</td>
        </tr>
      </tbody>
    </table>

    <h3 class="panel-title" style="margin-top: 24px">车辆台账（版本锁）</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>车辆编号</th><th>车牌号</th><th>类型</th><th>驾驶员</th>
          <th>当前里程</th><th>年检日期</th><th>台账状态</th><th>版本</th><th>当前占用</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="v in ledger" :key="String(v.id)">
          <td>{{ v.车辆编号 }}</td>
          <td>{{ v.车牌号 }}</td>
          <td>{{ v.车辆类型 }}</td>
          <td>{{ v.驾驶员 }}</td>
          <td>{{ fmt(v.当前里程) }}</td>
          <td>{{ v.年检日期 }}</td>
          <td :class="{ 'error-text': !v.可派单 }">{{ v.车辆状态 }}</td>
          <td>v{{ v.version }}</td>
          <td>{{ v.当前出勤编号 ? `${v.当前出勤编号}（${v.当前任务}）` : '—' }}</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ orderTotal }} 条出勤单</span>
      <span v-if="message" :class="messageOk ? 'ok-text' : 'error-text'">{{ message }}</span>
    </footer>

    <div v-if="returnTarget" class="modal-mask" @click.self="returnTarget = null">
      <form class="modal" @submit.prevent="submitReturn">
        <h3>出勤回库 - {{ returnTarget.出勤编号 }}</h3>
        <p class="hint-text">
          出车里程 {{ fmt(returnTarget.出车里程) }} km，回库里程读数只允许大于等于出车里程。
          回库后里程累计进车辆台账，任务同步置为完成。
        </p>
        <label class="form-item">
          <span>回库里程读数 (km)</span>
          <input v-model.number="returnForm.end_mileage" type="number" step="0.1" :min="Number(returnTarget.出车里程)" />
        </label>
        <label class="form-item">
          <span>实际驾驶员（留空沿用接单司机）</span>
          <input v-model="returnForm.actual_driver" :placeholder="String(returnTarget.实际驾驶员 || returnTarget.驾驶员)" />
        </label>
        <label class="form-item">
          <span>备注</span>
          <input v-model="returnForm.remark" placeholder="回库备注（可选）" />
        </label>
        <div class="modal-actions">
          <button class="btn" type="button" @click="returnTarget = null">取消</button>
          <button class="btn primary" type="submit">确认回库</button>
        </div>
      </form>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | boolean | null>

const orderColumns = ['出勤编号', '任务', '车辆', '驾驶员', '状态', '出车里程', '回库里程', '出勤里程', '时间']
const orderStatuses = ['已派车', '作业中', '已回库', '已取消']
const taskNames: Record<string, string> = { winter: '除雪防滑', flood: '防汛应急', patrol: '日常巡查' }
const taskCodeKeys: Record<string, string> = { winter: '作业编号', flood: '记录编号', patrol: '巡查编号' }
const taskPlaceKeys: Record<string, string> = { winter: '作业路段', flood: '影响路段', patrol: '巡查路段' }
const doneStatus: Record<string, string> = { winter: '已完成', flood: '已处置', patrol: '已完成' }

const stats = ref<Record<string, number>>({})
const ledger = ref<Row[]>([])
const tasks = reactive<Record<string, Row[]>>({ winter: [], flood: [], patrol: [] })
const orders = ref<Row[]>([])
const orderTotal = ref(0)
const busy = ref(false)
const message = ref('')
const messageOk = ref(true)

const acceptForm = reactive({
  vehicle_id: 0,
  task_type: 'winter',
  task_id: 0,
  driver: '',
  emergency: false,
})
const offlineForm = reactive({
  vehicle_id: 0,
  task_type: 'winter',
  task_id: 0,
  end_mileage: null as number | null,
  driver: '',
})
const orderFilter = reactive({ keyword: '', status: '' })

const returnTarget = ref<Row | null>(null)
const returnForm = reactive({ end_mileage: null as number | null, actual_driver: '', remark: '' })

const statCards = computed(() => [
  { label: '待命车辆', value: stats.value.待命车辆 ?? 0 },
  { label: '出车车辆', value: stats.value.出车车辆 ?? 0 },
  { label: '维修车辆', value: stats.value.维修车辆 ?? 0 },
  { label: '待派车出勤单', value: stats.value.待派车出勤单 ?? 0 },
  { label: '作业中出勤单', value: stats.value.作业中出勤单 ?? 0 },
  { label: '今日已回库', value: stats.value.今日已回库 ?? 0 },
  { label: '占用司机数', value: stats.value.占用司机数 ?? 0 },
])

const selectedVehicle = computed(() => ledger.value.find((v) => Number(v.id) === Number(acceptForm.vehicle_id)) || null)
const availableTasks = computed(() =>
  (tasks[acceptForm.task_type] || []).filter((t) => t.status !== doneStatus[acceptForm.task_type]),
)

function taskLabel(type: unknown) {
  return taskNames[String(type)] || String(type)
}
function taskCode(t: Row) {
  return String(t[taskCodeKeys[acceptForm.task_type]] ?? `ID ${t.id}`)
}
function taskPlace(t: Row) {
  return String(t[taskPlaceKeys[acceptForm.task_type]] ?? '')
}
function fmt(v: unknown) {
  if (v === null || v === undefined || v === '') return '—'
  const n = Number(v)
  return Number.isFinite(n) ? String(n) : String(v)
}

function flash(text: string, ok = true) {
  message.value = text
  messageOk.value = ok
}

function onVehicleChange() {
  const v = selectedVehicle.value
  if (v && !acceptForm.driver) acceptForm.driver = ''
}

async function fetchJson(url: string, init?: RequestInit) {
  const response = await request(url, init)
  const payload = await response.json().catch(() => ({}))
  return { ok: response.ok, status: response.status, payload }
}

async function loadBase() {
  const [s, l, w, f, p] = await Promise.all([
    fetchJson('/api/dispatch/stats'),
    fetchJson('/api/dispatch/ledger'),
    fetchJson('/api/winter?size=200'),
    fetchJson('/api/flood?size=200'),
    fetchJson('/api/patrol?size=200'),
  ])
  stats.value = (s.payload as Record<string, number>) || {}
  ledger.value = (l.payload as Row[]) || []
  tasks.winter = ((w.payload as { items?: Row[] }).items) || []
  tasks.flood = ((f.payload as { items?: Row[] }).items) || []
  tasks.patrol = ((p.payload as { items?: Row[] }).items) || []
}

async function reloadOrders() {
  const query = new URLSearchParams()
  if (orderFilter.keyword) query.set('keyword', orderFilter.keyword)
  if (orderFilter.status) query.set('status', orderFilter.status)
  query.set('size', '200')
  const { payload } = await fetchJson(`/api/dispatch?${query.toString()}`)
  orders.value = (payload as { items?: Row[] }).items || []
  orderTotal.value = Number((payload as { total?: number }).total ?? orders.value.length)
}

async function refreshAll() {
  await Promise.all([loadBase(), reloadOrders()])
}

async function submitAccept() {
  if (!acceptForm.vehicle_id || !acceptForm.task_id) {
    flash('请选择车辆与待执行任务', false)
    return
  }
  busy.value = true
  try {
    const { ok, status, payload } = await fetchJson('/api/dispatch/accept', {
      method: 'POST',
      body: JSON.stringify({
        vehicle_id: acceptForm.vehicle_id,
        task_type: acceptForm.task_type,
        task_id: acceptForm.task_id,
        driver: acceptForm.driver || null,
        emergency: acceptForm.emergency,
        expected_version: selectedVehicle.value ? Number(selectedVehicle.value.version) : null,
        client_token: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
      }),
    })
    const result = payload as { ok?: boolean; message?: string }
    if (status === 409) {
      flash(`调度冲突：${result.message || '车辆版本已变更，请刷新后重试'}`, false)
    } else if (result.ok === false) {
      flash(result.message || '派车未生效', false)
    } else {
      flash(result.message || '派车成功')
      acceptForm.vehicle_id = 0
      acceptForm.task_id = 0
      acceptForm.driver = ''
      acceptForm.emergency = false
    }
    await refreshAll()
  } finally {
    busy.value = false
  }
}

function startWork(row: Row) {
  void transition(`/api/dispatch/${row.id}/start`, {}, '已上工作业')
}

function openReturn(row: Row) {
  returnTarget.value = row
  returnForm.end_mileage = Number(row.回库里程 ?? row.出车里程)
  returnForm.actual_driver = ''
  returnForm.remark = ''
}

async function submitReturn() {
  if (!returnTarget.value || returnForm.end_mileage === null) {
    flash('请填写回库里程读数', false)
    return
  }
  const id = returnTarget.value.id
  returnTarget.value = null
  await transition(
    `/api/dispatch/${id}/return`,
    {
      end_mileage: returnForm.end_mileage,
      actual_driver: returnForm.actual_driver || null,
      remark: returnForm.remark || null,
    },
    '已回库，里程写入台账',
  )
}

async function cancelOrder(row: Row) {
  if (!window.confirm(`确认取消出勤单 ${row.出勤编号}？车辆与司机将被释放，任务回退到接单前状态。`)) return
  await transition(`/api/dispatch/${row.id}/cancel`, { remark: '调度台取消' }, '已取消并释放资源')
}

async function transition(url: string, body: Record<string, unknown>, okText: string) {
  busy.value = true
  try {
    const { payload } = await fetchJson(url, { method: 'POST', body: JSON.stringify(body) })
    const result = payload as { ok?: boolean; message?: string }
    flash(result.message || okText, result.ok !== false)
    await refreshAll()
  } finally {
    busy.value = false
  }
}

async function submitOffline() {
  if (!offlineForm.vehicle_id || !offlineForm.task_id || offlineForm.end_mileage === null) {
    flash('请完整填写车辆、任务与回库里程', false)
    return
  }
  busy.value = true
  try {
    const { payload } = await fetchJson('/api/dispatch/offline-report', {
      method: 'POST',
      body: JSON.stringify({
        vehicle_id: offlineForm.vehicle_id,
        task_type: offlineForm.task_type,
        task_id: offlineForm.task_id,
        end_mileage: offlineForm.end_mileage,
        driver: offlineForm.driver || null,
        client_token: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
      }),
    })
    const result = payload as { ok?: boolean; message?: string }
    flash(result.message || '离线回传已接收', result.ok !== false)
    offlineForm.task_id = 0
    offlineForm.end_mileage = null
    offlineForm.driver = ''
    await refreshAll()
  } finally {
    busy.value = false
  }
}

onMounted(refreshAll)
</script>

<style scoped>
.dispatch-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 16px;
  margin: 16px 0;
}
.panel {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 16px;
  border: 1px solid var(--border-color, #e3e8ef);
  border-radius: 10px;
  background: #fff;
}
.panel-title {
  margin: 0;
  font-size: 15px;
}
.form-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 13px;
}
.form-item input,
.form-item select,
.modal input,
.modal select {
  padding: 7px 10px;
  border: 1px solid var(--border-color, #d7dde6);
  border-radius: 6px;
  font-size: 13px;
}
.form-check {
  display: flex;
  gap: 8px;
  align-items: center;
  font-size: 13px;
}
.hint-text {
  font-size: 12px;
  color: #8a94a6;
}
.status-pill {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 12px;
  background: #eef2ff;
  color: #4a5bb8;
}
.ok-text {
  color: #1a9e57;
}
.emergency {
  font-weight: 600;
}
.tag {
  margin-left: 6px;
  padding: 1px 6px;
  border-radius: 4px;
  font-style: normal;
  font-size: 12px;
  background: #eef2ff;
  color: #4a5bb8;
}
.tag-danger {
  background: #fdecec;
  color: #d94848;
}
.tag-warn {
  background: #fff4e0;
  color: #c97a12;
}
.link.danger {
  color: #d94848;
}
.modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(20, 28, 42, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 30;
}
.modal {
  width: 420px;
  max-width: calc(100vw - 32px);
  background: #fff;
  border-radius: 10px;
  padding: 20px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
</style>
