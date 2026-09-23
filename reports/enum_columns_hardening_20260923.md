# 其余 6 个枚举列加固（品类 / 档位 / 链接类型 / 工单态 / 配方 / 汇率源）

日期：2026-09-23
前序：`reports/status_vocabulary_hardening_20260923.md`（`run_logs.status`，commit `d6e0971`）
范围：`db.py` / `api/server.py` / `crawler/model_links.py` / `crawler/samsung_api.py`
      / `tools/verify_quarterly_run.py` / `tools/test_enum_vocabularies.py`（新）
      / `tools/smoke_crawl_enum_guard.py`（新）
状态：已完成并全量回归通过（含真实抓取动态验证）

---

## 1. 为什么做（与上一轮同构）

`run_logs.status` 加固后，我按同一套判据把 `schema.sql` 里其余枚举语义列扫了一遍，
筛出 6 个（实为 8 个物理列，因 `rate_source` 出现两次）：

| 表.列 | 真实取值（生产库） | 写错的后果 |
|---|---|---|
| `models.category` | phone=4307, watch=750, tablet=647, earbuds=184, wearable=56, other=15 | **跨品类比价无意义**（平板屏幕 ≠ 手机屏幕）；比价矩阵/价格走势按品类分组，写错即静默污染结论 |
| `models.tier` | 中端=2720, 高端=1522, 入门=1284, 旗舰=433 | `api_tiers()` 直接 `SELECT DISTINCT tier` 喂下拉框 → 拼错值变成**下拉框里的一个新档位** |
| `models.model_url_kind` | model_api=3132, **NULL=1445**, model_page=1040, category_api_locator=186, brand_entry=135, model_text_fragment=21 | 取证链接类型失真 → 「这条价是机型页取的还是入口页兜的」分不清，正是上一轮 SKILL 记录的错配风险的元数据 |
| `price_snapshots.source_url_kind` | model_api=26354, model_page=10526, brand_entry=3581, category_api_locator=1062 | 同上，且**与左列是两套词汇表**（见 §3.1） |
| `price_snapshots.rate_source` | static=30958, `live:open.er-api.com`=10565 | 换算 CNH 上限时选错汇率源 |
| `exchange_rates.rate_source` | `live:open.er-api.com`=166, **NULL=33** | 同上 |
| `maintenance_queue.status` | resolved=29 | 工单漏跟进：`open_issues()` 查不到，问题从看板消失 |
| `brands.recipe_mode` | 5 种各 1 | 调度按配方分派抓取器；写错则该品牌整季抓不到且**不报错** |

共同点与上一轮完全一致：**写入时不报错，读取时不一定报错，指标对不上才发现**。

本轮同样**不做 DB 级 CHECK 迁移**——理由与上一轮相同（SQLite 加 CHECK 需整表重建），
且触发条件仍是「词汇表连续 2~3 个季度不再新增 + 巡检持续通过」。

---

## 2. 做了什么

### 2.1 单一词汇表 + 通用校验器（`db.py`）

新增 6 组常量与集合，并抽出**通用校验器**（避免 6 份复制粘贴）：

```python
CATEGORY_PHONE/TABLET/WATCH/EARBUDS/WEARABLE/OTHER   -> VALID_CATEGORIES (6), DEFAULT_CATEGORY
TIER_FLAGSHIP/HIGH/MID/ENTRY = "旗舰"/"高端"/"中端"/"入门" -> VALID_TIERS (4)
MODEL_URL_KIND_API/TEXT_FRAGMENT/PAGE/CATEGORY_API/BRAND_ENTRY -> VALID_MODEL_URL_KINDS (5)
SNAPSHOT_URL_KIND_REFERENCE_CN + 上述 5 值 -> VALID_SNAPSHOT_URL_KINDS (6)   # 独立对象
ISSUE_OPEN/RESOLVED/WONT_FIX -> VALID_ISSUE_STATUSES (3)
RATE_SOURCE_STATIC = "static"; RATE_SOURCE_LIVE_PREFIX = "live:"   # 前缀模式，非穷举
RECIPE_FORM_SELECT_CASCADE/API_REBORN/SAMSUNG_API/VIVO_API/XIAOMI_API -> VALID_RECIPE_MODES (5)
```

```python
def _validate_enum(value, valid, label, allow_none=False, prefix_ok=()):
    """非法即抛 ValueError，并附拼写纠错提示。
    allow_none=True —— NULL 有明确含义的列（model_url_kind=None 表示未生成链接）
    prefix_ok       —— 前缀模式列（rate_source 的 "live:<endpoint>"）
    """
```

### 2.2 三个必须特殊处理的地方

1. **NULL 是合法值，不是错误**。`models.model_url_kind` 有 1445 行 NULL（未生成取证链接）、
   `exchange_rates.rate_source` 有 33 行 NULL（存量行）。校验器用 `allow_none=True` 放行，
   巡检也**不把 NULL 计入非法**，只在 INFO 里报告 NULL 分布——否则会制造 1478 个假警报。
2. **前缀模式**。`rate_source` 形如 `live:<endpoint>`，端点会随数据源变化，**不能穷举**。
   巡检不能用 `NOT IN`，必须 `rate_source <> 'static' AND rate_source NOT LIKE 'live:%'`。
3. **两套词汇表**（见 §3.1）。

### 2.3 写入口收敛

| 写入口 | 新增校验 |
|---|---|
| `upsert_model()` | `category` / `tier` / `model_url_kind` |
| `upsert_brand()` | `recipe_mode` |
| `set_rate()` / `fetch_exchange_rates()` | `rate_source` |
| `insert_snapshot()` | `rate_source` + `source_url_kind`（**分别用各自的集合**） |
| `add_issue()` / `open_issues()` / `resolve_issue()` | 状态字面量 → `ISSUE_OPEN`/`ISSUE_RESOLVED` 常量 |

所有 SQL 里的字面量一并改成参数化常量，避免「常量改了、SQL 里的字符串没改」的漂移。

### 2.4 调用侧词汇表绑定到单一来源（消除复制粘贴漂移）

- `crawler/model_links.py`：`KIND_*` 改为 `db.MODEL_URL_KIND_*` 的绑定别名（附 `assert` 防 None），
  写入时**分别**调 `validate_model_url_kind()` 与 `validate_snapshot_url_kind()`——
  因为一次抓取同时写 `models` 和 `price_snapshots` **两张不同词汇表的表**。
- `crawler/samsung_api.py`：`_DE_TYPE_CATEGORY` / `_JP_TABLE_CATEGORY` 映射表绑定到
  `db.CATEGORY_*`，4 处残留 `"phone"`/`"other"` 字面量 → 常量。

### 2.5 可观测（`api/server.py`）

- 新增 `_invalid_enum_counts(c)`：一次统计 8 列非法值（`rate_source` 走 `NOT LIKE 'live:%'`），
  挂到 `kpis["invalid_enum_values"]`——**打开 `/api/overview` 就能看到，无需跑巡检**。
- `COALESCE(m.category,'phone')` 与 `status='open'` 等字面量改为从 `db.DEFAULT_CATEGORY` /
  `db.ISSUE_OPEN` 构造的单一来源 SQL 片段。
- `api_tier_matrix` 遇到非法 `category` 时往 stderr 告警（原来静默返回 0 行，
  看起来像「该品类没数据」，实为传参写错）。

### 2.6 巡检兜底（`tools/verify_quarterly_run.py`）

新增 `check_enum_vocabularies()`，覆盖 8 列，**ERROR 级**，消息里带后果说明：

```
i [INFO ] ENUM_VOCAB   枚举列白名单通过（8 列）；NULL 分布：models.model_url_kind=1445
```

写入口断言挡**新数据**，巡检挡**历史遗留 + 绕过写入口的直接 SQL**——两者互补，不可互替。

---

## 3. 踩到的坑（本轮新增经验）

### 3.1 ⚠️ 同名不同义词汇表（第二次遇到）

`models.model_url_kind` 与 `price_snapshots.source_url_kind` 共用
`model_api` / `model_page` / `category_api_locator` / `brand_entry` 等**同样的字符串**，
直觉上会想合并成一个集合，但它们是**两套词汇表**：

- `price_snapshots.source_url_kind` 多一个 `reference_cn`（`is_reference=1` 借用参考区价格时的标注）；
- 往 `models` 新增一种 kind，**不应该**静默把 `price_snapshots` 也放开（反之亦然）。

处理方式：**共享值常量、各自定义集合**（`VALID_SNAPSHOT_URL_KINDS` 独立 frozenset）。
这正是上一轮 `run_logs.status` 与 KB `rec.status` 同名不同义的教训，
说明这不是偶然，而是这类遗留库的**结构性风险**——已写入 SKILL。

### 3.2 中文短词的拼写提示会误报

`validate_tier('顶级')` 最初给出「是否想写 '中端'？」——因为两个两字中文词的编辑距离恒为 2，
固定阈值 `≤ 2` 必然命中。修正为 `_spelling_hint()`：要求 `best_d <= 2 AND best_d < len(候选)`，
即编辑距离必须**严格小于候选词长度**。修正后 `'顶级'` 不再提示（两字词距离 2 不 < 2），
而 `'phonee'→'phone'`、`'旗舰版'→'旗舰'`（距离 1 < 2/3）仍正常提示。

### 3.3 回归测试自己的坑：空表导致 UPDATE 命中 0 行

`exchange_rates` 在临时库里是空的，而测试只断言「非法值被拒」（走的是 UPDATE），
于是 0 行被改、统计恒为 0，与预期「1 处非法」对不上。修正：先做**正向写入**
（`set_rate(static)` + `set_rate('live:example.org')`）再造脏数据。
教训——**负向用例必须先有正向数据打底**，否则「被拦截」和「根本没这行」无法区分。

---

## 4. 验证证据

### 4.1 静态回归（Python 3.12）

| 用例 | 结果 |
|---|---|
| `tools/test_enum_vocabularies.py`（新，6 节） | 全部通过 ✓ |
| `tools/test_run_log_status.py`（上一轮，含 [5] 节 30 断言） | 全部通过 ✓ |
| `tools/test_amount_parse.py` | 通过 ✓ |
| `tools/verify_quarterly_run.py` | **ERROR 0** / WARN 0 / INFO 3 ✓ |
| `tools/verify_status_ui.py` | 通过 ✓ |
| `tools/test_push_pipeline.py` | 通过 ✓ |
| `monitor.py --report` | 35 个 scope 无 UNKNOWN ✓ |
| `output/gen_improvement_report.py` | 正常生成 ✓ |

巡检实况输出：

```
i [INFO ] STATUS_VOCAB   run_logs.status 白名单通过：resumed=186、success=135、failed=30、unavailable=26
i [INFO ] ENUM_VOCAB     枚举列白名单通过（8 列）；NULL 分布：models.model_url_kind=1445
合计：ERROR 0 / WARN 0 / INFO 3
```

### 4.2 负向注入（证明巡检不是「永远通过」）

在临时库分别注入 7 处非法值（`category='Phone'`、`tier='顶级'`、`model_url_kind='modle_api'`、
`source_url_kind='page'`、`maintenance_queue.status='opened'`、`recipe_mode='form_cascade'`、
`rate_source='live'`），巡检**全部捕获**并判 ERROR（exit 1）；复原后重新通过。

### 4.3 动态验证：真实抓取冒烟（关键）

静态检查只能证明「取值来源合法」，不能证明「新增断言不会在生产路径上误伤」。
`tools/smoke_crawl_enum_guard.py` 把生产库复制到临时目录、重定向 `db.DB_PATH`，
跑一次**真实抓取**，写完后统计 8 列非法值：

```
生产库副本（只读来源）：C:\Users\Dong\spare-parts-monitor\spare_parts.db
写入目标（临时库）：C:\Users\Dong\AppData\Local\Temp\smoke_crawl_enum_c0k4pen5\t.db
KB 记录 mode = samsung_api
抓取结果：status=success 写入=54 条
抓取后枚举非法值计数（应全为 0）：
   models.category 0 | models.tier 0 | models.model_url_kind 0
   price_snapshots.source_url_kind 0 | maintenance_queue.status 0 | brands.recipe_mode 0
   合计（含 rate_source 前缀列）0
结论：PASS —— 真实抓取未被写入口断言误伤 ✓
```

非破坏性：生产库全程只读，写入落在临时副本。

### 4.4 服务端

后端重启后：

- `/api/overview` → **HTTP 200**，`kpis["invalid_enum_values"]` 全部为 0；
- `/api/tier_matrix?tier=高端&category=phone` → **HTTP 200**，rows=24；
- 非法 `category=Phone` → **HTTP 200** + stderr `[warn]`（不再静默返回 0 行，也不会 500）。

---

## 5. 遗留与边界

| 项 | 状态 |
|---|---|
| DB 级 CHECK 迁移（8 列） | **不做**。触发条件：词汇表连续 2~3 个季度不再新增 + 巡检持续通过。届时注意：`run_logs`/`maintenance_queue` 是叶子表较安全，但迁移必须**独占数据库**（先停爬虫与 API） |
| 推送链路 | 仍未接通，待 `config.push.json`（企微 webhook + SMTP 凭据） |
| `models.model_url_kind` 1445 行 NULL | 合法存量（未生成链接），非缺陷；巡检按 INFO 报告 |
| `snapshot_url_kind='reference_cn'` / `ISSUE_WONT_FIX` | 词汇表已收录但当前 0 行，属文档化意图，保留 |
| `output/gen_improvement_report.py` | 被 `.gitignore` 排除（`output/*`），改动仅本地生效 |

---

## 6. 一句话结论

8 个枚举列现在都有**单一词汇表 + 写入口断言（挡新） + 巡检白名单（挡旧/挡绕过） + API 可观测**，
并由一次**真实抓取**证明护栏不误伤生产路径；DB 级 CHECK 仍按约定推迟。
