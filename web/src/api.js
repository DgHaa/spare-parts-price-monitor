const BASE = '/api'

export async function get(path, params = {}) {
  const u = new URL(BASE + path, window.location.origin)
  Object.entries(params).forEach(([k, v]) => {
    if (v !== null && v !== undefined && v !== '') u.searchParams.set(k, v)
  })
  const r = await fetch(u)
  if (!r.ok) throw new Error(await r.text())
  return r.json()
}

export const api = {
  brands: () => get('/brands'),
  quarters: () => get('/quarters'),
  list: (brand, country) => get('/list', { brand, country }),
  matrix: (quarter) => get('/matrix', { quarter }),
  trend: (part_id) => get('/trend', { part_id }),
  alerts: (quarter) => get('/alerts', { quarter }),
  health: () => get('/health'),
  anomalies: () => get('/anomalies'),
  runs: () => get('/runs'),
}
