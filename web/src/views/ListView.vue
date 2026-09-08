<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api.js'

const rows = ref([])
const loading = ref(false)
const error = ref('')
const brand = ref('')
const country = ref('')
const brands = ref([])
const countries = ref([])

async function load() {
  loading.value = true; error.value = ''
  try {
    const d = await api.list(brand.value, country.value)
    rows.value = d || []
    brands.value = [...new Set(rows.value.map(r => r.brand))].sort()
    countries.value = [...new Set(rows.value.map(r => r.country))].sort()
  } catch (e) { error.value = e.message } finally { loading.value = false }
}
onMounted(load)

function grouped() {
  const m = new Map()
  for (const r of rows.value) {
    const k = r.brand + '||' + r.country
    if (!m.has(k)) m.set(k, { brand: r.brand, country: r.country, models: new Map() })
    const g = m.get(k)
    if (!g.models.has(r.model)) g.models.set(r.model, [])
    g.models.get(r.model).push(r)
  }
  return [...m.values()]
}
</script>

<template>
  <div>
    <div class="controls">
      <label>品牌：<select v-model="brand" @change="load"><option value="">全部</option><option v-for="b in brands" :key="b" :value="b">{{ b }}</option></select></label>
      <label>国家：<select v-model="country" @change="load"><option value="">全部</option><option v-for="c in countries" :key="c" :value="c">{{ c }}</option></select></label>
      <span v-if="loading">加载中…</span>
    </div>
    <div v-if="error" class="err">{{ error }}</div>
    <div v-for="(g, i) in grouped()" :key="i" style="margin-bottom:14px">
      <h3 style="margin:6px 0">{{ g.brand }} · {{ g.country }}</h3>
      <table>
        <thead><tr><th>机型</th><th>备件</th><th>本地价</th><th>币种</th><th>CNY 等值</th><th>季度</th></tr></thead>
        <tbody>
          <tr v-for="(r, j) in [...g.models.values()].flat()" :key="j">
            <td>{{ r.model }}</td><td>{{ r.part }}</td>
            <td>{{ r.price }}</td><td>{{ r.currency }}</td>
            <td>{{ r.cny_price }}</td><td>{{ r.quarter }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <p v-if="!rows.length && !loading" class="err">暂无数据，请先运行季度抓取。</p>
  </div>
</template>
