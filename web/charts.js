/* charts.js - 零依赖 Canvas 图表（折线图/迷你走势），不依赖任何 CDN。 */
(function (global) {
  function setupCanvas(canvas) {
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const w = Math.max(rect.width, 280), h = Math.max(rect.height, 200);
    canvas.width = w * dpr; canvas.height = h * dpr;
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx, w, h };
  }

  const COLORS = ["#2563eb", "#dc2626", "#16a34a", "#d97706", "#7c3aed", "#0891b2", "#db2777", "#65a30d"];

  /**
   * lineChart(canvas, { labels:[], series:[{name, color?, data:[num]}], yfmt, title })
   */
  function lineChart(canvas, opt) {
    const { ctx, w, h } = setupCanvas(canvas);
    ctx.clearRect(0, 0, w, h);
    const labels = opt.labels || [];
    const series = opt.series || [];
    const padL = 56, padR = 16, padT = 16, padB = 34;
    const plotW = w - padL - padR, plotH = h - padT - padB;
    // y range
    let max = -Infinity, min = Infinity;
    series.forEach(s => s.data.forEach(v => { if (v != null) { max = Math.max(max, v); min = Math.min(min, v); } }));
    if (!isFinite(max)) { max = 1; min = 0; }
    if (min === max) { max = min + 1; }
    const yMin = Math.min(0, min), yMax = max * 1.08;
    const xAt = i => padL + (labels.length <= 1 ? plotW / 2 : (plotW * i / (labels.length - 1)));
    const yAt = v => padT + plotH - ((v - yMin) / (yMax - yMin)) * plotH;
    // grid + y ticks
    ctx.strokeStyle = "#eef2f7"; ctx.fillStyle = "#94a3b8"; ctx.font = "11px sans-serif";
    ctx.lineWidth = 1; ctx.textAlign = "right"; ctx.textBaseline = "middle";
    const ticks = 5;
    for (let i = 0; i <= ticks; i++) {
      const val = yMin + (yMax - yMin) * i / ticks;
      const y = yAt(val);
      ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
      ctx.fillText((opt.yfmt || ((n) => Math.round(n)))(val), padL - 8, y);
    }
    // x labels
    ctx.textAlign = "center"; ctx.textBaseline = "top";
    labels.forEach((lb, i) => { ctx.fillText(lb, xAt(i), h - padB + 8); });
    // series
    series.forEach((s, si) => {
      const color = s.color || COLORS[si % COLORS.length];
      ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.beginPath();
      let started = false;
      s.data.forEach((v, i) => {
        if (v == null) return;
        const x = xAt(i), y = yAt(v);
        if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
      });
      ctx.stroke();
      // points
      ctx.fillStyle = color;
      s.data.forEach((v, i) => {
        if (v == null) return;
        ctx.beginPath(); ctx.arc(xAt(i), yAt(v), 3, 0, Math.PI * 2); ctx.fill();
      });
    });
  }

  global.SPCharts = { lineChart, COLORS };
})(window);
