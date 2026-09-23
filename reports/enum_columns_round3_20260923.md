# 枚举列加固 · 第三轮：全库普查 + 推导型词表（part_type / currency / lang）

日期：2026-09-23
前序：`reports/enum_columns_hardening_20260923.md`（第二轮，commit `ddc311c`）
      `reports/status_vocabulary_hardening_20260923.md`（第一轮，commit `d6e0971`）
范围：`db.py` / `crawler/run.py` / `api/server.py` / `tools/verify_quarterly_run.py`
      / `tools/test_enum_vocabularies.py` / `tools/smoke_crawl_enum_guard.py`
      / `tools/smoke_generic_path_guard.py`（新）
状态：已完成并全量回归通过（含两条写入路径的真实抓取强证据）

---

## 1. 起因：我上一轮的结论是错的

第二轮收尾时我写过一句「剩下的自由文本列主要是纯备注类」。这句话**没有数据支撑**，
于是本轮先做了一次**全库列级普查**（每张表每个 TEXT 列的分组基数 + 样例 + 消费方式），
结果推翻了这个结论——至少 3 组列是货真价实的枚举语义，且风险不低：

| 表.列 | 基数 | 消费方式 | 漏掉它的后果 |
|---|---|---|---|
| `parts.part_type` / `canonical_type` | 24 | **比价聚合的分组键**（`api/server.py` 9 处 `COALESCE(canonical_type, part_type)` 做分组/过滤） | 写错凭空多出一个分组，同一类备件被拆成两行，**静默拆散比价矩阵**；影响 4.4 万行，比 `category` 更大 |
| `countries.currency` / `price_snapshots.currency` | 7 | `get_rate()` 折算 | 写错 → `get_rate` 返回 None → `run.py` 的 `cny = price * rate if rate else None` **静默产出 cny_price=NULL 的行**，该行随即从所有 CNY 口径的比价里消失，全链路不报错 |
| `parts.lang` | 5 | 语种别名查表 | 写错则静默回退 fallback 归一 |

另有一处**文档与实现不一致**：`part_norm.NormResult.rule` 的注释声明
`alias | alias_full | fallback` 三态，但 `alias_full` **全仓无任何代码写入**
（只有 `rule="alias"` 与 `rule="fallback"` 两个赋值点）。

**教训**：「还有哪些列没加固、为什么」必须靠普查回答，不能凭印象——
「刻意排除 + 理由」和「加固」同样重要，它让下一个人不必重新论证一遍。

---

## 2. 核心手法：能推导就不要手抄

本轮最重要的改进：词汇表**不手写**，而是从**唯一的生成器**推导。

| 词汇表 | 生成器（唯一来源） | 推导方式 |
|---|---|---|
| `VALID_PART_TYPES`（28 值） | `db._MANUAL_CATEGORY_MAP`（27 条正则→标签）+ 兜底「其他」 | `frozenset({lab for _rx, lab in _MANUAL_CATEGORY_MAP}) \| {PART_TYPE_OTHER}` |
| `VALID_CURRENCIES`（11 值） | `db.STATIC_RATES` | `frozenset(STATIC_RATES)` —— **能折算才算合法** |
| `VALID_LANGS`（5 值） | `crawler.part_norm._detect_lang()`（仅 5 个 return 分支） | 手工列举 + 测试核对与函数分支一一对应 |

为什么这么重要：手抄的副本必然在「往映射表加了一条正则、却忘了改常量」时静默失配，
而这正是本类加固要消灭的问题。推导则让它**不可能失配**。

回归测试里**独立重推一遍**并逐项比较（否则「从生成器推导」只是口号）：

```python
rederived = {lab for _rx, lab in db._MANUAL_CATEGORY_MAP} | {db.PART_TYPE_OTHER}
check("VALID_PART_TYPES == 独立重推的结果", set(db.VALID_PART_TYPES), rederived)
check("_MANUAL_CATEGORY_MAP 的每个标签都在白名单内",
      all(lab in db.VALID_PART_TYPES for _rx, lab in db._MANUAL_CATEGORY_MAP), True)
```

推导成立的**前提是生成器本身封闭**。反例：`exchange_rates.currency` 的取值由
汇率接口灌入（实测 166 种），是开放词表，**刻意不加白名单**。

---

## 3. 改动清单

### 3.1 `db.py`

- 新增 `PART_TYPE_OTHER`、`NORM_RULE_ALIAS/ALIAS_FULL/FALLBACK` + `VALID_NORM_RULES`。
- 新增 `VALID_PART_TYPES`（从 `_MANUAL_CATEGORY_MAP` 推导）、`VALID_LANGS`。
- 新增 `VALID_CURRENCIES`（绑定 `STATIC_RATES`）。
- 通用校验器新增 `allow_empty` 参数（**空串 ≠ NULL**：SQL 的 `COALESCE` 不认空串，
  空值会被当成一个名为 `''` 的真实分组）。
- 新增 `validate_part_type / validate_lang / validate_norm_rule / validate_currency`。
- `upsert_part()` 校验 `part_type`、`canonical_type`、`lang`、`norm_rule`；
  `upsert_country()` 与 `insert_snapshot()` 校验 `currency`。
  用 `try / else` 结构把枚举断言与「归一化引擎失败则回退」的兜底分开，
  避免兜底把断言吞掉。

### 3.2 `crawler/run.py`

- `write_rows()` 里的 `"model_api" / "model_page" / "brand_entry"` 三个字面量
  改为单源常量（上一轮扫列时漏掉了这个**局部变量**）。
- ⚠️ 第一版改动引入了 `db.MODEL_URL_KIND_*`，但该文件用的是
  `from db import (...)` 而**没有** `import db` —— 会在运行时 `NameError`。
  已改为把常量加进现有的 `from db import (...)` 列表。

### 3.3 `api/server.py`

- `_invalid_enum_counts()` 从 8 列扩到 **15 列**（新增 `parts.part_type`、
  `parts.canonical_type`、`parts.lang`、`parts.norm_rule`、`countries.currency`、
  `price_snapshots.currency`），挂到 `kpis["invalid_enum_values"]`。
- 该函数新增 `allow_empty` 支持。

### 3.4 `tools/verify_quarterly_run.py`

- `_enum_specs()` 从 6 条扩到 **14 条**（+2 处 `rate_source` 前缀列 = 16 处检查），
  每条带「后果」说明；新增 `allow_empty` 字段。

### 3.5 工具（新增 / 增强）

- **新增** `tools/smoke_generic_path_guard.py`：非 `samsung_api` 配方的端到端冒烟，
  走 `crawler/run.py` 通用路径（`run_all`），并**每次自证生产库行数未变**。
- `tools/smoke_crawl_enum_guard.py`：新增 `--fresh`；非 `samsung_api` 配方改为
  **明确 skip 并提示替代命令**（原先会用一个不匹配的配方跑出 failed，
  极易被误读成「护栏误伤生产」）。
- 两个冒烟脚本都新增「净增 / upsert 行数」分开报告（见 §5）。

---

## 4. 顺带发现的真实缺陷（已量化，未修，待决）

给 `parts.lang` 建白名单时，测试断言直接失败并暴露出一个**生产缺陷**：

```
_detect_lang("Rückglas")  →  'tr'    （期望 'de'）
```

根因：`crawler/part_norm._detect_lang()` 的判定顺序是
`日语 → 土耳其字符类 [ıİşŞğĞçÇöÖüÜ] → 中文 → 德语词表`，
而德语 `Rückglas` / `Rückkamera` 含 `ü`，被**先匹配的土耳其字符类**抢先命中。

**影响面（实测）**：

| 国家 | 被标为 `tr` 的备件 | 说明 |
|---|---|---|
| `de` | **84 行** | 德语损失类标签（`Rückglasschaden`、`Schaden an der Rückkamera`…） |

**严重度：P2（标签错误，不影响正确性）**——`part_norm.normalize()` 的查表键是
`(part_type, key)`，**不含 `lang`**，所以归一化本身不受影响，`lang` 也不参与任何比价计算。

**处置**：不属于本轮加固范围，且修它属于**行为变更 + 需回填 84 行**，
故按「等你点头再动」的惯例**未修改**。但也没有放任：在
`tools/test_enum_vocabularies.py` [7.3b] 加了一条**「已知缺陷」断言**把当前行为钉住
（`_detect_lang("Rückglas") == "tr"`），一旦有人修复，这条断言会失败并提醒同步更新本报告。

> 顺带发现文档不一致：`NormResult.rule` 注释声明 `alias_full`，但无代码写入该值。
> 已纳入 `VALID_NORM_RULES`（宽松处理，不误报），并在巡检消息里注明。

---

## 5. 验证证据

### 5.1 静态回归（Python 3.12）

| 用例 | 结果 |
|---|---|
| `tools/test_enum_vocabularies.py`（扩至 8 节） | 全部通过 ✓ |
| `tools/test_run_log_status.py` | 全部通过 ✓ |
| `tools/test_amount_parse.py` | 全部通过 ✓ |
| `tools/verify_status_ui.py` | 通过 ✓（退出码 0） |
| `tools/test_push_pipeline.py` | 全部通过 ✓ |
| `tools/verify_quarterly_run.py` | **ERROR 0 / WARN 0 / INFO 3** ✓ |

巡检实况：

```
i [INFO ] STATUS_VOCAB   run_logs.status 白名单通过：resumed=186、success=135、failed=30、unavailable=26
i [INFO ] ENUM_VOCAB     枚举列白名单通过（14 列 + rate_source 前缀列 2 处）；NULL 分布：models.model_url_kind=1445
合计：ERROR 0 / WARN 0 / INFO 3
```

在**真实生产库**上，新增的 7 个列全部零误报——这同时验证了推导出的词表是准确的。

### 5.2 负向注入

在临时库注入非法值（`part_type='屏暮'`、`currency='EURO'`、`currency=''` 等），
全部被写入口拦下且**未落库**，巡检判 ERROR；复原后重新通过。

### 5.3 动态验证：两条写入路径的真实抓取（本轮最强证据）

**这里踩了一个「假证据」陷阱，值得单独记录**：冒烟脚本一开始显示
「写入 54 条」但表行数 `41523 -> 41523` 不变——看起来像「什么都没写」，
很容易被误读成「护栏把写入全拦了」。真相是 `insert_snapshot` 有
`UNIQUE(part_id, quarter)` + `ON CONFLICT DO UPDATE`：**重抓同一季度是 UPDATE 而非 INSERT**。

为此给两个冒烟脚本加了 `--fresh`：抓取前先删掉该 brand/country 本季快照，
让写入必须是 INSERT，从而得到「**净增 N 行**」的强证据。

| 路径 | 配方 | 清空后抓取 | 非法值 | 生产库 |
|---|---|---|---|---|
| `samsung_api` 专用路径 | samsung_api（samsung/cn） | **净增 54 行** | 0 | 未改动（41523）✓ |
| `run.py` 通用路径 | api_reborn（oppo/cn，275 机型） | **净增 4603 行** | 0 | 未改动（41523）✓ |

> oppo/cn 净增 4603 < 清空的 6841：因为本次实抓只取到 4606 条价（官网可用性波动），
> 属正常抓取波动——**不是**护栏拦截（护栏拦截会抛 `ValueError` 且非法值不为 0）。

两条路径都印证：**新增的写入口断言不误伤生产路径，且写入真的发生了**。

### 5.4 静态 lint（新增）

AST 全仓检查「引用了 `mod.X` 却没 `import mod`」——本轮真实踩到
（`run.py` 缺 `import db`，导入期不报错、只在跑到那条抓取路径时才 `NameError`）。
当前全仓 **0 违规**。同时断言 `run.MODEL_URL_KIND_*` 确实等于 `db.MODEL_URL_KIND_*`。

---

## 6. 全库 TEXT 列分类清单（本轮普查产出）

### A. 已加固（16 处检查 / 15 列）

`run_logs.status`、`models.category`、`models.tier`、`models.model_url_kind`、
`price_snapshots.source_url_kind`、`price_snapshots.rate_source`、
`exchange_rates.rate_source`、`maintenance_queue.status`、`brands.recipe_mode`、
**`parts.part_type`**、**`parts.canonical_type`**、**`parts.lang`**、**`parts.norm_rule`**、
**`part_alias.part_type`**、**`part_alias.canonical_type`**、**`countries.currency`**、
**`price_snapshots.currency`**（粗体为本轮新增）。

### B. 刻意排除（附理由，避免后来者重新论证）

| 列 | 理由 |
|---|---|
| `exchange_rates.currency`（166 值） | **开放词表**：由汇率接口响应灌入，接口返回什么就是什么 |
| `models.color`(21) / `edition`(8) / `spec`(58) / `base_model`(3361) | **开放词表**：新配色/新规格随官网持续出现，加白名单会天天误报 |
| `models.name` / `model_key` / `source_url` / `model_url` / `model_page_url` / `model_url_locator` | 自由文本 / URL / JSON |
| `parts.name` / `canonical_name` / `canonical_spec` / `variant`；`part_alias.alias_key` / `canonical_name` / `source` | 自由文本（规格组合天然开放） |
| `run_logs.error_text` / `anomaly_reason`；`maintenance_queue.issue_summary` / `diagnosis` / `proposed_fix` / `resolved_by` | 自由文本（`resolved_by` 含 `manual(Dong)` 这类带变量的值） |
| `brands.note` / `price_caveat`；`third_party_prices.note`；`*.quarter` / `*.captured_at` / `*.rate_as_of` 等 | 备注文本 / 标识 / 时间戳 |
| `countries.code` / `name` / `locale` | 标识与展示名（`code` 是主键） |
| **归档表与隔离表**（`*_legacy_archive`、`*_quarantine`、`*_badfix`、`*_hermes`） | 历史快照，**不再写入**，刻意不约束。反证：`price_snapshots_legacy_archive.rate_source` 里存在 `static(legacy)` 这种历史值——正说明历史表不该被当前词表约束 |

---

## 7. 遗留与边界

| 项 | 状态 |
|---|---|
| DB 级 CHECK 迁移 | **仍不做**。触发条件：词汇表连续 2~3 个季度不再新增 + 巡检持续通过 |
| `_detect_lang` 的 tr/de 歧义（de 国 84 行） | **未修**（行为变更 + 需回填），已用「已知缺陷」断言钉住并量化，待确认 |
| `NormResult.rule` 声明 `alias_full` 但无代码写入 | 已纳入白名单（不误报），文档与实现不一致待决 |
| 推送链路 | 仍未接通，待 `config.push.json` |
| `output/gen_improvement_report.py` | 被 `.gitignore` 排除（`output/*`），改动仅本地生效 |

---

## 8. 一句话结论

先做**全库列级普查**（推翻了上一轮的乐观结论），再用**从生成器推导**的方式给
备件品类 / 币种 / 语种等 7 个列加上护栏；过程中抓出一个真实的语种误判缺陷
（已量化并钉住，未擅自修改），并用**两条写入路径的真实抓取（净增 54 / 4603 行）**
证明护栏不误伤生产。
