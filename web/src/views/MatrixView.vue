<script setup>
import { ref, onMounted, watch, computed } from 'vue'
import { api } from '../api.js'

const props = defineProps({ quarter: String })
const rows = ref([])
const countries = ref([])
const country = ref('')
const showOrig = ref(false)
const loading = ref(false)
const error = ref('')

async function load() {
  loading.value = true; error.value = ''
  try {
    const d = await api.matrix(props.quarter)
    rows.value = d.rows || []
    const cs = [...new Set(rows.value.map(r => r.country))]
    countries.value = cs
    if (!country.value && cs.length) country.value = cs[0]
  } catch (e) { error.value = e.message } finally { loading.value = false }
}
onMounted(load)
watch(() => props.quarter, load)

const filtered = computed(() => rows.value.filter(r => r.country === country.value))
const parts = computed(() => [...new Set(filtered.value.map(r => r.part))].filter(Boolean).sort())
const rowKeys = computed(() => {
  const m = new Map()
  for (const r of filtered.value) {
    const k = r.brand + '||' + r.model
    if (!m.has(k)) m.set(k, { brand: r.brand, model: r.model, cells: {} })
    m.get(k).cells[r.part] = r
  }
  return [...m.values()].sort((a, b) => (a.brand + a.model).localeCompare(b.brand + b.model))
})
function cell(r) {
  if (!r) return { text: '—', title: '' }
  const v = showOrig.value ? r.price : r.cny_price
  const title = `${r.price} ${r.currency} → CNY ${r.cny_price}`
  return { text: v == null ? '—' : Number(v).toLocaleString(), title }
}
</script>

<template>
  <div>
    <div class="controls">
      <label>国家/地区：
        <select v-model="country">
          <option v-for="c in countries" :key="c" :value="c">{{ c }}</option>
        </select>
      </label>
      <label><input type="checkbox" v-model="showOrig" /> 显示原币（默认显示 CNY 等值）</label>
      <span v-if="loading">加载中…</span>
    </div>
    <div v-if="error" class="err">{{ error }}</div>
    <table v-else>
      <thead>
        <tr><th>品牌</th><th>机型</th><th v-for="p in parts" :key="p">{{ p }}</th></tr>
      </thead>
      <tbody>
        <tr v-for="(r, i) in rowKeys" :key="i">
          <td>{{ r.brand }}</td>
          <td>{{ r.model }}</td>
          <td v-for="p in parts" :key="p" :title="cell(r.cells[p]).title">{{ cell(r.cells[p]).text }}</td>
        </tr>
      </tbody>
    </table>
    <p v-if="!rowKeys.length && !loading" class="err">该季度/国家暂无数据，请先运行季度抓取。</p>
  </div>
</template>
