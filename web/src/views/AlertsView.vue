<script setup>
import { ref, onMounted, watch } from 'vue'
import { api } from '../api.js'

const props = defineProps({ quarter: String })
const data = ref({ rows: [] })
const loading = ref(false)
const error = ref('')
const THRESHOLD = 0.05

async function load() {
  loading.value = true; error.value = ''
  try { data.value = await api.alerts(props.quarter) }
  catch (e) { error.value = e.message } finally { loading.value = false }
}
onMounted(load)
watch(() => props.quarter, load)

function cls(pct) {
  if (pct == null) return ''
  return pct >= 0 ? 'up' : 'down'
}
function fmt(pct) {
  if (pct == null) return '—'
  return (pct * 100).toFixed(1) + '%'
}
</script>

<template>
  <div>
    <p>环比上一季度（{{ data.prev_quarter || '—' }}) 变动，超 {{ (THRESHOLD*100).toFixed(0) }}% 标红/绿：</p>
    <div v-if="error" class="err">{{ error }}</div>
    <table v-else>
      <thead><tr><th>品牌</th><th>国家</th><th>机型</th><th>备件</th><th>上季价</th><th>本季价</th><th>币种</th><th>环比</th></tr></thead>
      <tbody>
        <tr v-for="(r, i) in data.rows" :key="i">
          <td>{{ r.brand }}</td><td>{{ r.country }}</td><td>{{ r.model }}</td><td>{{ r.part }}</td>
          <td>{{ r.prev_price }}</td><td>{{ r.price }}</td><td>{{ r.currency }}</td>
          <td :class="cls(r.change_pct)" class="warn">{{ fmt(r.change_pct) }}</td>
        </tr>
      </tbody>
    </table>
    <p v-if="!data.rows.length && !loading" class="err">该季度暂无环比数据（需至少两个季度快照）。</p>
  </div>
</template>
