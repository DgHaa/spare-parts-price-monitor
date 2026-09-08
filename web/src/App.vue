<script setup>
import { ref, onMounted, computed } from 'vue'
import { api } from './api.js'
import MatrixView from './views/MatrixView.vue'
import TrendView from './views/TrendView.vue'
import AlertsView from './views/AlertsView.vue'
import ListView from './views/ListView.vue'
import MonitoringView from './views/MonitoringView.vue'

const tabs = [
  { key: 'matrix', label: '比价矩阵', comp: MatrixView },
  { key: 'trend', label: '价格走势', comp: TrendView },
  { key: 'alerts', label: '异动告警', comp: AlertsView },
  { key: 'list', label: '清单浏览', comp: ListView },
  { key: 'monitor', label: '监控运维', comp: MonitoringView },
]
const current = ref('matrix')
const quarters = ref([])
const quarter = ref('')

onMounted(async () => {
  try {
    quarters.value = await api.quarters()
    quarter.value = quarters.value[quarters.value.length - 1] || ''
  } catch (e) {
    console.error(e)
  }
})

const currentComp = computed(() => tabs.find(t => t.key === current.value).comp)
</script>

<template>
  <div class="app">
    <header>
      <h1>竞品备件价格中台</h1>
      <nav>
        <button v-for="t in tabs" :key="t.key" :class="{active: current===t.key}" @click="current=t.key">
          {{ t.label }}
        </button>
      </nav>
      <label class="q">
        季度：
        <select v-model="quarter">
          <option v-for="q in quarters" :key="q" :value="q">{{ q }}</option>
          <option v-if="!quarters.length" value="">（暂无数据）</option>
        </select>
      </label>
    </header>
    <main>
      <component :is="currentComp" :quarter="quarter" />
    </main>
  </div>
</template>

<style>
* { box-sizing: border-box; }
body { margin: 0; font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; background: #f5f6f8; color: #1f2329; }
.app { max-width: 1200px; margin: 0 auto; padding: 16px; }
header { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; margin-bottom: 16px; }
header h1 { font-size: 20px; margin: 0; }
nav { display: flex; gap: 6px; }
nav button { border: 1px solid #d0d3d9; background: #fff; padding: 6px 12px; border-radius: 6px; cursor: pointer; }
nav button.active { background: #2b6cff; color: #fff; border-color: #2b6cff; }
.q select { padding: 6px 8px; border-radius: 6px; border: 1px solid #d0d3d9; }
main { background: #fff; border-radius: 10px; padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,.06); }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { border: 1px solid #e6e8eb; padding: 6px 8px; text-align: left; }
th { background: #f0f2f5; }
.up { color: #d93026; }   /* 涨红（A股习惯） */
.down { color: #1a8a3b; } /* 跌绿 */
.warn { color: #d93026; font-weight: 600; }
.controls { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 12px; align-items: center; }
.controls select, .controls input { padding: 5px 8px; border: 1px solid #d0d3d9; border-radius: 6px; }
.err { color: #d93026; padding: 12px; }
</style>
