"""生成 AI 探索宣传海报（竖版长图 / 极简白）。
读取 output/shots_poster/*.png 转 base64 嵌入 HTML，输出到 output/poster.html。
"""
import base64
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "output" / "shots_poster"
OUT = ROOT / "output" / "poster.html"


def b64img(name):
    p = SHOTS / f"{name}.png"
    if not p.exists():
        return ""
    data = base64.b64encode(p.read_bytes()).decode()
    return f"data:image/png;base64,{data}"


html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>AI 探索 · 跨品牌备件价格智能监控中台</title>
<style>
  :root{{
    --ink:#0f172a; --muted:#64748b; --line:#e2e8f0; --bg-soft:#f8fafc;
    --primary:#2563eb; --primary-soft:#eff6ff;
  }}
  *{{box-sizing:border-box}}
  body{{margin:0;background:#f1f5f9;font-family:"Segoe UI","PingFang SC","Microsoft YaHei",system-ui,sans-serif;color:var(--ink);line-height:1.6}}
  .poster{{max-width:800px;margin:0 auto;background:#fff;box-shadow:0 12px 48px rgba(15,23,42,.1)}}
  .hero{{padding:78px 48px 60px;text-align:center;background:#fff}}
  .hero .eyebrow{{font-size:13px;color:var(--primary);font-weight:700;letter-spacing:.08em;margin-bottom:14px}}
  .hero h1{{font-size:36px;font-weight:800;margin:0 0 16px;letter-spacing:-.6px;line-height:1.25}}
  .hero .sub{{font-size:17px;color:#475569;max-width:560px;margin:0 auto 28px;line-height:1.7}}
  .tag{{display:inline-block;padding:7px 16px;border:1px solid var(--primary);color:var(--primary);border-radius:999px;font-size:12px;font-weight:600;background:var(--primary-soft)}}
  .section{{padding:48px}}
  .section.alt{{background:var(--bg-soft)}}
  .section-title{{font-size:21px;font-weight:800;margin:0 0 22px;display:flex;align-items:center;gap:10px}}
  .section-title .num{{display:inline-flex;align-items:center;justify-content:center;width:28px;height:28px;background:var(--primary);color:#fff;border-radius:50%;font-size:14px}}
  .pain-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:18px}}
  .pain-card{{background:#fff;border:1px solid var(--line);border-radius:12px;padding:22px}}
  .pain-card h4{{margin:0 0 8px;font-size:15px}}
  .pain-card p{{margin:0;font-size:13px;color:var(--muted);line-height:1.65}}
  .shot-card{{background:#fff;border:1px solid var(--line);border-radius:14px;padding:18px;margin-bottom:24px;box-shadow:0 4px 14px rgba(15,23,42,.05)}}
  .shot-card h3{{margin:0 0 6px;font-size:16px}}
  .shot-card p{{margin:0 0 14px;font-size:13px;color:var(--muted)}}
  .shot-card img{{width:100%;border-radius:10px;border:1px solid var(--line);display:block}}
  .ai-grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:18px}}
  .ai-card{{background:#fff;border:1px solid #dbeafe;border-radius:12px;padding:22px}}
  .ai-card h4{{margin:0 0 8px;color:var(--primary);font-size:15px}}
  .ai-card p{{margin:0;font-size:13px;color:#334155;line-height:1.65}}
  .metrics{{display:flex;justify-content:space-between;text-align:center;background:#fff;border:1px solid var(--line);border-radius:14px;padding:28px 24px}}
  .metric .num{{font-size:30px;font-weight:800;color:var(--primary);line-height:1}}
  .metric .lbl{{font-size:12px;color:var(--muted);margin-top:6px}}
  .footer{{padding:30px 48px;text-align:center;color:#94a3b8;font-size:12px;background:#fff;border-top:1px solid var(--line)}}
  @media (max-width:600px){{
    .pain-grid,.ai-grid{{grid-template-columns:1fr}}
    .metrics{{flex-wrap:wrap;gap:18px}}
    .hero{{padding:54px 24px 40px}}
    .section{{padding:32px 24px}}
    .hero h1{{font-size:28px}}
  }}
</style>
</head>
<body>
<div class="poster">
  <section class="hero">
    <div class="eyebrow">内部提效案例</div>
    <h1>AI 探索 · 把多国官网的备件价，<br>变成可追溯的实时情报</h1>
    <p class="sub">覆盖 5 大品牌 / 7 国官网 / 近 3000 机型，季度自更新——让采购、合规与运营决策有据可依。</p>
    <div class="tag">AI-Driven Spare-Parts Price Intelligence</div>
  </section>

  <section class="section alt">
    <div class="section-title"><span class="num">1</span>业务痛点：为什么需要它？</div>
    <div class="pain-grid">
      <div class="pain-card">
        <h4>🔀 价格散落多官网</h4>
        <p>Apple / Samsung / Oppo / Vivo / Xiaomi 各国官网价格分散，人工逐个打开成本高。</p>
      </div>
      <div class="pain-card">
        <h4>⏱ 更新节奏难跟上</h4>
        <p>新品/调价/季节性活动频繁，人工巡检容易漏掉关键变化。</p>
      </div>
      <div class="pain-card">
        <h4>🔍 数据难溯源</h4>
        <p>内部 Excel 里的数字来自哪里、什么时候抓的，无法一一对应官方来源。</p>
      </div>
    </div>
  </section>

  <section class="section">
    <div class="section-title"><span class="num">2</span>平台能力：一张图讲清</div>

    <div class="shot-card">
      <h3>📊 总览仪表盘</h3>
      <p>全局看覆盖度：5 大品牌 · 7 国官网 · 近 3000 机型 · 本季 2860 机型有价。KPI 与覆盖矩阵一目了然。</p>
      <img src="{b64img('overview')}" alt="总览仪表盘" />
    </div>

    <div class="shot-card">
      <h3>🧮 同型号跨国比价</h3>
      <p>同一基础机型按（规格/颜色/版本）分组，统一折算 CNY，单元格热力高亮各国高低，价差一眼可见。</p>
      <img src="{b64img('matrix')}" alt="比价矩阵" />
    </div>

    <div class="shot-card">
      <h3>🩺 运行监控（风险可控）</h3>
      <p>抓取健康状态、运行日志、待修队列实时可见。当前仅 2026Q3 单季数据，环比告警暂时为空，待下季度补齐后可直接替换为「异动告警」截图。</p>
      <img src="{b64img('monitor')}" alt="运行监控" />
    </div>
  </section>

  <section class="section alt">
    <div class="section-title"><span class="num">3</span>AI 探索：双线提效</div>
    <div class="ai-grid">
      <div class="ai-card">
        <h4>🤖 业务侧：用 AI 做监控提效</h4>
        <p>人工巡检 → Playwright 自动抓取；人工比价 → CNY 统一折算热力矩阵；人工盯盘 → 异常自动发现与待修队列。</p>
      </div>
      <div class="ai-card">
        <h4>🛠 工程侧：用 AI 加速构建</h4>
        <p>本平台由 AI 辅助设计、编码与迭代，从需求到可用系统的周期大幅缩短，持续根据反馈快速演进。</p>
      </div>
    </div>
  </section>

  <section class="section">
    <div class="section-title"><span class="num">4</span>价值与可信度</div>
    <div class="metrics">
      <div class="metric"><div class="num">5</div><div class="lbl">大品牌</div></div>
      <div class="metric"><div class="num">7</div><div class="lbl">国家/地区官网</div></div>
      <div class="metric"><div class="num">~3,000</div><div class="lbl">机型覆盖</div></div>
      <div class="metric"><div class="num">26,000+</div><div class="lbl">备件价条数</div></div>
    </div>
  </section>

  <footer class="footer">
    备件价格中台 · Spare-Parts Monitor · 内部资料
  </footer>
</div>
</body>
</html>
"""

OUT.write_text(html, encoding="utf-8")
print(f"海报已生成: {OUT}")
