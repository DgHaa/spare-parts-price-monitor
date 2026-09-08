<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api.js'

const health = ref([])
const anomalies = ref([])
const runs = ref([])
const err = ref('')
const lastRefresh = ref('')

async function load() {
  err.value = ''
  try {
    const [h, a, r] = await Promise.all([api.health(), api.anomalies(), api.runs()])
    health.value = h
    anomalies.value = a
    runs.value = r.slice(0, 50)
    lastRefresh.value = new Date().toLocaleString()
  } catch (e) {
    err.value = String(e)
  }
}

onMounted(load)

function statusClass(s) {
  if (s === 'success') return 'ok'
  if (s === 'skipped') return 'skip'
  return 'fail'
}
</script>

<template>
  <div class="monitor">
    <div class="controls">
      <button @click="load">刷新</button>
      <span class="muted" v-if="lastRefresh">更新于 {{ lastRefresh }}</span>
    </div>
    <div class="err" v-if="err">{{ err }}</div>

    <section v-if="anomalies.length">
      <h3 class="red">待修队列（自愈 Agent 处理中 / 待处理）：{{ anomalies.length }}</h3>
      <table>
        <thead><tr><th>#</th><th>品牌</th><th>国家</th><th>问题</th><th>发现时间</th></tr></thead>
        <tbody>
          <tr v-for="a in anomalies" :key="a.id">
            <td>{{ a.id }}</td><td>{{ a.brand }}</td><td>{{ a.country }}</td>
            <td>{{ a.issue_summary }}</td><td>{{ a.detected_at }}</td>
          </tr>
        </tbody>
      </table>
    </section>
    <section v-else>
      <h3 class="green">无待修项，全部抓取健康</h3>
    </section>

    <h3>品牌 / 国家 健康总览</h3>
    <table>
      <thead>
        <tr><th>品牌</th><th>国家</th><th>状态</th><th>季度</th><th>行数</th><th>异常原因</th><th>待修</th><th>上次运行</th></tr>
      </thead>
      <tbody>
        <tr v-for="h in health" :key="h.brand + h.country">
          <td>{{ h.brand }}</td><td>{{ h.country }}</td>
          <td :class="statusClass(h.status)">{{ h.status }}</td>
          <td>{{ h.quarter }}</td>
          <td>{{ h.rows_written }}</td>
          <td class="warn" v-if="h.anomaly_flag">{{ h.anomaly_reason }}</td>
          <td v-else>—</td>
          <td>{{ h.open_issues ? '有 ' + h.open_issues + ' 项' : '无' }}</td>
          <td>{{ h.finished_at }}</td>
        </tr>
        <tr v-if="!health.length"><td colspan="8" class="muted">暂无运行记录，先跑一次抓取</td></tr>
      </tbody>
    </table>

    <h3>运行日志（最近 50 条）</h3>
    <table>
      <thead><tr><th>品牌</th><th>国家</th><th>状态</th><th>行数</th><th>错误</th><th>完成时间</th></tr></thead>
      <tbody>
        <tr v-for="r in runs" :key="r.id">
          <td>{{ r.brand }}</td><td>{{ r.country }}</td>
          <td :class="statusClass(r.status)">{{ r.status }}</td>
          <td>{{ r.rows_written }}</td>
          <td class="warn" v-if="r.error_text">{{ r.error_text }}</td>
          <td v-else>—</td>
          <td>{{ r.finished_at }}</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.monitor h3 { margin: 16px 0 8px; font-size: 15px; }
.red { color: #d93026; }
.green { color: #1a8a3b; }
.ok { color: #1a8a3b; font-weight: 600; }
.fail { color: #d93026; font-weight: 600; }
.skip { color: #888; }
.muted { color: #888; font-size: 12px; }
.controls { display: flex; gap: 10px; align-items: center; margin-bottom: 8px; }
.controls button { border: 1px solid #d0d3d9; background: #fff; padding: 6px 12px; border-radius: 6px; cursor: pointer; }
</style>
