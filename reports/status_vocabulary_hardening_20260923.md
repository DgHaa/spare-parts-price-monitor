# run_logs.status 词汇表加固（应用层枚举 + 写入口断言 + 巡检兜底）

日期：2026-09-23
范围：`db.py` / `crawler/run.py` / `crawler/samsung_api.py` / `monitor.py` / `api/server.py`
      / `tools/verify_quarterly_run.py` / `tools/test_run_log_status.py` / `tools/backfill_run_log_status.py`
状态：已完成并全量回归通过

---

## 1. 为什么做

`run_logs.status` 是无约束的 `TEXT` 列。SQLite 会照单全收任何字符串——`'succes'`（少个 s）、
`'Success'`（大小写）、`''`、`NULL` 都能写进去且**不报任何错**。

危险不在写入，而在**四个下游消费者对未知值的兜底不一致**：

| 消费者 | 遇到未知值 | 后果 |
|---|---|---|
| `api/server.py` 覆盖率推导 | 落到 `else` → 判为 `empty` | **静默**：虚增「从未抓到」缺口，看起来像我们漏抓 |
| `output/gen_improvement_report.py` | `status IN ('failed','partial')` 匹配不到 | **静默**：拼错的 `failed` 被漏报，故障从报告消失 |
| `monitor.py` 巡检 | `else` → `FAIL` | 会喊出来，相对安全 |
| `web/app.js` 徽章 | 显示原始文本 | 样式错乱，信息未丢 |

即「写入时不报错，读取时不一定报错，指标对不上才发现」——与「人工费 50000」事故同构：
错误静默穿过系统，只在末端以「看起来不正常的数字」露出马脚。

**本次采取的是轻量方案，不做 DB 级 CHECK 迁移**：SQLite 不支持
`ALTER TABLE ... ADD CONSTRAINT`，加 CHECK 必须整表重建（关外键→建新表→拷数据→删旧表
→改名→重建索引→`foreign_key_check`），成本高一个数量级，而收益（防拼写错误）与应用层
断言几乎相同。待状态集合连续 2~3 个季度不再新增后，再考虑一次性迁移。

---

## 2. 做了什么

### 2.1 单一词汇表（db.py）

新增常量与校验（`db.py` 文件头，紧跟 `DB_PATH`）：

```python
STATUS_SUCCESS     = "success"      # 本轮抓取成功
STATUS_PARTIAL     = "partial"      # 部分品类/机型成功
STATUS_FAILED      = "failed"       # 真失败（含"自动发现 0 机型"这类静默丢数据）
STATUS_RESUMED     = "resumed"      # 断点续跑：本季机型均已抓取（正常）
STATUS_UNAVAILABLE = "unavailable"  # 官网不提供备件价 / KB 研判需真机代理（非我方缺口）
STATUS_SKIPPED     = "skipped"      # 未收录：无 KB 记录

VALID_STATUSES  = frozenset({...六值...})
NEUTRAL_STATUSES = frozenset({RESUMED, UNAVAILABLE, SKIPPED})   # 中性态，不建工单
STATUS_PRIORITY  = (FAILED, PARTIAL, SUCCESS, RESUMED, UNAVAILABLE, SKIPPED)
```

配套函数：

- `validate_status(status)` —— 非法即抛 `ValueError`，并给出**拼写纠错提示**
  （大小写差异优先，其次 Levenshtein 距离 ≤3）。例：
  - `'succes'` → `非法 run_logs.status='succes'，是否想写 'success'？`
  - `'Success'` → `非法 run_logs.status='Success'：大小写不匹配，应为 'success'`
- `summarize_status(statuses, default=STATUS_SKIPPED)` —— 多品类区域的汇总逻辑，
  由 `STATUS_PRIORITY` 单源驱动。**保留原有特例语义**：`unavailable` 仅当全部品类都
  不可用时才成立；与 `skipped` 混合时按 `skipped` 上报（不把「我方缺口」说成「对方不提供」）。

### 2.2 写入口断言（fail fast）

`log_run()` 是 `run_logs` **唯一**写入函数，因此一处校验即覆盖全部 18 处调用点：

```python
def log_run(...):
    status = validate_status(status)   # ← 非法值在此抛异常，脏数据进不了库
    ...
```

### 2.3 写入口收敛（字面量 → 常量）

| 文件 | 改动 |
|---|---|
| `crawler/run.py` | 17 处 run_logs 状态字面量改为常量；汇总 if/elif 链（10 行）替换为 `summarize_status()`；`_ever_succeeded` 的 SQL 参数化 |
| `crawler/samsung_api.py` | 4 处字面量改为常量 |
| `monitor.py` | 巡检标记改为 `STATUS_FLAGS` 映射表（由常量做键）；`prev_quarter_rows` SQL 参数化；`FAILING_STATUSES` 常量 |
| `api/server.py` | 覆盖率推导改用常量；`last_success_at` 子查询参数化 |
| `output/gen_improvement_report.py` | 失败集查询改用 `STATUS_FAILED`/`STATUS_PARTIAL` 参数化 ⚠️ 该文件被 `.gitignore` 的 `output/*` 排除，改动仅本地生效 |
| `tools/backfill_run_log_status.py` | 4 处 `'skipped'` 字面量改为常量 |

**刻意未动**的地方（加了注释防误改）：KB 配方的 `rec["status"] in ("blocked","unavailable")`
与 API 统计字典的 `st.get("skipped")`。这两处取值虽与状态常量同名，却是**另一套词汇表**，
改成常量引用会把两个语义错误耦合。

### 2.4 未知状态不再静默（读取侧加固）

- `monitor.py`：未知状态不再被静默归入 `FAIL`，改为独立标记 `UNKNOWN` 并打印
  `[warn] 未知 run_logs.status=... 合法值 [...]`。巡检是最后一道可见性防线。
- `api/server.py`：覆盖率推导的 `else` 分支不再无条件归入 `empty`——新增
  `status_unknown` 标记 + `kpis["invalid_status_scopes"]` 计数 + stderr 告警。
  `cov_status` 仍保持 `empty`（不新增前端分类，避免破坏既有配色/图例）。

### 2.5 巡检白名单断言

`tools/verify_quarterly_run.py` 新增 `check_status_vocabulary()`：

```
SELECT status, COUNT(*) FROM run_logs GROUP BY status
```

实际取值必须 ⊆ `db.VALID_STATUSES`，否则报 **ERROR** 并列出非法值 × 行数，退出码 1。
这是应用层枚举的**可观测兜底**：写入断言挡新数据，巡检挡历史遗留与绕过 `log_run` 的直接 SQL。

---

## 3. 验证证据

### 3.1 词汇表单元测试（`tools/test_run_log_status.py` 新增第 [5] 节）

```
[5] 状态词汇表加固
  ✓ VALID_STATUSES 数量: 6          ✓ VALID_STATUSES 内容 / STATUS_PRIORITY 等集 / NEUTRAL ⊆ VALID
  ✓ validate_status 对六个合法值全部放行
  ✓ validate_status('succes')  抛 ValueError + 纠错提示含 'success'
  ✓ validate_status('Success') 抛 ValueError + 纠错提示含 'success'
  ✓ validate_status('resummed') 抛 ValueError + 纠错提示含 'resumed'
  ✓ validate_status('') / (None) 抛 ValueError
  ✓ log_run 非法状态被拦截（异常信息含『非法 run_logs.status』）
  ✓ 非法写入未落库（行数不变: 10 → 10）
  ✓ 合法状态正常落库
  ✓ summarize_status([]) 回退默认 skipped
  ✓ 全 unavailable → unavailable
  ✓ unavailable+skipped 混合 → skipped（不把缺口说成『对方不提供』）
  ✓ 巡检捕获非法状态 + 判为 ERROR
  ✓ 合法数据巡检通过（无 STATUS_INVALID 误报）
结果：全部通过 ✓
```

### 3.2 巡检真实库（含负向用例）

正向（真实库 `spare_parts.db`）：

```
i [INFO ] STATUS_VOCAB   run_logs.status 白名单通过：resumed=186、success=135、failed=30、unavailable=26
合计：ERROR 0 / WARN 0 / INFO 2      结果：全部通过 ✓
```

负向（临时库注入 `'succes'` 与 `'UNKNOWN_X'`）：

```
✗ [ERROR] STATUS_INVALID  run_logs.status 出现非法值：'succes'×1、'UNKNOWN_X'×1；
                          合法值 ['failed','partial','resumed','skipped','success','unavailable']
                          （非法值会被覆盖率推导静默归入 empty，并让改进报告漏报 failed）
合计：ERROR 1 / WARN 0 / INFO 1      结果：存在 ERROR，需人工介入 ✗      exit code = 1 ✓
```

### 3.3 全量回归（Python 3.12 + Playwright）

| 脚本 | 结果 |
|---|---|
| `tools/test_run_log_status.py` | 全部通过 ✓（含新增第 [5] 节） |
| `tools/test_amount_parse.py` | 全部通过 ✓ |
| `tools/test_push_pipeline.py` | 全部通过 ✓（真实 HTTP/TLS 投递） |
| `tools/verify_quarterly_run.py` | ERROR 0 ✓ |
| `tools/verify_status_ui.py` | 全部通过 ✓（真实 Chromium，6 断言） |
| `monitor.py --report` | 35 个 scope 正常，标记全为 RESUME / N/A，**无 UNKNOWN 告警** ✓ |
| `tools/backfill_run_log_status.py`（只读） | 存量 `skipped` = 0 行 ✓ |
| `output/gen_improvement_report.py` | 正常生成（brands=5 price_rows=41523） ✓ |

### 3.4 后端接口

重启后 `GET /api/overview`：

```
HTTP 200
coverage              = {'ok': 25, 'stale': 0, 'failed': 0, 'unavailable': 10, 'empty': 0}
invalid_status_scopes = 0
last_success_at 覆盖数 = 25 / 35        （SQL 参数化后仍正确）
```

---

## 4. 遗留项

1. **DB 级 CHECK 约束仍未加**（本次刻意不做）。触发条件：状态集合连续 2~3 个季度不再新增，
   且巡检 `STATUS_VOCAB` 持续为 INFO 通过。届时迁移需先停爬虫与 API（独占 DB），
   流程见 `db.py` 文件头注释。
2. `output/gen_improvement_report.py` 被 `.gitignore` 的 `output/*` 排除，其常量化改动
   仅本机生效；若希望纳入版本控制需单独调整忽略规则。
