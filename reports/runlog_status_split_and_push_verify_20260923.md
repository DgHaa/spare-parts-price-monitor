# run_logs 状态语义拆分 + 异动推送链路真实验证（2026-09-23）

> 两项收尾工作：
> ① 把 `run_logs.status` 里混记的 `skipped` 拆成 `resumed` / `unavailable` / `skipped`；
> ② 把「从未真正执行过」的异动推送发送路径跑通并验证。

## 一、结论速览

| 项 | 结果 |
|---|---|
| 状态拆分 | 完成，4 个状态位：`success`/`partial`/`failed` + `resumed`/`unavailable`/`skipped` |
| 历史回填 | 208 行 `skipped` 全部按证据归类：**184 → resumed，24 → unavailable**，0 残留 |
| 覆盖度指标修正 | 「覆盖待补」由虚高的 **10 → 0**（那 10 个是官方不提供，不是我方缺口） |
| 状态回归测试 | `tools/test_run_log_status.py` **13 项全通过**（含 7 种汇总优先级组合） |
| 推送链路 | `tools/test_push_pipeline.py` **全通过**：企微**真发真收**、邮件**真实 TLS + SMTP 对话** |
| 推送链路发现缺陷 | **2 个**（代理隐式劫持、邮件阈值硬编码），均已修复 |
| 界面验证 | `tools/verify_status_ui.py` **7 项全通过**（10 个「官方不提供」格子正确渲染） |
| 全套回归 | 5 个脚本全绿；比价矩阵端到端 PASS |

## 二、为什么状态会混记（问题定性）

原实现把三种**完全不同**的情况都写成 `skipped`：

| 实际情况 | 性质 | 原状态 | 新状态 |
|---|---|---|---|
| 断点续跑：本季机型均已抓，本轮无新增 | 正常，且该区域**有数据** | `skipped` | `resumed` |
| KB 研判「官网不提供备件价 / 需真机代理」 | 对方不公布，**非我方缺口** | `skipped` | `unavailable` |
| 未收录（无 KB 记录） | 配置缺失 | `skipped` | `skipped`（保留） |
| 自动发现 0 机型（选择器失效/改版/反爬） | **真失败**，且会写待修工单 | `skipped` + `anomaly=0` | **`failed` + `anomaly=1`** |

最后一类是最危险的：它会往待修队列写工单，却记成"正常跳过"且 `anomaly_flag=0`——
正是 2026-09 那次「vivo/tr 浏览器并发顶超时 → 自动发现 0 机型 → 本季数据整块缺失，
而 run_log 看起来只是跳过」的成因。

另外「接口正常返回但官方未公布价表」（OPPO `partPriceList` 为空 / 小米 `code=14`）
本质也是**官方无价可抓**，归入 `unavailable` 而非 `skipped`；reason 文案区分机型级与区域级。

## 三、改动清单

### 写入侧（状态产出）
| 文件 | 改动 |
|---|---|
| `crawler/run.py` | ① `blocked/unavailable` → `unavailable`；② 断点续跑（REBORN/API 与浏览器两条路径）→ `resumed`；③ 「自动发现 0 机型」→ `failed` + `anomaly=1`；④ 多品类汇总优先级重写 |
| `crawler/samsung_api.py` | 独立的续跑判定 `skipped` → `resumed` |
| `db.py` | `run_logs.status` 注释补全 6 个枚举值与语义 |

汇总优先级（Apple 这类一个区域含 phone/tablet/watch 多条记录）：

```
failed > partial > success > resumed > unavailable > skipped
```
即「有坏消息先报坏消息；有好消息就报好消息；都没有才报中性的续跑/不可用」。
混记 `resumed`+`unavailable` 时取 `resumed`（说明该区域确有数据在跑，信息量更大）。

### 消费侧（展示与判定）
| 文件 | 改动 |
|---|---|
| `api/server.py` | 新增 `cov_status='unavailable'`（原先落到 `empty`「从未抓到数据」）；KPI 独立统计 |
| `web/app.js` | 状态徽章新增 `resumed`/`unavailable`；`covLabel`/`runLabel` 补全新文案；KPI 新增「官方不提供」卡片；图例新增蓝色格子 |
| `web/styles.css` | 新增 `.cov-na`（信息蓝，刻意不用灰——灰=没抓到，会误导） |
| `monitor.py` | 巡检标记细分 `OK`/`RESUME`/`N/A`/`SKIP`/`FAIL`（原先只有 OK/SKIP/FAIL） |
| `output/gen_improvement_report.py` | 失败集口径改为 `failed/partial`（原先 `failed/skipped/blocked`——`blocked` 是 KB 的 recipe 状态，从不出现在 run_logs） |

## 四、历史回填（带备份）

`tools/backfill_run_log_status.py`（默认只读预览，`--apply` 才写库）。判据顺序即 crawler 的短路顺序：

1. KB 中该 brand×country 的记录**全部** blocked/unavailable → `unavailable`
2. 该 brand×country 在该季度**有价行** → `resumed`
3. 其余 → 保持 `skipped` 不动

```
历史 status='skipped' 共 208 行；拟重新归类 208 行：
  skipped → resumed      184 行
  skipped → unavailable   24 行
  保持 skipped 不动（未收录/判不出来）  0 行
```

`unavailable` 的 24 行正好落在已知的 10 个不可用区域上
（apple/tr、samsung/mx、vivo/de、vivo/jp、xiaomi/{de,tr,my,jp,ae,mx}），与 KB 研判一致。

**刻意不改 `anomaly_flag`**：`monitor.py` 会对「最新一次运行且 failed/partial 且
anomaly_flag=1」的区域补写待修工单，回填历史 anomaly 会凭空造出一批工单。

备份：`backups/spare_parts_before_runlog_status_backfill_20260923_164530.db`

回填后分布：`failed 30 / resumed 186 / success 135 / unavailable 26`

## 五、端到端验证

### 5.1 四类状态实测落库

| 场景 | 命令 | run_log |
|---|---|---|
| 真实产出 | `crawler.run --brand samsung --country cn --force` | `#1629 success` rows=54 |
| 断点续跑 | `crawler.run --brand samsung --country cn` | `#1630 resumed` |
| 官网不提供 | `crawler.run --brand xiaomi --country de` | `#1631 unavailable` |
| 官网不提供 | `crawler.run --brand vivo --country de` | `#1632 unavailable` |
| 多品类汇总 | `crawler.run --brand apple --country cn` | `#1633 resumed`（reason 列出 phone/tablet/watch 三条） |

未收录（`skipped`）与「自动发现 0 机型 → failed」两条路径无法在现网自然触发，
用隔离测试 `tools/test_run_log_status.py` 覆盖（**临时库**，不碰真实数据）：

```
[1] 无 KB 记录 → skipped                                   ✓ anomaly=0
[2] 多品类汇总优先级 ×7 种组合                              ✓ 全对
[3] 「自动发现 0 机型」→ failed + anomaly=1 + page 已关闭    ✓
[4] KB 标 unavailable 的区域 → unavailable, anomaly=0       ✓
结果：全部通过 ✓（13 项）
```

### 5.2 覆盖度指标修正

```
修正前：coverage = {ok: 25, stale: 0, failed: 0, empty: 10}
修正后：coverage = {ok: 25, stale: 0, failed: 0, unavailable: 10, empty: 0}
```

「覆盖待补」= stale+failed+empty，由 **10 → 0**。原先那 10 个「从未抓到数据」是误判——
它们是官方不公布价格的区域，不该算成我方的覆盖缺口（会误导排查方向）。

界面实测（`tools/verify_status_ui.py`，真实 Chromium）：

```
接口：coverage.unavailable = 10
  ✓ KPI 含「官方不提供」        ✓ 图例含「官方不提供」
  ✓ .cov-na 格子数 = 10         ✓ 悬停文案出现「官网不提供」
  ✓ 悬停文案出现「续跑」        ✓ 不再出现含糊的「跳过（断点续跑/无新增）」
结果：全部通过 ✓
```

巡检输出（`monitor.py --report`）：`[RESUME] oppo/cn …` 与 `[N/A] xiaomi/de …`
已能一眼区分，不再统统显示 SKIP。

## 六、推送链路真实验证（本次的重点）

### 6.1 为什么之前从没验证过

仓库没有 `config.push.json` → `load_config()` 恒为 `enabled=False` →
`push_quarterly_alerts()` 永远在「仅本地预览，不发送」那条 return 上返回。
**真正调 `send_wechat` / `send_email` 的代码从未被执行过**。只要那段有笔误，
线上永远不报错——因为根本跑不到。

### 6.2 测试设计（`tools/test_push_pipeline.py`）

- **临时库**：复制真实库结构（含 `UNIQUE(part_id,quarter)`），注入 8 条 2026Q2 环比
  （+25% / −30% / +5% / −3% / +40% / −20% / +12% / −18%），阈值 15% → 应命中 5 条
- **临时配置**：`push.CONFIG_PATH` 指向临时文件（**不落仓库**，避免误提交凭据）
- **企微接收端**：本地真实 HTTP 服务，**真发真收**
- **邮件接收端**：本地真实 **TLS SMTP sink**（openssl 自签证书），走真实
  `smtplib.SMTP_SSL` 握手与 SMTP 对话；仅跳过证书校验（那是标准库职责，非被测代码）

### 6.3 结果

```
[push] 2026Q3 计算到 5 条异动（对比 2026Q2，阈值 ±15%）
[push] 企微推送成功
[push] 邮件推送成功

[2] 企微：loopback HTTP 真发真收（且在环境代理为死地址时仍送达 = 确实直连）
  ✓ 收到请求数=1  ✓ Content-Type=application/json  ✓ msgtype=markdown
  ✓ 正文含标题  ✓ 正文含「共 **5** 条异动」  ✓ 5 条异动全部出现在正文
[3] 邮件：真实 TLS + SMTP 对话
  ✓ 收件人数=2  ✓ MAIL FROM 存在
  ✓ Subject = 「备件价中台 2026Q3 异动（5条）」  ✓ 正文含 5 条异动且机型齐全
[4] 发送失败必须优雅降级（不抛异常）           ✓
[5] 阈值参数化                                 ✓
结果：全部通过 ✓
```

阈值过滤也验证正确：8 条环比里只有 |环比| ≥ 15% 的 5 条进入推送，
并按 |环比| 降序（40 → −30 → 25 → −20 → −18）。

### 6.4 顺带抓到的两个真实缺陷（已修）

**① 企微 webhook 被爬虫代理隐式劫持（P1）**

本机为爬虫访问海外品牌站设置了 `http_proxy=https://127.0.0.1:52304`（见
`crawler/core.get_proxy`），而 `urllib.urlopen` 会**隐式读取**这些环境变量——
于是企微告警被悄悄绕道爬虫出口节点。

*证据*：把 webhook 指向一个未监听端口，回来的是代理的
`HTTP Error 502 Bad Gateway`，而不是「连接被拒」——说明请求确实经过了代理。

风险：爬虫代理重启/限流/被墙时，**告警会静默失败**（只打印一行"企微推送失败"）。
而 `smtplib` 不读环境代理，邮件反而直连——这种不对称本身就是个坑。

*修复*：`send_wechat` 新增 `proxy_mode`（`auto`/`direct`/`proxy`）。默认 `auto`
= **先直连，失败再回退走环境代理并打印说明**，兼容「必须走代理才能出网」的环境。
测试里把环境代理指向死地址后请求**仍送达**，即证明直连生效。

**② 邮件文案把阈值硬编码成 ±15%（P2）**

`format_email` 未接收 `threshold` 参数，标题与正文都写死「±15%」；而微信侧
`format_wechat` 读的是真实参数。改 `--threshold 0.25` 后，**邮件会说谎**。
*修复*：参数化，并加回归断言（`±25%` / `±30%` 正确、不再出现硬编码 `±15%`）。

### 6.5 附带安全项

`config.push.json` 含企微 webhook key 与 SMTP 密码，此前**未被 gitignore**。
已加入阻止入仓（模板见 `config.push.json.example`）。

## 七、复现命令

```bash
# 状态拆分回归（临时库，安全）
python tools/test_run_log_status.py

# 历史状态回填（默认只读预览）
python tools/backfill_run_log_status.py
python tools/backfill_run_log_status.py --apply     # 会先自动备份

# 推送链路真实发送验证（临时库 + 临时配置 + 本地接收端）
python tools/test_push_pipeline.py

# 界面状态呈现验证（需先 python api/server.py）
python tools/verify_status_ui.py --shot out.png

# 其余既有验证
python tools/test_amount_parse.py
python tools/verify_quarterly_run.py --baseline backups/<跑批前备份>.db
python tools/verify_oppo_matrix.py --brand oppo --model "OPPO A6x"
```

## 八、遗留

1. **推送链路仍是「未接通」状态**：本次只证明了**代码路径能正确发送**，
   真正上线还需要用户提供 `config.push.json`（企微 webhook + SMTP 凭据）。
   当前 `enabled=false`，跑批仍只做本地预览。
2. **`partial` 未纳入 `monitor.py` 的 neutral 判定**：`partial` 仍记 `FAIL`（沿用旧行为）。
   若希望它单独成档可再拆。
3. **`run_logs` 无状态约束**：`status` 是自由文本，写错拼写不会被拦。
   若后续还要加状态，建议加 CHECK 约束或集中定义枚举常量。
