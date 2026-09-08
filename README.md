# 竞品备件价格中台（spare-parts-monitor）

华为国内外友商（Apple / Samsung / OPPO / vivo / 小米 / Google）各产品备件价格的**抓取 · 存储 · 比价监控**中台。
季度更新，生产抓取**无 LLM**（算力≈0），skill 仅用于站点改版时的校准。

## 架构
```
[校准层] spare-parts-price skill（仅改版时人工/自动触发，产出抓取配方落 KB）
        │  executor.py / google_calibrate.py
        ▼
[生产层] crawler/  —— 本机 Windows 计划任务，每季跑一次，无 LLM
        ├─ run.py         读 skill KB 配方 → 代理感知浏览器 → 落库
        ├─ core.py        代理检测(环境变量>系统代理) + 浏览器启动
        └─ 断点续跑(本季已抓机型跳过)
        ▼
[存储]   spare_parts.db (SQLite)
        brands / countries / models / parts / price_snapshots / exchange_rates
        ▼
[中台]   api/server.py(JSON，零依赖)  +  web/(零构建原生前端：总览/比价矩阵/走势/告警/清单/监控)
        ▼
[监控层] monitor.py —— 扫 run_logs 判成功/失败/行数骤降 → 写 maintenance_queue（待修队列）
        ▼
[自愈 Agent] WorkBuddy automation「备件价中台-自愈维护Agent」(每6h轮询队列)
        └─ 载 spare-parts-price skill + agent-browser 实查故障页 → 改 crawler/executor/KB → 重跑验证 → 关单
```

### 数据表（在上方基础上新增两张）
- `run_logs`：每次 brand×country 抓取一条（status/行数/错误/异常标记）——接口监控判定依据。
- `maintenance_queue`：待修工单（open/resolved）——自愈 Agent 的工作来源。

## 1. 安装依赖
```bat
pip install -r requirements.txt
playwright install chromium
```
（OPPO 走内部 API、vivo/小米/Apple/Google 走浏览器回放，均复用 skill 的抓取配方。）

## 2. 代理（Google 等国必须）
脚本自动读取：**环境变量** `HTTPS_PROXY` > **Windows 系统代理(Internet 选项)**。
```bat
set HTTPS_PROXY=http://127.0.0.1:7890      :: 或开启 Clash「系统代理」即可，无需 set
```

## 3. 跑抓取（本机）
```bat
cd C:\Users\Dong\spare-parts-monitor
python -m crawler.run                 :: 全量(分批/断点续跑)
python -m crawler.run --brand oppo --country my   :: 单品牌单国调试
```
- OPPO：API 自动发现**全部机型**（全量，138 机型 × 各区域）。
- vivo：下拉自动发现全部机型（`discover_vivo_models`）。
- 三星（de/my 级联表单、tr/jp/ae 备件表）、苹果（双下拉）、小米（系列→型号展开）：均已实现**全量机型自动发现**，由 `discover_models` 按 KB 的 `query.mode` 分发到对应发现器。
- Google：单级下拉自动发现（`discover_dropdown_models`），但国内被墙，需本机挂代理/系统代理才能直达 `store.google.com/<国>/repair-cost-estimator`。
- 自动发现出的型号文本会直接驱动 `executor.run_query` 选型号（form_select_cascade 的 model 级、spare_parts_table 的 select_model、xiaomi 的 `get_by_text(model)`、google 下拉、vivo 的 li 匹配），**发现即生效**；若某站改版导致选择器失效，自愈 Agent 会介入校准。

## 4. 启动中台（开箱即跑，无需 npm/node）
后端同时托管前端（:8000，零依赖、零构建）：
```bat
python api/server.py          :: 打开 http://localhost:8000
```
前端 `web/` 为原生 HTML/CSS/JS（含 Canvas 图表），由 `api/server.py` 直接静态托管，
不再依赖 Vite/npm。修改前端后刷新浏览器即可生效。

> 演示数据：本环境无法安装浏览器（沙箱限制），已用 `seed_demo.py` 灌入示例数据——
> OPPO Ace2（6 国）与 vivo X300 Pro（3 国）为**真实校准价**，其余品牌为演示价。
> 在本机装好 chromium 后跑一次 `python -m crawler.run` 即可用真实抓取覆盖。
```bat
python seed_demo.py           :: 灌入/重置为演示数据
python -m crawler.run         :: 真实抓取（覆盖演示数据）
```

## 5. 季度自动跑（Windows 计划任务）
建一个 `run_quarterly.bat`：
```bat
@echo off
cd /d C:\Users\Dong\spare-parts-monitor
python run_quarterly.py >> quarterly.log 2>&1
```
注册（每 3 个月 1 号 03:00）：
```bat
schtasks /create /tn "SparePartsQuarterly" /tr "C:\Users\Dong\spare-parts-monitor\run_quarterly.bat" /sc MONTHLY /mo 3 /d 1 /st 03:00
```

## 数据视图
- **比价矩阵**：某季度×某国家，品牌×机型×备件 透视（默认 CNY 等值，可切原币）。
- **价格走势**：选单备件，跨季度 CNY 曲线（需 ≥2 季度快照）。
- **异动告警**：环比上季，超 ±5% 标红/绿。
- **清单浏览**：按品牌×国家×机型 浏览已抓备件。
- **监控运维**：品牌/国家 健康总览（绿/红）、待修队列、运行日志。

## 6. 监控与自愈（接口异常自动维护）
抓取脚本每次运行都会写 `run_logs`，并做异常检测：
- **0 行且真正尝试过抽取** → 疑似选择器失效/页面改版/反爬（自动 `detect_antibot` 识别 access denied/cloudflare 等）。
- **运行时异常** → 记录错误。
- 命中异常即写入 `maintenance_queue`（同 brand/country 去重，不重复建单）。

巡检（可选，作为兜底）：
```bat
python monitor.py              :: 扫 run_logs 补写漏掉的队列项
python monitor.py --report     :: 仅打印健康摘要
```

**自愈 Agent**（已注册为 WorkBuddy 自动化 `备件价中台-自愈维护Agent`，每 6 小时轮询）：
发现队列有 open 项时，自动**加载 spare-parts-price skill → 用 agent-browser 实查故障页 → 判断 404/选择器失效/反爬/API 参数变更 → 改 `crawler/run.py` 或 `executor.py` 或 KB 定位器 → 重跑 `--brand/--country` 验证 → 关单**。
人工也可查看/操作队列：
```bat
python queue.py list-open                         :: 看待修项
python queue.py resolve <id> "<诊断>" "<修改>"     :: 标记已修复（Agent 修复后自动调用）
```
> 注意：自愈 Agent 需要 WorkBuddy 在运行时才会执行；它只在队列非空时才改代码，队列空时什么都不做。

### 6.1 修复后刷新前端
中台前端为零构建静态页（`web/`），由 `api/server.py` 直接托管、数据经 API 实时读取。自愈 Agent 修复爬虫并重跑入库后，前端刷新即见最新数据，**无需重建**；若 Agent 改动过 `web/` 前端源码，会先 `taskkill` 占用 8000 的进程再重启 `api/server.py` 使托管生效。

### 6.2 季度异动自动推送（企微 / 邮件）
`run_quarterly.py` 在抓取结束后自动调用 `push.py`：计算本季相对上季的备件价环比异动
（同一 `brand/country/model/part_type`，阈值默认 ±15%），通过**企业微信机器人** + **邮件**推送。

配置 `config.push.json`（文件缺失或 `enabled=false` 时只本地预览、不发送）：
```json
{
  "enabled": false,
  "alert_threshold": 0.15,
  "wechat": {"webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..."},
  "email":  {"smtp_host": "smtp.exmail.qq.com", "smtp_port": 465,
             "smtp_user": "alert@your.com", "smtp_pass": "***", "to": ["ops@your.com"]}
}
```
也可用环境变量覆盖（便于计划任务注入凭据，不落盘）：
`SPM_WECHAT_WEBHOOK` / `SPM_SMTP_HOST` / `SPM_SMTP_PORT` / `SPM_SMTP_USER` / `SPM_SMTP_PASS` / `SPM_EMAIL_TO`。
企微消息为 markdown（🔺涨 / 🔻跌），邮件为纯文本。手动运行：`python push.py --quarter 2026Q3 --threshold 0.1`。


## 说明 / 风险
- 抓取可能违反各站 ToS，建议仅内部竞品比价、控制频率。
- 各区域价格 API 可能返回统一基准价（OPPO 已知各区域数值相同，币种待前端本地化确认）。
- Google 在国内需代理/海外节点；Apple/vivo/小米注意限速与反爬。
