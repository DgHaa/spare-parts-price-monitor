"""db.py - 中台 SQLite 存储层（零依赖）。

季度快照天然时间序列：price_snapshots 按 (part_id, quarter) 落点，
支撑比价矩阵(某季度透视)、价格走势(跨季度曲线)、异动告警(环比)。

表：
  brands            品牌 + 抓取配方类型(来自 skill KB 的 query.mode)
  countries         国家代码 + 币种 + 语区
  models           品牌×国别×机型(发现阶段填充)
                   base_model = 归一化基础机型(去掉规格/颜色，用于跨规格聚合比价)
                   spec       = 规格/SKU(如 8GB+256GB)，同一基础机型的同一种备件价格可能因规格而异
  parts            机型×备件
  price_snapshots  备件×季度×价格(原币+CNY等值+物料/人工费)
  exchange_rates   季度×币种→CNY(快照，可追溯)
"""
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "spare_parts.db"

# ── run_logs.status 合法取值（应用层枚举）─────────────────────────────────
# 背景：status 列是无约束 TEXT，SQLite 会照单全收任何字符串（'succes' / 'Success'
# / '' / NULL），而下游消费者对未知值的兜底并不一致：
#   api/server.py 覆盖率推导  -> else 分支判为 empty（虚增"从未抓到"缺口）
#   gen_improvement_report.py -> status IN ('failed','partial') 匹配不到（故障漏报）
#   monitor.py                -> else 判为 FAIL（会喊出来，安全）
#   web/app.js                -> 显示原始文本（样式错但不丢信息）
# 即"写入不报错、读取不一定报错、指标对不上才发现"。这里收敛为单一常量源 +
# 写入前断言，让拼写错误在 log_run() 处立即抛出（fail fast），零迁移成本。
#
# 注意：这里只做应用层软约束，尚未加 DB 级 CHECK 约束。SQLite 不支持
# ALTER TABLE ADD CONSTRAINT，需整表重建（关外键→建新表→拷数据→删旧表→改名→
# 重建索引→foreign_key_check）。待状态集合稳定（连续 2~3 个季度不再新增）后，
# 由 tools/verify_quarterly_run.py 的白名单巡检确认无脏数据，再做一次性迁移。
STATUS_SUCCESS = "success"          # 本轮抓取成功
STATUS_PARTIAL = "partial"          # 部分品类/机型成功
STATUS_FAILED = "failed"            # 真失败（含"自动发现 0 机型"这类静默丢数据）
STATUS_RESUMED = "resumed"          # 断点续跑：本季机型均已抓取，本轮无新增（正常）
STATUS_UNAVAILABLE = "unavailable"  # 官网不提供备件价 / KB 人工研判需真机代理（非我方缺口）
STATUS_SKIPPED = "skipped"          # 未收录：无 KB 记录

#: run_logs.status 的全部合法取值。任何写入必须命中此集合。
VALID_STATUSES = frozenset({
    STATUS_SUCCESS, STATUS_PARTIAL, STATUS_FAILED,
    STATUS_RESUMED, STATUS_UNAVAILABLE, STATUS_SKIPPED,
})

#: 中性状态：不代表失败，不应触发待修队列。
NEUTRAL_STATUSES = frozenset({STATUS_RESUMED, STATUS_UNAVAILABLE, STATUS_SKIPPED})

#: 多品类汇总时的状态优先级（越靠前越"需要关注"）。
STATUS_PRIORITY = (STATUS_FAILED, STATUS_PARTIAL, STATUS_SUCCESS,
                   STATUS_RESUMED, STATUS_UNAVAILABLE, STATUS_SKIPPED)


def validate_status(status):
    """校验 run_logs.status 合法性；非法时抛 ValueError，并给出拼写纠错提示。"""
    if status in VALID_STATUSES:
        return status
    if status is not None:
        low = str(status).strip().lower()
        for s in VALID_STATUSES:
            if s.lower() == low:
                raise ValueError(
                    f"非法 run_logs.status={status!r}：大小写不匹配，应为 {s!r}")
    hint = _spelling_hint(status or "", VALID_STATUSES)
    raise ValueError(
        f"非法 run_logs.status={status!r}{hint}；合法值：{sorted(VALID_STATUSES)}")


def _edit_distance(a, b):
    """Levenshtein 距离（仅用于拼写纠错提示，输入极短，无需优化）。"""
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _spelling_hint(value, valid):
    """给出「是否想写 X？」提示；不确定时返回空串（宁可不说，也别误导）。

    仅在**编辑距离明显小于候选长度**时才提示。这条约束是为了挡住中文短词的伪匹配：
    「顶级」与「中端」编辑距离为 2，而两个字的中文词互相比较距离恒为 2，
    若只看固定阈值就会提示「是否想写 '中端'？」，反而误导。
    对 'succes'(6) → 'success' 距离 1 < 6，仍会正常提示。
    """
    best, best_d = None, 99
    for s in valid:
        d = _edit_distance(str(value).strip().lower(), str(s).lower())
        if d < best_d:
            best, best_d = s, d
    if best is not None and best_d <= 2 and best_d < len(str(best)):
        return f"，是否想写 {best!r}？"
    return ""


def summarize_status(statuses, default=STATUS_SKIPPED):
    """多品类区域的 run_log 状态汇总：按 STATUS_PRIORITY 取「最需关注」的那个。

    一个 brand×country 可能有多条 KB 记录（如 Apple phone/tablet/watch），逐品类抓取后
    要汇总成**一条** run_log（否则同秒多条日志会互相覆盖，见 crawler/run.py 说明）。

    语义（2026-09-23 状态拆分后）：
      failed > partial > success > resumed > unavailable > skipped
      即"有坏消息先报坏消息；有好消息就报好消息；都没有才报中性的续跑/不可用"。

    特例：unavailable 仅当**全部**品类都不可用时才成立。若与 skipped 混合，说明仍有
    品类属"未收录"（我方缺口），按 skipped 上报 —— 不能把缺口说成"对方不提供"。
    空列表回退 default。
    """
    if not statuses:
        return default
    for st in STATUS_PRIORITY:
        if st == STATUS_UNAVAILABLE:
            if all(s == STATUS_UNAVAILABLE for s in statuses):
                return STATUS_UNAVAILABLE
            continue
        if st == STATUS_SKIPPED:
            return STATUS_SKIPPED
        if st in statuses:
            return st
    return default


# ── 其余枚举列（同一加固模式：单源常量 + 写入口断言 + 巡检白名单）─────────────
# 这些列与 run_logs.status 同病：自由文本、无 CHECK 约束、且被多处读取，其中
# 存在**静默**消费者 ——
#   api/server.py  用 COALESCE(m.category,'phone') 兜底：NULL 会被兜成 phone，
#                  但**拼错的值不会被兜住**，会凭空多出一个分组，静默拆散比价矩阵
#                  （且按 category 过滤时那一行直接不可见）
#   api_tiers()    直接 SELECT DISTINCT tier 喂给下拉框：拼错值会变成一个"新档位"选项
# 分工：写入口断言挡新数据；巡检白名单挡历史遗留与绕过写入口的直接 SQL。

# models.category —— 产品品类。⚠️ 跨品类比价无意义（平板屏幕 ≠ 手机屏幕），
# 比价矩阵与价格走势都必须按品类分组，故品类写错会静默污染比价结论。
CATEGORY_PHONE = "phone"
CATEGORY_TABLET = "tablet"
CATEGORY_WATCH = "watch"
CATEGORY_EARBUDS = "earbuds"
CATEGORY_WEARABLE = "wearable"
CATEGORY_OTHER = "other"
VALID_CATEGORIES = frozenset({
    CATEGORY_PHONE, CATEGORY_TABLET, CATEGORY_WATCH,
    CATEGORY_EARBUDS, CATEGORY_WEARABLE, CATEGORY_OTHER,
})
#: 与 guess_category() 的兜底、api/server.py 的 COALESCE(...,'phone') 保持一致
DEFAULT_CATEGORY = CATEGORY_PHONE

# models.tier —— 产品档位（"同档位·跨品牌对标"维度的分组键）
TIER_FLAGSHIP = "旗舰"
TIER_HIGH = "高端"
TIER_MID = "中端"
TIER_ENTRY = "入门"
VALID_TIERS = frozenset({TIER_FLAGSHIP, TIER_HIGH, TIER_MID, TIER_ENTRY})

# models.model_url_kind —— 机型级取证链接的类型。
# None 是**合法**值：表示该机型尚未生成/未校验取证链接（库内现有 1445 行为 NULL）。
MODEL_URL_KIND_API = "model_api"
MODEL_URL_KIND_TEXT_FRAGMENT = "model_text_fragment"
MODEL_URL_KIND_PAGE = "model_page"
MODEL_URL_KIND_CATEGORY_API = "category_api_locator"
MODEL_URL_KIND_BRAND_ENTRY = "brand_entry"
VALID_MODEL_URL_KINDS = frozenset({
    MODEL_URL_KIND_API, MODEL_URL_KIND_TEXT_FRAGMENT, MODEL_URL_KIND_PAGE,
    MODEL_URL_KIND_CATEGORY_API, MODEL_URL_KIND_BRAND_ENTRY,
})

# price_snapshots.source_url_kind —— ⚠️ 与 models.model_url_kind 是**两套不同词汇表**：
# 取值是后者的子集，另多一个 reference_cn（is_reference=1 借用参考区价格时的标注）。
# 这里刻意**共享值常量、各自定义集合** —— 往 models 新增一种 kind 时，不应静默把
# price_snapshots 也放开（反之亦然）。这正是 run_logs.status 与 KB status 同名的教训。
SNAPSHOT_URL_KIND_REFERENCE_CN = "reference_cn"
VALID_SNAPSHOT_URL_KINDS = frozenset({
    MODEL_URL_KIND_API, MODEL_URL_KIND_TEXT_FRAGMENT, MODEL_URL_KIND_PAGE,
    MODEL_URL_KIND_CATEGORY_API, MODEL_URL_KIND_BRAND_ENTRY,
    SNAPSHOT_URL_KIND_REFERENCE_CN,   # 仅此列有；文档化意图（当前 is_reference=1 为 0 行）
})

# maintenance_queue.status —— 待修工单状态
ISSUE_OPEN = "open"
ISSUE_RESOLVED = "resolved"
ISSUE_WONT_FIX = "wont_fix"   # 文档化的第三态；目前无代码写入，保留意图
VALID_ISSUE_STATUSES = frozenset({ISSUE_OPEN, ISSUE_RESOLVED, ISSUE_WONT_FIX})

# exchange_rates.rate_source —— 汇率来源。形状为 "static" 或 "live:<endpoint>"，
# 是**前缀模式**而非穷举集合（端点会随数据源变化），故放行 "live:" 前缀。
RATE_SOURCE_STATIC = "static"
RATE_SOURCE_LIVE_PREFIX = "live:"

# brands.recipe_mode —— 抓取配方类型（取值来自 KB 的 query.mode）
RECIPE_FORM_SELECT_CASCADE = "form_select_cascade"
RECIPE_API_REBORN = "api_reborn"
RECIPE_SAMSUNG_API = "samsung_api"
RECIPE_VIVO_API = "vivo_api"
RECIPE_XIAOMI_API = "xiaomi_api"
VALID_RECIPE_MODES = frozenset({
    RECIPE_FORM_SELECT_CASCADE, RECIPE_API_REBORN, RECIPE_SAMSUNG_API,
    RECIPE_VIVO_API, RECIPE_XIAOMI_API,
})

# parts.part_type / parts.canonical_type / part_alias.part_type / part_alias.canonical_type
# —— **备件品类**。这是比价聚合的核心分组键（api/server.py 有 9 处
# `COALESCE(p.canonical_type, p.part_type)` 做分组/过滤），写错会凭空多出一个分组、
# 静默拆散比价矩阵——与 models.category 同构，但影响面更大（库内 4.4 万行）。
# ⚠️ 词汇表**不在此手写**：它由 `_MANUAL_CATEGORY_MAP`（唯一的生成器）推导而来，
# 见该表下方的 VALID_PART_TYPES。手抄一份必然与生成器漂移（往映射表加一条正则
# 却忘了同步常量），故刻意从源头推导。
PART_TYPE_OTHER = "其他"   # 兜底类：normalize_category() 未命中任何规则时的落点

# parts.norm_rule —— 备件名归一化所走的路径（内部溯源字段）
NORM_RULE_ALIAS = "alias"
NORM_RULE_ALIAS_FULL = "alias_full"   # ⚠️ 仅见于 part_norm.NormResult 的文档注释，
                                      #    当前**无任何代码写入**（文档与实现不一致，
                                      #    已记入 reports/enum_columns_round2 待决）
NORM_RULE_FALLBACK = "fallback"
VALID_NORM_RULES = frozenset({NORM_RULE_ALIAS, NORM_RULE_ALIAS_FULL, NORM_RULE_FALLBACK})


def _validate_enum(value, valid, label, allow_none=False, prefix_ok=(), allow_empty=False):
    """通用枚举校验：非法即抛 ValueError，并附拼写纠错提示。

    allow_none=True  —— 用于 NULL 有明确含义的列（如 model_url_kind=None 表示未生成链接）
    prefix_ok        —— 用于前缀模式列（如 rate_source 的 "live:<endpoint>"）
    allow_empty=True —— 用于「空串表示无值」的列（如 part_alias.canonical_type，
                        空串 = 不做类型改写；注意 SQL 里 COALESCE 不认空串，
                        故仅用于那些空串确实被消费方当"无"处理的列）
    """
    if value is None:
        if allow_none:
            return value
        raise ValueError(f"非法 {label}=None；合法值：{sorted(valid)}")
    if value in valid:
        return value
    if allow_empty and value == "":
        return value
    if isinstance(value, str):
        for p in prefix_ok:
            if value.startswith(p) and len(value) > len(p):
                return value
        low = value.strip().lower()
        for s in valid:
            if str(s).lower() == low:
                raise ValueError(f"非法 {label}={value!r}：大小写不匹配，应为 {s!r}")
        hint = _spelling_hint(value, valid)
        raise ValueError(f"非法 {label}={value!r}{hint}；合法值：{sorted(valid)}")
    raise ValueError(f"非法 {label}={value!r}；合法值：{sorted(valid)}")


def validate_category(v):
    """校验 models.category（产品品类）。"""
    return _validate_enum(v, VALID_CATEGORIES, "models.category")


def validate_tier(v):
    """校验 models.tier（产品档位）。"""
    return _validate_enum(v, VALID_TIERS, "models.tier")


def validate_model_url_kind(v):
    """校验 models.model_url_kind（None 合法 = 尚未生成取证链接）。"""
    return _validate_enum(v, VALID_MODEL_URL_KINDS, "models.model_url_kind",
                          allow_none=True)


def validate_issue_status(v):
    """校验 maintenance_queue.status（待修工单状态）。"""
    return _validate_enum(v, VALID_ISSUE_STATUSES, "maintenance_queue.status")


def validate_recipe_mode(v):
    """校验 brands.recipe_mode（None 合法 = 未指定抓取配方）。"""
    return _validate_enum(v, VALID_RECIPE_MODES, "brands.recipe_mode",
                          allow_none=True)


def validate_rate_source(v):
    """校验 exchange_rates / price_snapshots 的 rate_source。

    None 合法 = 历史行未记录来源。
    """
    return _validate_enum(v, {RATE_SOURCE_STATIC}, "exchange_rates.rate_source",
                          allow_none=True, prefix_ok=(RATE_SOURCE_LIVE_PREFIX,))


def validate_snapshot_url_kind(v):
    """校验 price_snapshots.source_url_kind（None 合法 = 未标注链接粒度）。"""
    return _validate_enum(v, VALID_SNAPSHOT_URL_KINDS,
                          "price_snapshots.source_url_kind", allow_none=True)


def validate_norm_rule(v):
    """校验 parts.norm_rule（归一化路径）。None 合法 = 归一化整体失败的历史行。"""
    return _validate_enum(v, VALID_NORM_RULES, "parts.norm_rule", allow_none=True)


def validate_currency(v):
    """校验 countries.currency / price_snapshots.currency。

    ⚠️ 词汇表 = STATIC_RATES 的键（见其定义处），**不能**用于 exchange_rates.currency：
    后者由 open.er-api.com 的响应灌入，实测 166 种（接口返回什么就是什么），是开放词表。

    None 合法 = 未标注币种（历史行）。
    """
    return _validate_enum(v, VALID_CURRENCIES, "currency", allow_none=True)


SCHEMA = """
CREATE TABLE IF NOT EXISTS brands (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE,
  recipe_mode TEXT,
  note TEXT
);
CREATE TABLE IF NOT EXISTS countries (
  code TEXT PRIMARY KEY,
  name TEXT,
  currency TEXT,
  locale TEXT
);
CREATE TABLE IF NOT EXISTS models (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  brand_id INTEGER,
  country_code TEXT,
  name TEXT,
  model_key TEXT,
  source_url TEXT,
  discovered_at TEXT,
  category TEXT DEFAULT 'phone',  -- 产品品类，合法值见 VALID_CATEGORIES（写入口 validate_category）
                                  -- phone/tablet/watch/earbuds/wearable/other
                                  -- ⚠️ 跨品类比价无意义（平板屏幕 ≠ 手机屏幕），比价矩阵必须按品类分组
                                  -- 尚无 DB 级 CHECK：改错会在 COALESCE(...,'phone') 之外多出一个分组，
                                  -- 静默拆散矩阵，故由 tools/verify_quarterly_run.py 白名单巡检兜底
  tier TEXT,               -- 产品档位，合法值见 VALID_TIERS：旗舰/高端/中端/入门
                           -- （公平跨品牌比价的关键维度）
  base_model TEXT,         -- 归一化基础机型(去规格/颜色)，跨规格聚合比价键
  spec TEXT,               -- 规格/SKU(如 8GB+256GB)，同基础机型不同规格备件价可能不同
  -- 机型级取证链接（"一机一链"）：source_url 只到品牌入口页不足以举证，
  -- 下列字段记录『这一台机型』的实际链接、类型、定位方式与真实校验结果。
  model_url TEXT,          -- 机型级链接：点开即可核对该机型价格
  model_url_kind TEXT,     -- 取证链接类型，合法值见 VALID_MODEL_URL_KINDS（写入口校验）：
                           --   model_api / model_text_fragment / model_page /
                           --   category_api_locator / brand_entry
                           -- NULL 合法 = 尚未生成/未校验取证链接
  model_url_locator TEXT,  -- 机型在该链接内容中的定位（JSON 路径 / 机型 tag / 表行文本）
  model_url_verified INTEGER DEFAULT 0,  -- 1=已实际请求并确认命中该机型；0=未校验/未命中
  model_url_checked_at TEXT,             -- 校验时点(ISO)
  model_page_url TEXT,     -- 人可读的官网页面（机型页或含该机型的价表页）
  UNIQUE(brand_id, country_code, model_key)
);
CREATE TABLE IF NOT EXISTS parts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  model_id INTEGER,
  name TEXT,
  part_type TEXT,
  canonical_name TEXT,
  canonical_type TEXT,
  canonical_spec TEXT,
  variant TEXT,
  lang TEXT,
  norm_rule TEXT,
  norm_conf INTEGER,
  norm_version TEXT,
  UNIQUE(model_id, name)
);
CREATE TABLE IF NOT EXISTS part_alias (
  part_type      TEXT NOT NULL,
  alias_key      TEXT NOT NULL,
  canonical_name TEXT NOT NULL,
  canonical_type TEXT,
  lang           TEXT,
  source         TEXT,
  PRIMARY KEY (part_type, alias_key)
);
CREATE TABLE IF NOT EXISTS price_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  part_id INTEGER,
  quarter TEXT,
  price REAL,
  currency TEXT,
  cny_price REAL,
  material_fee REAL,
  labor_fee REAL,
  source_url TEXT,
  captured_at TEXT,
  tax_included INTEGER,
  -- 人工费取证（P1-1）：官网是否单列人工费 + 原文证据
  has_labor_split INTEGER,        -- 1=官网单列人工费并取证；0=官网仅给总价(无单独人工费)
  labor_note TEXT,                -- 人工费原文说明（如"官网《保外人工指导价（元）》列：40元"）；无则"官网未单列人工费，仅提供含人工的总维修价"
  labor_source_url TEXT,          -- 人工费证据来源页 URL
  -- 数据可信度（P1-2 / P0-2）
  is_seed INTEGER DEFAULT 0,      -- 1=演示/种子数据(不可作真实结论)；0=真实抓取
  rate_source TEXT,               -- 汇率来源，前缀模式（写入口 validate_rate_source）：
                                  --   "static" 或 "live:<endpoint>"；NULL = 历史行未记录
  rate_as_of TEXT,                -- 汇率时点(ISO)，用于折算可信度标注
  source_url_kind TEXT,           -- 该行 source_url 的粒度，合法值见 VALID_SNAPSHOT_URL_KINDS
                                  -- ⚠️ 与 models.model_url_kind 是两套词汇表（本列多 reference_cn），
                                  -- 刻意不共用集合（写入口 validate_snapshot_url_kind）
  -- 参考价（B 方案，2026-09-23）：本地官方无价时，借用同机型 CN 官方价填充，
  -- 必须明确标注、绝不参与本地价差放大。
  is_reference INTEGER DEFAULT 0, -- 1=参考价（非本地官方价）；0=本地官方抓取
  reference_region TEXT,          -- 参考价来源区域（如 'cn'）；本地官方价则为 NULL
  UNIQUE(part_id, quarter)
);
CREATE TABLE IF NOT EXISTS third_party_prices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  brand TEXT,
  part_type TEXT,
  quarter TEXT,
  ref_cny REAL,
  note TEXT,
  UNIQUE(brand, part_type, quarter)
);
CREATE TABLE IF NOT EXISTS exchange_rates (
  quarter TEXT,
  currency TEXT,
  rate_to_cny REAL,
  rate_source TEXT,               -- 汇率来源，同上（static 或 live:<endpoint>）
  rate_as_of TEXT,                -- 汇率快照时点(ISO)
  PRIMARY KEY(quarter, currency)
);
CREATE TABLE IF NOT EXISTS run_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  brand TEXT,
  country TEXT,
  quarter TEXT,
  started_at TEXT,
  finished_at TEXT,
  status TEXT,            -- 合法值见文件头 VALID_STATUSES（应用层枚举 + log_run 断言）
                          -- 尚无 DB 级 CHECK 约束：SQLite 不支持 ALTER TABLE ADD CONSTRAINT，
                          -- 待状态集合稳定后整表重建迁移；在此之前由
                          -- tools/verify_quarterly_run.py 的白名单巡检兜底
                          -- success      本轮抓取成功
                          -- partial      部分品类/机型成功
                          -- failed       真失败（含"自动发现 0 机型"的静默丢数据）
                          -- resumed      断点续跑：本季机型均已抓，本轮无新增（正常）
                          -- unavailable  官网不提供备件价 / KB 研判需真机代理（非我方缺口）
                          -- skipped      未收录（无 KB 记录）
  rows_written INTEGER,
  error_text TEXT,
  anomaly_flag INTEGER DEFAULT 0,
  anomaly_reason TEXT
);
CREATE TABLE IF NOT EXISTS maintenance_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  brand TEXT,
  country TEXT,
  detected_at TEXT,
  issue_summary TEXT,
  status TEXT DEFAULT 'open',   -- 待修工单状态，合法值见 VALID_ISSUE_STATUSES
                                -- （写入口常量 ISSUE_OPEN / ISSUE_RESOLVED / ISSUE_WONT_FIX）
                                -- open / resolved / wont_fix
  diagnosis TEXT,
  proposed_fix TEXT,
  resolved_at TEXT,
  resolved_by TEXT
);
"""

# 静态兜底汇率（仅当实时获取失败时用，单位：1 外币 = ? CNY）
STATIC_RATES = {
    "CNY": 1.0, "USD": 7.20, "EUR": 7.80, "JPY": 0.048, "AED": 1.96,
    "MYR": 1.55, "TRY": 0.21, "SGD": 5.30, "GBP": 9.10, "KRW": 0.0052, "MXN": 0.39,
}

# countries.currency / price_snapshots.currency 的合法集合同样**推导**而非手写：
# 一个币种若不在汇率表里，get_rate() 返回 None，而 run.py 的
# `cny = price * rate if rate else None` 会让 cny_price **静默为 NULL**——
# 该行随后从所有 CNY 口径的比价里消失，且全链路不报错。
# 因此「能折算」才是这个列的合法条件，直接绑定汇率表，新增国家时若币种无汇率
# 会在写入口 fail-fast（强迫显式补汇率），而不是悄悄产出无 CNY 的行。
VALID_CURRENCIES = frozenset(STATIC_RATES)


_WAL_ENSURED = False  # 进程级标记：journal_mode=WAL 是**库文件属性**，每进程设一次即可


def get_conn():
    global _WAL_ENSURED
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    # 并发容错：抓取进程与 API 服务同时访问同一库时，等待而非直接报 "database is locked"
    conn.execute("PRAGMA busy_timeout=15000")
    # journal_mode=WAL 持久化在数据库文件头里，一旦设置，后续连接自动继承。
    # 但**每建一次连接就执行一次该 PRAGMA 实测要 116ms**（对比裸 sqlite3.connect 仅 1.9ms，
    # busy_timeout 仅 1.4ms）——因为它要走一次"设置日志模式"的排他流程。
    # 平台里 get_conn 被调用成千上万次（每条价格快照一次），fetch_rates 写 166 个币种
    # 就白花 19s，故改为每进程只设一次（2026-09-17 优化，详见报告 §16.3）。
    if not _WAL_ENSURED:
        try:
            conn.execute("PRAGMA journal_mode=WAL")  # 读写并发，减少锁竞争
            _WAL_ENSURED = True
        except Exception:
            pass
    return conn


def _retry_exec(conn, sql, params=()):
    """执行单条写语句，遇到 'database is locked' 时退避重试，避免并发抓取时 init_db 迁移崩溃。"""
    import time
    for attempt in range(40):
        try:
            return conn.execute(sql, params)
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < 39:
                time.sleep(0.4)
                continue
            raise


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    # 兼容旧库：tier / base_model / spec / color / edition 列在后续版本才加入
    # category（品类）用于跨品类分组比价：全品类口径下必须与 tier 一样参与分组
    try:
        conn.execute("ALTER TABLE models ADD COLUMN category TEXT DEFAULT 'phone'")
    except Exception:
        pass
    for col in ("tier", "base_model", "spec", "color", "edition"):
        try:
            conn.execute(f"ALTER TABLE models ADD COLUMN {col} TEXT")
        except Exception:
            pass
    # 机型级取证链接字段（"一机一链"）
    for col, typ in (("model_url", "TEXT"), ("model_url_kind", "TEXT"),
                     ("model_url_locator", "TEXT"), ("model_url_verified", "INTEGER DEFAULT 0"),
                     ("model_url_checked_at", "TEXT"), ("model_page_url", "TEXT")):
        try:
            conn.execute(f"ALTER TABLE models ADD COLUMN {col} {typ}")
        except Exception:
            pass
    # 备件名归一化字段（方案 A）：原文 name/part_type 保留不动，归一化结果另存新列
    for col, typ in (("canonical_name", "TEXT"), ("canonical_type", "TEXT"),
                     ("canonical_spec", "TEXT"), ("variant", "TEXT"), ("lang", "TEXT"),
                     ("norm_rule", "TEXT"), ("norm_conf", "INTEGER"), ("norm_version", "TEXT")):
        try:
            conn.execute(f"ALTER TABLE parts ADD COLUMN {col} {typ}")
        except Exception:
            pass
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_parts_canon "
                     "ON parts(canonical_type, canonical_name, canonical_spec)")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE price_snapshots ADD COLUMN source_url_kind TEXT")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE price_snapshots ADD COLUMN tax_included INTEGER")
    except Exception:
        pass
    # P1-1 人工费取证字段（类型需与 SCHEMA CREATE 对齐）
    for col, typ in (("has_labor_split", "INTEGER"), ("is_seed", "INTEGER DEFAULT 0"),
                     ("labor_note", "TEXT"), ("labor_source_url", "TEXT"),
                     ("rate_source", "TEXT"), ("rate_as_of", "TEXT"),
                     ("is_reference", "INTEGER DEFAULT 0"),
                     ("reference_region", "TEXT")):
        try:
            conn.execute(f"ALTER TABLE price_snapshots ADD COLUMN {col} {typ}")
        except Exception:
            pass
    try:
        conn.execute("ALTER TABLE exchange_rates ADD COLUMN rate_source TEXT")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE exchange_rates ADD COLUMN rate_as_of TEXT")
    except Exception:
        pass
    # P1-3 重算 part_type：仅当存在未计算(NULL)备件时才跑（幂等 + 避免每次抓取
    # 启动都全量扫描 17k 行导致长时间持锁、与并发写入冲突报 database is locked）
    if conn.execute("SELECT COUNT(*) FROM parts WHERE part_type IS NULL").fetchone()[0] > 0:
        for r in conn.execute("SELECT id, name FROM parts WHERE part_type IS NULL"):
            _retry_exec(conn, "UPDATE parts SET part_type=? WHERE id=?",
                        (normalize_category(r["name"]), r["id"]))
        conn.commit()
    # 回填历史行：重新计算 base_model/spec/color/edition 全部维度（幂等，
    # 仅当存在未计算行时跑，避免锁竞争）
    if conn.execute("SELECT COUNT(*) FROM models WHERE base_model IS NULL").fetchone()[0] > 0:
        rows = conn.execute("SELECT id, name FROM models WHERE base_model IS NULL").fetchall()
        for r in rows:
            _retry_exec(conn,
                        "UPDATE models SET base_model=?, spec=?, color=?, edition=? WHERE id=?",
                        (normalize_base_model(r["name"]), extract_spec(r["name"]),
                         extract_color(r["name"]), extract_edition(r["name"]), r["id"]))
        conn.commit()
    conn.commit()
    conn.close()


# 产品档位归类（用于跨品牌公平比价：旗舰比旗舰、入门比入门）
def classify_tier(name):
    s = (name or "").lower()
    if any(k in s for k in ["ultra", "pro max", "fold", "mate", "find x", "magic", "rsr", "至臻", "pura", "保时捷"]):
        return "旗舰"
    if any(k in s for k in ["pro", "plus", "note", "max", "数字"]):
        return "高端"
    if any(k in s for k in ["redmi", "a 系列", "畅享", "青春", "play", "lite", "se", "neo", "y 系列"]):
        return "入门"
    return "中端"


# ---------------- 规格/SKU 维度（同一基础机型的不同规格备件价可能不同） ----------------
# 例："Xiaomi 13 Pro 8GB内存 陶瓷黑 128GB" -> base_model="Xiaomi 13 Pro", spec="8GB+128GB"
#     "Redmi Note 13 5G 8GB 256GB"        -> base_model="Redmi Note 13", spec="8GB+256GB"
# 目的：①把同产品的多个规格聚合到 base_model 便于选产品；②跨国比价时锁定"同一规格"，
#       避免把 A 国的 256GB 版与 B 国的 128GB 版混算导致比价失真。
_SPEC_TOKEN = re.compile(
    r"\d+\s?(?:GB|TB)(?:内存)?"                          # 128GB / 256 GB / 8GB内存
    r"(?:\s?[+]?\s?\d+\s?(?:GB|TB)(?:内存)?)?"          # 链式: 12GB+256GB / 8GB 256GB
    r"|\d+\s?RAM",                                      # 8GB RAM
    re.IGNORECASE)
# 营销版本(edition)后缀白名单：这些是同一手机产品线的"特别版/限定版"，剥离后
# 能让 base_model 收敛到产品线(便于跨国/跨版本公平比价)，同时单独保留 edition 维度。
# 注意：仅收录明确属于"同产品线变体"的版本词，避免误并真正独立的产品线
# （如 OPPO A1 活力版 是独立机型，不在此列；天玑版=芯片变体也不折叠）。
_EDITION_WORDS = ["兰博基尼版", "保时捷设计", "EVA限定版", "火星探索版", "超级闪充版",
                  "摄影师版", "星籁版", "新声版", "灵动版", "元气版", "典藏版",
                  "至臻版", "探索版", "限定版"]
# 按长度降序拼接，保证 "EVA限定版" 先于 "限定版" 命中（避免子串重复）
_EDITION_RE = re.compile(
    r"(?<![一-鿿])(?:" + "|".join(re.escape(e) for e in sorted(_EDITION_WORDS, key=len, reverse=True)) +
    r")(?![一-鿿])\s*$", re.IGNORECASE)

# 颜色词表（中英文）。颜色是 SKU 的一个维度：同机型同规格、不同配色备件价也可能不同
# （尤其后盖/边框），故需从机型名中剥离颜色，避免 base_model 因颜色分裂，同时单独保留
# 颜色维度用于公平比价。英文词用整词边界匹配，避免误伤 "Redmi" 等含色字的品牌名。
_COLOR_WORDS = ["陶瓷黑", "陶瓷白", "曜石黑", "远山蓝", "钛金灰", "暗夜紫", "松柏绿", "晴雪",
                "幻夜黑", "量子蓝", "秘银", "苍岭", "钛黑", "钛灰", "钛白", "钛蓝", "钛原", "钛青",
                "午夜黑", "午夜色", "星光色", "深空灰", "银色", "金色", "蓝色", "红色",
                "绿色", "紫色", "黑色", "白色", "粉色", "黄色", "青山黛", "丹霞橙",
                "Natural Titanium", "Titanium", "Ceramic Black", "Ceramic White", "Ceramic",
                "Phantom Black", "Phantom White", "Phantom", "Midnight", "Starlight",
                "Graphite", "Blue", "Black", "White", "Gold", "Silver", "Green", "Purple",
                "Pink", "Yellow", "Red", "Gray", "Grey"]
# 按长度降序拼接，保证 "Natural Titanium" 先于 "Titanium" 命中（避免子串重复）
_COLOR_RE = re.compile(
    r"(?<![a-z])(?:" + "|".join(re.escape(c) for c in sorted(_COLOR_WORDS, key=len, reverse=True)) + r")(?![a-z])",
    re.IGNORECASE)


def extract_spec(name):
    """从机型全名中提取规格/SKU 串（如 8GB+256GB）；无规格返回 None。"""
    s = (name or "").strip()
    specs = []
    for m in _SPEC_TOKEN.finditer(s):
        tok = re.sub(r"\s+", "", m.group(0))          # 去空格: "256 GB"->"256GB"
        tok = re.sub(r"内存$", "", tok)               # "8GB内存"->"8GB"
        if tok:
            specs.append(tok)
    if not specs:
        return None
    # 去重保序，用 + 连接（"8GB 256GB" -> "8GB+256GB"）
    return "+".join(dict.fromkeys(specs))


def extract_color(name):
    """从机型全名中提取颜色/配色（如 钛黑 / Natural Titanium）；无颜色返回 None。

    用整词边界匹配，避免把 "Redmi" 误判为颜色 "Red"。
    """
    s = (name or "").strip()
    if not s:
        return None
    found = []
    for c in sorted(_COLOR_WORDS, key=len, reverse=True):
        if re.search(r"(?<![a-z])" + re.escape(c) + r"(?![a-z])", s, re.IGNORECASE):
            if any(c.lower() in f.lower() for f in found):
                continue
            found.append(c)
    if not found:
        return None
    return " ".join(found)


def extract_edition(name):
    """从机型全名抽取营销版本(edition)后缀（如 火星探索版 / EVA限定版 / 兰博基尼版）。

    先剥离规格/颜色/5G，再匹配结尾的版本词；无版本返回 None。
    仅收录 _EDITION_WORDS 白名单内的"同产品线变体"，避免误并独立机型(如 A1 活力版)。
    """
    s = (name or "").strip()
    s = _SPEC_TOKEN.sub(" ", s)
    s = _COLOR_RE.sub(" ", s)
    s = re.sub(r"\s*5G\b", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    m = _EDITION_RE.search(s)
    return m.group(0).strip() if m else None


def normalize_base_model(name):
    """归一化基础机型名：去掉规格 token、颜色词、营销版本后缀，便于跨规格/跨颜色/跨版本聚合。"""
    s = (name or "").strip()
    s = _SPEC_TOKEN.sub(" ", s)
    s = _COLOR_RE.sub(" ", s)
    s = re.sub(r"\s*5G\b", " ", s)        # 去掉网络制式(含词尾 5G)
    s = _EDITION_RE.sub(" ", s)          # 折叠 edition 到独立维度，base_model 收敛到产品线
    s = re.sub(r"\s+", " ", s).strip(" -_")
    return s or (name or "").strip()


# ---- 产品品类（区别于"备件品类"）：phone / tablet / watch / earbuds / wearable / other ----
# 口径：**官方备件价表里出现的品类全收**（平板/手表/耳机/手环/戒指都要），
# 但跨品类比价无意义（"平板屏幕" ≠ "手机屏幕"），所以比价矩阵必须按 category 分组。
# 顺序敏感：tablet/watch 先判，避免 Galaxy Watch 被 \bTab\b 之类规则误伤。
# ⚠️ 不能写 \bWatch\b：Galaxy Watch4 的 h 与 4 都是词字符、中间无单词边界，会漏网。
_CATEGORY_RULES = [
    (re.compile(r"iPad|\bPad\b|\bTab\b|平板", re.I), "tablet"),
    (re.compile(r"Watch\d*|WATCH\d*|手表", re.I), "watch"),
    (re.compile(r"\bBuds\b|Earbuds|AirPods|\bEnco\b|\bTWS\b|耳机", re.I), "earbuds"),
    (re.compile(r"\bRing\b|\bFit\d*\b|\bBand\b|手环|穿戴", re.I), "wearable"),
    (re.compile(r"\bTV\b|电视|笔记本|\bMac\b|空调|冰箱|洗衣机", re.I), "other"),
]


def guess_category(name):
    """按机型名推断产品品类。抓链若能从源头确定品类应显式传，此函数只作兜底/回填。"""
    s = name or ""
    for pat, cat in _CATEGORY_RULES:
        if pat.search(s):
            return cat
    return "phone"



# 人工品类映射表（P1-3）：把抓取得到的本地化/品牌特定备件名映射到规范品类，
# 目标是把 normalize_category 落"其他"的比例从 ~30% 压到 <10%。
# 顺序敏感：前面的规则先命中。覆盖中/英/日/德/土多语，含三星等维修费用表的
# 本地化"损伤类"标签（如 画面の損傷=屏幕损伤、バッテリー修理=电池维修、Rückglasschaden=后盖损伤）。
_MANUAL_CATEGORY_MAP = [
    (re.compile(r"屏幕|画面|screen|display|ekran|ディスプレイ|画面の損傷|画面および背面", re.I), "屏幕"),
    (re.compile(r"后盖|背板|后壳|back\s*cover|back\s*glass|arka\s*kapak|背面ガラス|rückglas|rückglasschaden", re.I), "后盖"),
    (re.compile(r"电池|蓄电|battery|pil|バッテリー|バッテリー修理|batterieservice", re.I), "电池"),
    (re.compile(r"主板|逻辑板|board|mainboard|anakart|メイン基板|ロジック", re.I), "主板"),
    (re.compile(r"摄像|相机|镜头|camera|lens|kamera|カメラ|背面カメラ|rückkamera|schaden an der rückkamera|后摄", re.I), "摄像头"),
    (re.compile(r"充电口|接口|尾插|usb|type-c|充電コネクタ", re.I), "充电口"),
    (re.compile(r"扬声器|speaker|hoparlör|スピーカー", re.I), "扬声器"),
    (re.compile(r"受话器|听筒|earpiece|受話器", re.I), "听筒"),
    (re.compile(r"马达|振动|vibration|バイブ|titreşim|昇降|升降", re.I), "振动马达"),
    (re.compile(r"中框|框架|frame|フレーム|çerçeve|滑动", re.I), "中框"),
    (re.compile(r"指纹|fingerprint|parmak", re.I), "指纹"),
    (re.compile(r"侧键|电源键|音量键|音量|button|ボタン|tuş", re.I), "侧键"),
    (re.compile(r"麦克风|mic|microphone|マイク|mikrofon", re.I), "麦克风"),
    (re.compile(r"数据线|cable|ケーブル|kablo", re.I), "数据线"),
    (re.compile(r"耳机|earphone|イヤホン|kulaklık|左耳|右耳", re.I), "耳机"),
    (re.compile(r"适配器|adapter|充電器|アダプター|şarj cihaz|充电头|闪充", re.I), "电源适配器"),
    (re.compile(r"电源线|power cord|電源コード", re.I), "电源线"),
    (re.compile(r"遥控器|remote|リモコン", re.I), "遥控器"),
    (re.compile(r"壁挂|wall mount", re.I), "壁挂"),
    (re.compile(r"底座|スタンド|stand", re.I), "底座"),
    (re.compile(r"表盘|文字盤|watch face", re.I), "表盘"),
    (re.compile(r"表带|腕带|バンド|band|kayış", re.I), "表带"),
    (re.compile(r"手写笔|笔尖|stylus|スタイラス", re.I), "手写笔"),
    (re.compile(r"护眼膜|贴膜|film|フィルム", re.I), "贴膜"),
    (re.compile(r"键盘|keyboard|キーボード", re.I), "键盘"),
    (re.compile(r"转接线|av输入", re.I), "转接线"),
    (re.compile(r"眼镜|メガネ|gözlük", re.I), "眼镜"),
]

# 备件品类词汇表：**从上面的映射表推导**，而不是手抄一份。
# 这样往 _MANUAL_CATEGORY_MAP 加一条规则时，白名单自动跟着扩展，永不失配。
# （手抄的副本会在"加了正则但忘了改常量"时静默失配——正是本类加固要消灭的问题。）
# 注：normalize_category() 末尾的通用关键词兜底全部返回映射表里已有的标签，故无需另计。
VALID_PART_TYPES = frozenset({lab for _, lab in _MANUAL_CATEGORY_MAP}) | {PART_TYPE_OTHER}


def validate_part_type(v):
    """校验备件品类（parts.part_type / parts.canonical_type 等）。

    None 非法：upsert_part 里 `part_type or normalize_category(name)` 保证非空。
    """
    return _validate_enum(v, VALID_PART_TYPES, "parts.part_type")


# parts.lang / part_alias.lang —— 备件原文语种。词汇表**封闭**：
# 由 crawler/part_norm._detect_lang() 唯一产生（该函数只有 5 个 return 分支）。
# 新增语种 = 显式修改那个函数，属有意识的决定，故此处断言语种漂移是安全的。
# 回归测试 test_enum_vocabularies 会核对本集合与 _detect_lang 的分支一一对应。
VALID_LANGS = frozenset({"zh", "en", "ja", "de", "tr"})


def validate_lang(v):
    """校验 parts.lang / part_alias.lang（备件原文语种）。"""
    return _validate_enum(v, VALID_LANGS, "lang")


def normalize_category(name):
    """备件名 -> 归一化品类（跨品牌/跨型号对齐比价行的关键）。

    优先命中人工映射表（含多语/品牌特定/本地化损伤标签），再回退通用关键词，
    最后落"其他"。人工映射表是 P1-3 把"其他"压到 <10% 的核心。
    """
    s = (name or "").lower()
    for rx, lab in _MANUAL_CATEGORY_MAP:
        if rx.search(s):
            return lab
    if any(k in s for k in ["屏", "display", "screen"]):
        return "屏幕"
    if any(k in s for k in ["电池", "蓄电", "battery"]):
        return "电池"
    if any(k in s for k in ["主板", "逻辑板", "board", "mainboard"]):
        return "主板"
    if any(k in s for k in ["后盖", "背板", "后壳", "back"]):
        return "后盖"
    if any(k in s for k in ["摄像", "相机", "镜头", "camera", "lens"]):
        return "摄像头"
    if any(k in s for k in ["充电", "接口", "尾插", "usb", "type-c"]):
        return "充电口"
    return "其他"


def this_quarter(dt=None):
    dt = dt or datetime.now()
    q = (dt.month - 1) // 3 + 1
    return f"{dt.year}Q{q}"


def upsert_brand(name, recipe_mode=None, note="", conn=None):
    # 枚举列写入口校验：配方类型来自 KB 的 query.mode，写错会让 _job_needs_browser
    # 误判该品牌是否需要浏览器（进而影响并发池划分），故在此拦下。
    recipe_mode = validate_recipe_mode(recipe_mode)
    own = conn is None
    c = conn or get_conn()
    cur = c.execute("INSERT INTO brands(name, recipe_mode, note) VALUES(?,?,?) "
                    "ON CONFLICT(name) DO UPDATE SET recipe_mode=excluded.recipe_mode",
                    (name, recipe_mode, note))
    bid = c.execute("SELECT id FROM brands WHERE name=?", (name,)).fetchone()["id"]
    if own:
        c.commit(); c.close()
    return bid


def upsert_country(code, name="", currency="", locale="", conn=None):
    # 枚举列写入口校验：币种写错会让 get_rate() 返回 None，
    # 进而 run.py 的 `cny = price * rate if rate else None` 静默产出无 CNY 价的快照行
    # （该行从此在所有 CNY 口径的比价里消失，且全链路不报错）。
    currency = validate_currency(currency)
    own = conn is None
    c = conn or get_conn()
    c.execute("INSERT INTO countries(code, name, currency, locale) VALUES(?,?,?,?) "
              "ON CONFLICT(code) DO UPDATE SET currency=excluded.currency, locale=excluded.locale",
              (code, name, currency, locale))
    if own:
        c.commit(); c.close()


def upsert_model(brand_id, country_code, name, model_key=None, source_url="", tier=None,
                base_model=None, spec=None, color=None, edition=None, conn=None,
                model_url=None, model_url_kind=None, model_url_locator=None,
                model_url_verified=None, model_url_checked_at=None, model_page_url=None,
                category=None):
    """写入/更新机型。

    机型级取证链接（model_url*）为可选参数：传 None 时保留库内既有值（COALESCE），
    因此抓取链路可以先落价格、再由校验器单独回填/更新链接，互不覆盖。

    category（品类）同理：抓取链路若能从源头确定品类就显式传，传 None 时保留库内既有值。
    """
    model_key = model_key or name
    if category is None:
        category = guess_category(name)
    if base_model is None:
        base_model = normalize_base_model(name)
    if spec is None:
        spec = extract_spec(name)
    if color is None:
        color = extract_color(name)
    if edition is None:
        edition = extract_edition(name)
    if tier is None:
        tier = classify_tier(name)
    # 枚举列写入口校验（fail fast）：品类写错会静默拆散比价矩阵分组，
    # 档位写错会在"同档位对标"里凭空多出一个选项，故一律在此拦下。
    category = validate_category(category)
    tier = validate_tier(tier)
    model_url_kind = validate_model_url_kind(model_url_kind)
    own = conn is None
    c = conn or get_conn()
    c.execute("""INSERT INTO models(brand_id, country_code, name, model_key, source_url, discovered_at,
                    category, tier, base_model, spec, color, edition,
                    model_url, model_url_kind, model_url_locator, model_url_verified,
                    model_url_checked_at, model_page_url)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(brand_id, country_code, model_key)
                   DO UPDATE SET tier=excluded.tier, source_url=excluded.source_url,
                                 base_model=excluded.base_model, spec=excluded.spec,
                                 color=excluded.color, edition=excluded.edition,
                                 category=COALESCE(excluded.category, models.category),
                                 model_url=COALESCE(excluded.model_url, models.model_url),
                                 model_url_kind=COALESCE(excluded.model_url_kind, models.model_url_kind),
                                 model_url_locator=COALESCE(excluded.model_url_locator, models.model_url_locator),
                                 model_url_verified=COALESCE(excluded.model_url_verified, models.model_url_verified),
                                 model_url_checked_at=COALESCE(excluded.model_url_checked_at, models.model_url_checked_at),
                                 model_page_url=COALESCE(excluded.model_page_url, models.model_page_url)""",
                 (brand_id, country_code, name, model_key, source_url,
                  datetime.now().isoformat(timespec="seconds"), category, tier, base_model, spec, color,
                  edition,
                  model_url, model_url_kind, model_url_locator, model_url_verified,
                  model_url_checked_at, model_page_url))
    row = c.execute("SELECT id FROM models WHERE brand_id=? AND country_code=? AND model_key=?",
                    (brand_id, country_code, model_key)).fetchone()
    if own:
        c.commit(); c.close()
    return row["id"]


def upsert_part(model_id, name, part_type=None, conn=None):
    """写入/更新备件。name/part_type 保留官网原文；canonical_* 为归一化结果（方案 A）。

    归一化在此统一完成，因此 run.py / samsung_api.py / seed_demo.py / refetch_failing.py
    等所有写入路径自动生效，无需各自处理。
    """
    part_type = part_type or normalize_category(name)
    # 枚举列写入口校验：品类是比价聚合的分组键，写错会静默拆散矩阵（详见 VALID_PART_TYPES）
    part_type = validate_part_type(part_type)
    try:
        from crawler import part_norm as _pn
        nr = _pn.normalize(name or "", part_type)
        canonical_type = nr.canonical_type or part_type
        canon = (nr.canonical, validate_part_type(canonical_type), nr.spec,
                 nr.variant, validate_lang(nr.lang), validate_norm_rule(nr.rule),
                 nr.conf, "part_alias.json@v" + _pn.alias_version())
    except ValueError:
        raise                      # 枚举断言必须向上抛，不能被下面的兜底吞掉
    except Exception:
        canon = (name or "", part_type, "", "", "", NORM_RULE_FALLBACK, 0, None)
    own = conn is None
    c = conn or get_conn()
    c.execute(
        "INSERT INTO parts(model_id, name, part_type, canonical_name, canonical_type,"
        " canonical_spec, variant, lang, norm_rule, norm_conf, norm_version)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(model_id, name) DO UPDATE SET part_type=excluded.part_type,"
        " canonical_name=excluded.canonical_name, canonical_type=excluded.canonical_type,"
        " canonical_spec=excluded.canonical_spec, variant=excluded.variant, lang=excluded.lang,"
        " norm_rule=excluded.norm_rule, norm_conf=excluded.norm_conf,"
        " norm_version=excluded.norm_version",
        (model_id, name, part_type) + canon)
    row = c.execute("SELECT id FROM parts WHERE model_id=? AND name=?", (model_id, name)).fetchone()
    if own:
        c.commit(); c.close()
    return row["id"]


def model_already_captured(brand_id, country_code, model_key, quarter):
    conn = get_conn()
    row = conn.execute(
        """SELECT 1 FROM price_snapshots ps
           JOIN parts p ON p.id=ps.part_id
           JOIN models m ON m.id=p.model_id
           WHERE m.brand_id=? AND m.country_code=? AND m.model_key=? AND ps.quarter=? LIMIT 1""",
        (brand_id, country_code, model_key, quarter)).fetchone()
    conn.close()
    return row is not None


def captured_model_keys(brand_id, country_code, quarter):
    """本季已抓到价的机型集合（model_key），供抓取前**批量跳过**用。

    与 model_already_captured 的区别：那个是一台一次查询，OPPO 全量 275 台会打 275 次；
    这里是单次查询返回集合，让 crawl 流程能在**发请求之前**就过滤掉已抓机型——
    断点续跑原本只跳过"写库"，仍会对每台已抓机型重复 POST 取价（275 台约 5 分钟白跑）。
    注意：官方无备件价（partPriceList 为空）的机型不入库，故不在本集合内，
    重跑时仍会再取一次（属预期，宁多一次请求也不误判为"已抓"）。

    参考价（is_reference=1）必须排除：它不是本地官方价，只是借用他国同机型的价。
    若把它算作"已抓到价"，那些'仅因参考价而存在行'的机型会被永久跳过，再也拿不到
    真实本地价 —— 2026-09-23 de 重抓即因此漏掉 20 台（参考价机型与国际机型同名所致）。
    """
    conn = get_conn()
    rows = conn.execute(
        """SELECT DISTINCT m.model_key FROM price_snapshots ps
           JOIN parts p ON p.id=ps.part_id
           JOIN models m ON m.id=p.model_id
           WHERE m.brand_id=? AND m.country_code=? AND ps.quarter=?
             AND COALESCE(ps.is_reference, 0)=0""",
        (brand_id, country_code, quarter)).fetchall()
    conn.close()
    return {r["model_key"] for r in rows if r["model_key"]}


def insert_snapshot(part_id, quarter, price, currency, cny_price,
                    material_fee=None, labor_fee=None, source_url="", captured_at=None, tax_included=None,
                    labor_note=None, labor_source_url=None, has_labor_split=None,
                    is_seed=0, rate_source=None, rate_as_of=None, conn=None,
                    source_url_kind=None, is_reference=0, reference_region=None):
    """写入一条备件价格快照。

    labor_note / labor_source_url / has_labor_split：人工费取证（P1-1）。
      - 官网单列人工费(has_labor_split=1)：labor_fee 填金额，labor_note 写官网原文说明，labor_source_url 写证据页。
      - 官网仅给总价(has_labor_split=0)：labor_fee 留 NULL，labor_note 明确写"官网未单列人工费，仅提供含人工的总维修价"，绝不编造。
    is_seed：1=演示/种子数据；0=真实抓取（P1-2）。
    rate_source / rate_as_of：折算所用汇率来源与时点（P0-2）。
    source_url_kind：source_url 的粒度（model_api / model_text_fragment / model_page /
      category_api_locator / brand_entry / reference_cn）。前端据此如实标注"这条价格的链接是否精确到本机型"。
    is_reference / reference_region（B 方案，2026-09-23）：is_reference=1 表示这不是本地官方价，
      而是借用 reference_region（如 'cn'）同机型的官方价作参考。前端必须显式标注、
      且**不参与本地价差归因**（其 cny_price 等于参考区原值，天然不会制造假价差）。
    """
    # 枚举列写入口校验：链接粒度写错会让前端"这条价格是否精确到本机型"的标注说谎；
    # 汇率来源写错会让折算可信度标注失真。
    rate_source = validate_rate_source(rate_source)
    source_url_kind = validate_snapshot_url_kind(source_url_kind)
    # 币种写错 → get_rate() 返回 None → cny_price 静默为 None（见 upsert_country 注释）
    currency = validate_currency(currency)
    own = conn is None
    c = conn or get_conn()
    c.execute("""INSERT INTO price_snapshots(part_id, quarter, price, currency, cny_price,
                    material_fee, labor_fee, source_url, captured_at, tax_included,
                    has_labor_split, labor_note, labor_source_url, is_seed, rate_source, rate_as_of,
                    source_url_kind, is_reference, reference_region)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(part_id, quarter) DO UPDATE SET
                     price=excluded.price, currency=excluded.currency, cny_price=excluded.cny_price,
                     material_fee=excluded.material_fee, labor_fee=excluded.labor_fee,
                     source_url=excluded.source_url, captured_at=excluded.captured_at,
                     tax_included=excluded.tax_included,
                     has_labor_split=excluded.has_labor_split, labor_note=excluded.labor_note,
                     labor_source_url=excluded.labor_source_url, is_seed=excluded.is_seed,
                     rate_source=excluded.rate_source, rate_as_of=excluded.rate_as_of,
                     source_url_kind=excluded.source_url_kind,
                     is_reference=excluded.is_reference, reference_region=excluded.reference_region""",
                 (part_id, quarter, price, currency, cny_price, material_fee, labor_fee,
                  source_url, captured_at or datetime.now().isoformat(timespec="seconds"), tax_included,
                  has_labor_split, labor_note, labor_source_url, is_seed, rate_source, rate_as_of,
                  source_url_kind, int(is_reference or 0), reference_region))
    if own:
        c.commit(); c.close()


def upsert_third_party(brand, part_type, quarter, ref_cny, note="", conn=None):
    own = conn is None
    c = conn or get_conn()
    c.execute("""INSERT INTO third_party_prices(brand, part_type, quarter, ref_cny, note)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(brand, part_type, quarter) DO UPDATE SET
                     ref_cny=excluded.ref_cny, note=excluded.note""",
                 (brand, part_type, quarter, ref_cny, note))
    if own:
        c.commit(); c.close()


def set_rate(quarter, currency, rate, source="static", as_of=None):
    source = validate_rate_source(source)
    conn = get_conn()
    conn.execute("""INSERT INTO exchange_rates(quarter, currency, rate_to_cny, rate_source, rate_as_of)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(quarter, currency) DO UPDATE SET
                     rate_to_cny=excluded.rate_to_cny, rate_source=excluded.rate_source, rate_as_of=excluded.rate_as_of""",
                 (quarter, currency, rate, source, as_of))
    conn.commit(); conn.close()


def get_rate(quarter, currency):
    if currency == "CNY":
        return 1.0
    conn = get_conn()
    row = conn.execute("SELECT rate_to_cny FROM exchange_rates WHERE quarter=? AND currency=?",
                       (quarter, currency)).fetchone()
    conn.close()
    if row:
        return row["rate_to_cny"]
    return STATIC_RATES.get(currency)


def get_rate_meta(quarter, currency):
    """返回 (rate_to_cny, source, as_of)。用于前端折算可信度标注（P0-2）。"""
    if currency == "CNY":
        return 1.0, "static", None
    conn = get_conn()
    row = conn.execute("SELECT rate_to_cny, rate_source, rate_as_of FROM exchange_rates WHERE quarter=? AND currency=?",
                       (quarter, currency)).fetchone()
    conn.close()
    if row and row["rate_to_cny"] is not None:
        return row["rate_to_cny"], (row["rate_source"] or "static"), row["rate_as_of"]
    return STATIC_RATES.get(currency), "static", None


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def fetch_rates(quarter):
    """尽力拉取实时汇率（open.er-api.com，本机已验证可达），失败回退 STATIC_RATES。
    返回 {currency: rate_to_cny}，并把来源/时点写入 exchange_rates 供前端标注。
    rate_to_cny 定义：1 外币 = ? CNY。
    """
    import json
    import urllib.request
    as_of = _now_iso()
    live = {}
    src = RATE_SOURCE_STATIC
    try:
        with urllib.request.urlopen("https://open.er-api.com/v6/latest/USD", timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        rates = (data.get("rates") or {})
        usd_cny = rates.get("CNY")
        if usd_cny:
            for cur, per_usd in rates.items():
                if not per_usd:
                    continue
                live[cur] = 1.0 if cur == "CNY" else usd_cny / per_usd
            src = RATE_SOURCE_LIVE_PREFIX + "open.er-api.com"
    except Exception as e:
        sys.stderr.write(f"[warn] 实时汇率获取失败，回退 STATIC_RATES: {e}\n")
    # 合并静态兜底（实时未覆盖的币种）
    for cur, rt in STATIC_RATES.items():
        live.setdefault(cur, rt)
    src = validate_rate_source(src)
    # 批量落库：旧实现对每个币种各调一次 set_rate（各建一次连接 + 各跑一次
    # PRAGMA journal_mode=WAL ≈116ms），166 个币种就要 ~19s；这里单连接单事务
    # executemany 一次写完（实测 <0.1s）。落库失败不阻断抓取——get_rate 会回退静态汇率。
    try:
        conn = get_conn()
        conn.executemany(
            """INSERT INTO exchange_rates(quarter, currency, rate_to_cny, rate_source, rate_as_of)
               VALUES(?,?,?,?,?)
               ON CONFLICT(quarter, currency) DO UPDATE SET
                 rate_to_cny=excluded.rate_to_cny,
                 rate_source=excluded.rate_source, rate_as_of=excluded.rate_as_of""",
            [(quarter, cur, rt, src, as_of) for cur, rt in live.items()])
        conn.commit()
        conn.close()
    except Exception as e:
        sys.stderr.write(f"[warn] 汇率落库失败（不影响抓取，get_rate 回退 STATIC_RATES）: {e}\n")
    return live


def rows_to_dict(rs):
    return [dict(r) for r in rs]


def log_run(brand, country, quarter, started_at, finished_at, status,
            rows_written, error_text="", anomaly_flag=0, anomaly_reason="", conn=None):
    """每次品牌×国别抓取结束写一条运行日志（监控层判定成功与否的依据）。

    status 必须是 VALID_STATUSES 之一，否则抛 ValueError（见文件头说明）。
    这里做应用层软约束：写入口唯一，故一处校验即覆盖全部调用方。
    """
    status = validate_status(status)
    own = conn is None
    c = conn or get_conn()
    c.execute("""INSERT INTO run_logs(brand,country,quarter,started_at,finished_at,
                    status,rows_written,error_text,anomaly_flag,anomaly_reason)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                 (brand, country, quarter, started_at, finished_at, status,
                  rows_written, error_text, anomaly_flag, anomaly_reason))
    if own:
        c.commit(); c.close()


def add_issue(brand, country, issue_summary, conn=None):
    """把异常写入待修队列；同 brand/country 已有 open 项则去重跳过。返回 issue id 或 None。"""
    own = conn is None
    c = conn or get_conn()
    ex = c.execute("SELECT 1 FROM maintenance_queue WHERE brand=? AND country=? AND status=?",
                   (brand, country, ISSUE_OPEN)).fetchone()
    if ex:
        if own:
            c.close()
        return None
    cur = c.execute(
        "INSERT INTO maintenance_queue(brand,country,detected_at,issue_summary,status) VALUES(?,?,?,?,?)",
        (brand, country, datetime.now().isoformat(timespec="seconds"), issue_summary,
         ISSUE_OPEN))
    iid = cur.lastrowid
    if own:
        c.commit(); c.close()
    return iid


def open_issues():
    conn = get_conn()
    out = rows_to_dict(conn.execute(
        "SELECT * FROM maintenance_queue WHERE status=? ORDER BY detected_at DESC",
        (ISSUE_OPEN,)))
    conn.close(); return out


def resolve_issue(issue_id, diagnosis="", proposed_fix="", resolved_by="agent"):
    conn = get_conn()
    conn.execute(
        """UPDATE maintenance_queue SET status=?, diagnosis=?, proposed_fix=?,
                  resolved_at=?, resolved_by=? WHERE id=?""",
        (ISSUE_RESOLVED, diagnosis, proposed_fix,
         datetime.now().isoformat(timespec="seconds"), resolved_by, issue_id))
    conn.commit(); conn.close()


if __name__ == "__main__":
    init_db()
    print("DB initialized at", DB_PATH)
