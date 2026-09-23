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
  category TEXT DEFAULT 'phone',  -- 产品品类：phone/tablet/watch/earbuds/wearable/other
                                  -- ⚠️ 跨品类比价无意义（平板屏幕 ≠ 手机屏幕），比价矩阵必须按品类分组
  tier TEXT,               -- 产品档位：旗舰/高端/中端/入门（公平跨品牌比价的关键维度）
  base_model TEXT,         -- 归一化基础机型(去规格/颜色)，跨规格聚合比价键
  spec TEXT,               -- 规格/SKU(如 8GB+256GB)，同基础机型不同规格备件价可能不同
  -- 机型级取证链接（"一机一链"）：source_url 只到品牌入口页不足以举证，
  -- 下列字段记录『这一台机型』的实际链接、类型、定位方式与真实校验结果。
  model_url TEXT,          -- 机型级链接：点开即可核对该机型价格
  model_url_kind TEXT,     -- model_api / model_text_fragment / model_page / category_api_locator / brand_entry
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
  rate_source TEXT,               -- 汇率来源：live:<endpoint> / static(legacy) / static
  rate_as_of TEXT,                -- 汇率时点(ISO)，用于折算可信度标注
  source_url_kind TEXT,           -- 该行 source_url 的粒度：见 models.model_url_kind
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
  rate_source TEXT,               -- live:<endpoint> / static
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
  status TEXT,            -- success / failed / partial / skipped
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
  status TEXT DEFAULT 'open',   -- open / resolved / wont_fix
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
    try:
        from crawler import part_norm as _pn
        nr = _pn.normalize(name or "", part_type)
        canon = (nr.canonical, nr.canonical_type or part_type or "", nr.spec,
                 nr.variant, nr.lang, nr.rule, nr.conf, "part_alias.json@v" + _pn.alias_version())
    except Exception:
        canon = (name or "", part_type or "", "", "", "", "fallback", 0, None)
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
    """
    conn = get_conn()
    rows = conn.execute(
        """SELECT DISTINCT m.model_key FROM price_snapshots ps
           JOIN parts p ON p.id=ps.part_id
           JOIN models m ON m.id=p.model_id
           WHERE m.brand_id=? AND m.country_code=? AND ps.quarter=?""",
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
    src = "static"
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
            src = "live:open.er-api.com"
    except Exception as e:
        sys.stderr.write(f"[warn] 实时汇率获取失败，回退 STATIC_RATES: {e}\n")
    # 合并静态兜底（实时未覆盖的币种）
    for cur, rt in STATIC_RATES.items():
        live.setdefault(cur, rt)
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
    """每次品牌×国别抓取结束写一条运行日志（监控层判定成功与否的依据）。"""
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
    ex = c.execute("SELECT 1 FROM maintenance_queue WHERE brand=? AND country=? AND status='open'",
                   (brand, country)).fetchone()
    if ex:
        if own:
            c.close()
        return None
    cur = c.execute(
        "INSERT INTO maintenance_queue(brand,country,detected_at,issue_summary,status) VALUES(?,?,?,?, 'open')",
        (brand, country, datetime.now().isoformat(timespec="seconds"), issue_summary))
    iid = cur.lastrowid
    if own:
        c.commit(); c.close()
    return iid


def open_issues():
    conn = get_conn()
    out = rows_to_dict(conn.execute(
        "SELECT * FROM maintenance_queue WHERE status='open' ORDER BY detected_at DESC"))
    conn.close(); return out


def resolve_issue(issue_id, diagnosis="", proposed_fix="", resolved_by="agent"):
    conn = get_conn()
    conn.execute(
        """UPDATE maintenance_queue SET status='resolved', diagnosis=?, proposed_fix=?,
                  resolved_at=?, resolved_by=? WHERE id=?""",
        (diagnosis, proposed_fix, datetime.now().isoformat(timespec="seconds"), resolved_by, issue_id))
    conn.commit(); conn.close()


if __name__ == "__main__":
    init_db()
    print("DB initialized at", DB_PATH)
