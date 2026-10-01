<template>
  <section class="page" data-module="vehicle">
    <header class="page-head">
      <div>
        <h2>养护车辆管理</h2>
        <p class="page-desc">车辆台账围绕待命、已派车、上工作业、回库形成出勤状态链；派车/上工/回库请走「出勤调度」，本页只做送修、送检、归库等台账维护。</p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="exportRows">导出车辆台账</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>车辆编号/车牌</span>
        <input v-model="keyword" placeholder="按编号或车牌检索" />
      </label>
      <label class="filter-item">
        <span>车辆状态</span>
        <select v-model="status">
          <option value="">全部</option>
          <option v-for="s in statuses" :key="s" :value="s">{{ s }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>版本</th>
          <th>台账动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>v{{ row.version ?? 0 }}</td>
          <td class="row-actions">
            <button
              v-for="action in actionsFor(row)"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无车辆台账数据</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条车辆台账记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/vehicle'
const columns = ['车辆编号', '车辆类型', '车牌号', '所属单位', '年检日期', '驾驶员', '当前里程', '车辆状态']
const statuses = ['在库', '已派车', '上工作业', '维修', '年检中', '报废']

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const keyword = ref('')
const status = ref('')

const stats = computed(() => [
  { label: '在库待命', value: rows.value.filter(r => r.status === '在库').length },
  { label: '在途车辆', value: rows.value.filter(r => r.status === '已派车' || r.status === '上工作业').length },
  { label: '维修/年检中', value: rows.value.filter(r => r.status === '维修' || r.status === '年检中').length },
])

function actionsFor(row: Row): string[] {
  switch (row.status) {
    case '在库':
      return ['送修车辆', '送检车辆']
    case '维修':
      return ['维修归库']
    case '年检中':
      return ['年检归库']
    default:
      return []
  }
}

function resetFilters() {
  keyword.value = ''
  status.value = ''
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message || '车辆台账动作未生效')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '车辆台账操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams()
  if (keyword.value) query.set('keyword', keyword.value)
  if (status.value) query.set('status', status.value)
  try {
    const response = await request(`${ENDPOINT}?${query.toString()}`)
    if (!response.ok) {
      throw new Error('车辆台账读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '车辆台账读取失败'
  }
}

onMounted(reload)
</script>
