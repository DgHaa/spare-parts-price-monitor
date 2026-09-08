<script setup>
import { ref, onMounted, watch, nextTick } from 'vue'
import * as echarts from 'echarts'
import { api } from '../api.js'

const items = ref([])        // 备件清单（来自 /list），用于下拉
const partId = ref('')
const series = ref([])
const loading = ref(false)
const error = ref('')
const chartEl = ref(null)
let chart = null

async function loadItems() {
  try { items.value = await api.list() } catch (e) { error.value = e.message }
}
async function loadTrend() {
  if (!partId.value) return
  loading.value = true; error.value = ''
  try {
    series.value = await api.trend(partId.value)
    render()
  } catch (e) { error.value = e.message } finally { loading.value = false }
}
function render() {
  if (!chartEl.value) return
  if (!chart) chart = echarts.init(chartEl.value)
  const xs = series.value.map(r => r.quarter)
  const ys = series.value.map(r => r.cny_price ?? r.price)
  chart.setOption({
    title: { text: series.value.length ? `${series.value[0].brand} ${series.value[0].model} ${series.value[0].part}` : '价格走势', left: 'center', textStyle: { fontSize: 14 } },
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: xs },
    yAxis: { type: 'value', name: 'CNY 等值' },
    series: [{ type: 'line', data: ys, smooth: true, itemStyle: { color: '#2b6cff' } }],
  })
}
onMounted(async () => { await loadItems(); await nextTick(); if (partId.value) loadTrend() })
watch(partId, loadTrend)
</script>

<template>
  <div>
    <div class="controls">
      <label>选择备件：
        <select v-model="partId">
          <option value="">—</option>
          <option v-for="(r, i) in items" :key="i" :value="r.part_id">
            {{ r.brand }} · {{ r.country }} · {{ r.model }} · {{ r.part }}
          </option>
        </select>
      </label>
      <span v-if="loading">加载中…</span>
    </div>
    <div v-if="error" class="err">{{ error }}</div>
    <div ref="chartEl" style="width:100%;height:380px"></div>
    <p v-if="!series.length && !loading" class="err">请选择备件查看跨季度走势（需至少两个季度快照）。</p>
  </div>
</template>
