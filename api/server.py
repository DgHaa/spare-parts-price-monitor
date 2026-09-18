"""api/server.py - 备件价格监控中台后端（Python 标准库 http.server，零依赖）。

提供 JSON API 给前端调用，并直接托管 web/ 静态前端（无需 npm build）：
  GET /api/overview     总览 KPI + 品牌×国家覆盖矩阵 + 健康汇总
  GET /api/brands       品牌列表
  GET /api/quarters     已有快照的季度列表
  GET /api/health       每个 brand/country 最新运行状态 + 待修项
  GET /api/anomalies    当前 open 的待修队列（自愈 Agent 工作来源）
  GET /api/runs         运行日志历史
  GET /api/list         机型/备件清单浏览(可 brand/country 过滤)
  GET /api/matrix       某季度比价矩阵(扁平行，前端自行透视)
  GET /api/alerts       异动告警(环比上季)
  GET /api/models       某 brand/country 下的机型列表(筛选用)
  GET /api/parts        某机型下的备件列表(筛选用)
  GET /api/part_series  单备件跨季度价格走势(按 brand/country/model/part 定位)

运行：python api/server.py   (默认 :8000)
"""
import json
import sqlite3
import sys
import os
import threading
import subprocess
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import db

WEB_ROOT = ROOT / "web"
# 端口：默认 8000；可用环境变量覆盖（PORT=8010 python api/server.py），
# 或用命令行覆盖（python api/server.py --port 8010）。命令行优先于环境变量。
PORT = int(os.environ.get("PORT", "8000"))
for _i, _a in enumerate(sys.argv[1:]):
    if _a == "--port" and _i + 1 < len(sys.argv[1:]):
        PORT = int(sys.argv[1:][_i + 1])
    elif _a.startswith("--port="):
        PORT = int(_a.split("=", 1)[1])


def conn():
    c = sqlite3.connect(str(db.DB_PATH))
    c.row_factory = sqlite3.Row
    return c


def rows_to_dict(rs):
    return [dict(r) for r in rs]


def api_countries():
    c = conn()
    out = rows_to_dict(c.execute("SELECT code, name, currency FROM countries ORDER BY code"))
    c.close()
    return out


def api_brands():
    c = conn()
    out = rows_to_dict(c.execute("SELECT id,name,recipe_mode,price_caveat FROM brands ORDER BY name"))
    c.close()
    return out


def api_list(brand=None, country=None):
    c = conn()
    q = """SELECT b.name brand, m.country_code country, m.name model,
                  p.id part_id, p.name part, ps.quarter, ps.price, ps.currency, ps.cny_price,
                  ps.material_fee, ps.labor_fee, ps.has_labor_split, ps.labor_note,
                  ps.labor_source_url, ps.is_seed, ps.rate_source, ps.rate_as_of
           FROM models m
           JOIN brands b ON b.id=m.brand_id
           JOIN parts p ON p.model_id=m.id
           JOIN price_snapshots ps ON ps.part_id=p.id
           WHERE 1=1"""
    args = []
    if brand:
        q += " AND b.name=?"; args.append(brand)
    if country:
        q += " AND m.country_code=?"; args.append(country)
    q += " ORDER BY b.name, m.country_code, m.name, p.name, ps.quarter"
    out = rows_to_dict(c.execute(q, args))
    c.close()
    return out


def api_matrix(quarter=None, category=None):
    """全量价行（供外部取数/导出）。

    2026-09-18：带出 `category`（产品品类）并支持按它过滤。全品类口径下，
    「屏幕」在手机与平板上都存在，取数方必须自行限定品类，否则会跨品类混算。
    """
    c = conn()
    if not quarter:
        quarter = db.this_quarter()
    q = """SELECT b.name brand, m.country_code country, m.name model,
                  COALESCE(m.category,'phone') category,
                  p.name part, ps.price, ps.currency, ps.cny_price
           FROM price_snapshots ps
           JOIN parts p ON p.id=ps.part_id
           JOIN models m ON m.id=p.model_id
           JOIN brands b ON b.id=m.brand_id
           WHERE ps.quarter=?"""
    args = [quarter]
    if category:
        q += " AND COALESCE(m.category,'phone')=?"; args.append(category)
    q += " ORDER BY b.name, m.country_code, m.name, p.name"
    out = rows_to_dict(c.execute(q, args))
    c.close()
    return {"quarter": quarter, "category": category, "rows": out}


def api_alerts(quarter=None):
    c = conn()
    if not quarter:
        quarter = db.this_quarter()
    y, qq = int(quarter[:4]), int(quarter[-1])
    pq = f"{y-1}Q4" if qq == 1 else f"{y}Q{qq-1}"
    q = """SELECT b.name brand, m.country_code country, m.name model, p.name part,
                  cur.price price, prev.price prev_price, cur.currency,
                  CASE WHEN prev.price THEN (cur.price-prev.price)/prev.price ELSE NULL END AS change_pct
           FROM price_snapshots cur
           JOIN price_snapshots prev ON prev.part_id=cur.part_id AND prev.quarter=?
           JOIN parts p ON p.id=cur.part_id
           JOIN models m ON m.id=p.model_id
           JOIN brands b ON b.id=m.brand_id
           WHERE cur.quarter=?"""
    out = rows_to_dict(c.execute(q, (pq, quarter)))
    c.close()
    return {"quarter": quarter, "prev_quarter": pq, "rows": out}


def api_quarters():
    c = conn()
    out = [r["quarter"] for r in c.execute(
        "SELECT DISTINCT quarter FROM price_snapshots ORDER BY quarter")]
    c.close()
    return out


def api_health():
    c = conn()
    q = """SELECT r.brand, r.country, r.quarter, r.status, r.rows_written,
                  r.anomaly_flag, r.anomaly_reason, r.finished_at,
                  (SELECT COUNT(*) FROM maintenance_queue q
                   WHERE q.brand=r.brand AND q.country=r.country AND q.status='open') AS open_issues
           FROM run_logs r
           WHERE r.id IN (SELECT MAX(id) FROM run_logs GROUP BY brand, country)
           ORDER BY r.brand, r.country"""
    out = rows_to_dict(c.execute(q))
    c.close()
    return out


def api_anomalies():
    c = conn()
    out = rows_to_dict(c.execute(
        "SELECT * FROM maintenance_queue WHERE status='open' ORDER BY detected_at DESC"))
    c.close()
    return out


def api_runs():
    c = conn()
    out = rows_to_dict(c.execute("SELECT * FROM run_logs ORDER BY finished_at DESC LIMIT 200"))
    c.close()
    return out


def api_overview():
    """总览：KPI 汇总 + 品牌×国家覆盖矩阵 + 健康汇总。"""
    c = conn()
    quarters = [r["quarter"] for r in c.execute(
        "SELECT DISTINCT quarter FROM price_snapshots ORDER BY quarter")]
    latest = quarters[-1] if quarters else db.this_quarter()
    # KPI
    kpis = {}
    kpis["brands"] = c.execute("SELECT COUNT(*) n FROM brands").fetchone()["n"]
    kpis["countries"] = c.execute("SELECT COUNT(DISTINCT country_code) n FROM models").fetchone()["n"]
    kpis["models"] = c.execute("SELECT COUNT(*) n FROM models").fetchone()["n"]
    kpis["price_rows"] = c.execute("SELECT COUNT(*) n FROM price_snapshots").fetchone()["n"]
    kpis["parts"] = c.execute("SELECT COUNT(*) n FROM parts").fetchone()["n"]
    kpis["quarters"] = len(quarters)
    kpis["latest_quarter"] = latest
    kpis["open_issues"] = c.execute(
        "SELECT COUNT(*) n FROM maintenance_queue WHERE status='open'").fetchone()["n"]
    # 2026-09-18：全品类口径下，总量 KPI 会掩盖品类结构（如"机型 5324 台"里有多少是手机）。
    # 比价/走势一律按 category 分组，故这里同时给出分类别明细。
    kpis["models_by_category"] = {r["category"]: r["n"] for r in rows_to_dict(c.execute(
        "SELECT COALESCE(category,'phone') category, COUNT(*) n FROM models GROUP BY 1"))}
    kpis["price_rows_by_category"] = {r["category"]: r["n"] for r in rows_to_dict(c.execute(
        """SELECT COALESCE(m.category,'phone') category, COUNT(*) n
           FROM price_snapshots ps
           JOIN parts p ON p.id=ps.part_id
           JOIN models m ON m.id=p.model_id
           GROUP BY 1"""))}
    # 健康汇总（最新一次运行）
    hl = rows_to_dict(c.execute(
        """SELECT status, COUNT(*) n FROM run_logs
           WHERE id IN (SELECT MAX(id) FROM run_logs GROUP BY brand, country)
           GROUP BY status"""))
    kpis["health"] = {r["status"]: r["n"] for r in hl}
    # 覆盖矩阵：每个 brand/country 最新状态 + 价行数
    cov = rows_to_dict(c.execute(
        """SELECT r.brand, r.country, r.status, r.rows_written, r.anomaly_flag,
                  (SELECT COUNT(*) FROM price_snapshots ps
                     JOIN parts p ON p.id=ps.part_id
                     JOIN models m ON m.id=p.model_id
                     JOIN brands b ON b.id=m.brand_id
                     WHERE b.name=r.brand AND m.country_code=r.country) AS price_rows,
                  (SELECT COUNT(*) FROM maintenance_queue q
                     WHERE q.brand=r.brand AND q.country=r.country AND q.status='open') AS open_issues
           FROM run_logs r
           WHERE r.id IN (SELECT MAX(id) FROM run_logs GROUP BY brand, country)
           ORDER BY r.brand, r.country"""))
    c.close()
    return {"kpis": kpis, "coverage": cov, "quarters": quarters}


def api_models(brand=None, country=None):
    c = conn()
    q = """SELECT DISTINCT m.name model, b.name brand, m.country_code country
           FROM models m JOIN brands b ON b.id=m.brand_id WHERE 1=1"""
    args = []
    if brand:
        q += " AND b.name=?"; args.append(brand)
    if country:
        q += " AND m.country_code=?"; args.append(country)
    q += " ORDER BY b.name, m.country_code, m.name"
    out = rows_to_dict(c.execute(q, args))
    c.close()
    return out


def api_parts(brand=None, country=None, model=None):
    c = conn()
    # 同时返回原文名(part)与归一化名(canonical/canonical_type)，供前端用备件级口径
    # 而非各自实现一套粗粒度 canon()——两套口径不一致会导致矩阵与走势页对不上。
    q = """SELECT DISTINCT COALESCE(p.canonical_name, p.name) canonical,
                  COALESCE(p.canonical_type, p.part_type) canonical_type,
                  p.variant variant,
                  p.name part, b.name brand, m.country_code country, m.name model
           FROM parts p
           JOIN models m ON m.id=p.model_id
           JOIN brands b ON b.id=m.brand_id WHERE 1=1"""
    args = []
    if brand:
        q += " AND b.name=?"; args.append(brand)
    if country:
        q += " AND m.country_code=?"; args.append(country)
    if model:
        q += " AND m.name=?"; args.append(model)
    q += " ORDER BY canonical, p.name"
    out = rows_to_dict(c.execute(q, args))
    c.close()
    return out


def api_part_series(brand=None, country=None, model=None, part=None):
    c = conn()
    q = """SELECT ps.quarter, ps.price, ps.currency, ps.cny_price,
                  COALESCE(p.canonical_name, p.name) canonical,
                  COALESCE(p.canonical_type, p.part_type) canonical_type,
                  p.variant variant,
                  b.name brand, m.name model, m.country_code country, p.name part
           FROM price_snapshots ps
           JOIN parts p ON p.id=ps.part_id
           JOIN models m ON m.id=p.model_id
           JOIN brands b ON b.id=m.brand_id WHERE 1=1"""
    args = []
    if brand:
        q += " AND b.name=?"; args.append(brand)
    if country:
        q += " AND m.country_code=?"; args.append(country)
    if model:
        q += " AND m.name=?"; args.append(model)
    if part:
        # 兼容两种入参：归一化名（前端首选）或官网原文名
        q += " AND (p.canonical_name=? OR p.name=?)"; args.extend([part, part])
    q += " ORDER BY ps.quarter"
    out = rows_to_dict(c.execute(q, args))
    c.close()
    return out


# ---------------- 比价维度（公平比价：锁定产品，只变一个维度） ----------------

def api_tiers():
    """已有档位列表（旗舰/高端/中端/入门），按语义排序。"""
    c = conn()
    order = {"旗舰": 0, "高端": 1, "中端": 2, "入门": 3}
    rows = [r["tier"] for r in c.execute(
        "SELECT DISTINCT tier FROM models WHERE tier IS NOT NULL")]
    rows.sort(key=lambda t: order.get(t, 9))
    c.close()
    return rows


def api_fx(quarter=None):
    """汇率留痕（P0-2）：返回某季度各币种→CNY 的折算率、来源与时点，供前端标注可信度。"""
    c = conn()
    if not quarter:
        quarter = db.this_quarter()
    q = """SELECT currency, rate_to_cny, rate_source, rate_as_of
           FROM exchange_rates WHERE quarter=? ORDER BY currency"""
    out = rows_to_dict(c.execute(q, (quarter,)))
    c.close()
    return {"quarter": quarter, "rates": out}


def api_brand_models(brand=None):
    """某品牌下的基础机型清单（按 base_model 聚合，含档位、规格/颜色/版本列表、有价格的国别）。用于模式①选机型。"""
    c = conn()
    q = """SELECT b.name brand, m.base_model, m.tier,
                  GROUP_CONCAT(DISTINCT m.country_code) countries,
                  GROUP_CONCAT(DISTINCT m.spec) specs,
                  GROUP_CONCAT(DISTINCT m.color) colors,
                  GROUP_CONCAT(DISTINCT m.edition) editions
           FROM models m JOIN brands b ON b.id=m.brand_id WHERE 1=1"""
    args = []
    if brand:
        q += " AND b.name=?"; args.append(brand)
    q += " GROUP BY m.base_model, m.tier ORDER BY m.tier, m.base_model"
    out = rows_to_dict(c.execute(q, args))
    for r in out:
        r["model"] = r["base_model"]          # 前端兼容字段名
        r["countries"] = [x for x in (r["countries"] or "").split(",") if x]
        r["specs"] = [x for x in (r["specs"] or "").split(",") if x]
        r["colors"] = [x for x in (r["colors"] or "").split(",") if x]
        r["editions"] = [x for x in (r["editions"] or "").split(",") if x]
    c.close()
    return out


def api_model_compare(brand=None, base_model=None, model=None, spec=None, color=None, quarter=None):
    """模式①：同一基础机型在各国家的备件价格（品牌→基础机型→[规格]→[颜色]→国家）。

    不锁定任何"参考配置"：同一机型的所有 (规格,颜色) 组合都会被返回，每个组合独立做
    跨国比价（避免把不同规格/颜色混算）。前端默认展示全部组合，可用规格/颜色 chips 筛选单一组合。
    """
    c = conn()
    if not quarter:
        quarter = db.this_quarter()
    q = """SELECT m.country_code country, m.spec, m.color, m.edition,
                  COALESCE(p.canonical_type, p.part_type, p.name) ctype,
                  COALESCE(p.canonical_name, p.name) cname,
                  COALESCE(p.canonical_spec, '') cspec,
                  p.variant variant, p.part_type cat, p.name part,
                  ps.price, ps.currency, ps.cny_price cny,
                  ps.material_fee, ps.labor_fee, ps.source_url, ps.tax_included, ps.captured_at,
                  ps.has_labor_split, ps.labor_note, ps.labor_source_url, ps.is_seed,
                  ps.rate_source, ps.rate_as_of, ps.source_url_kind,
                  m.name model_name, m.model_url, m.model_url_kind, m.model_url_locator,
                  m.model_url_verified, m.model_page_url
           FROM price_snapshots ps
           JOIN parts p ON p.id=ps.part_id
           JOIN models m ON m.id=p.model_id
           JOIN brands b ON b.id=m.brand_id
           WHERE ps.quarter=?"""
    args = [quarter]
    if brand:
        q += " AND b.name=?"; args.append(brand)
    if base_model:
        q += " AND m.base_model=?"; args.append(base_model)
    elif model:
        q += " AND m.name=?"; args.append(model)
    if spec:
        q += " AND m.spec=?"; args.append(spec)
    if color:
        q += " AND m.color=?"; args.append(color)
    q += (" ORDER BY COALESCE(p.canonical_type, p.part_type),"
          " COALESCE(p.canonical_name, p.name), COALESCE(p.canonical_spec, ''),"
          " m.spec, m.color, m.edition, m.country_code")
    rows = rows_to_dict(c.execute(q, args))
    c.close()
    countries = sorted(set(r["country"] for r in rows))
    # 收集所有 (规格,颜色,版本) 配置，按覆盖国家数降序（最常见组合排前）
    conf_cov = {}
    for r in rows:
        conf_cov.setdefault((r["spec"] or "", r["color"] or "", r["edition"] or ""), set()).add(r["country"])
    configs = sorted(conf_cov, key=lambda k: -len(conf_cov[k]))
    groups = []
    for (sp, co, ed) in configs:
        parts_map = {}
        for r in rows:
            if (r["spec"] or "") != sp or (r["color"] or "") != co or (r["edition"] or "") != ed:
                continue
            # 分组键用归一化三元组：跨语言同义名合并（屏幕组件/Screen Component），
            # 规格仍参与分组（主板 8G+256G ≠ 16G+512G），颜色/版本不参与（已剥离到 variant）。
            key = (r["ctype"], r["cname"], r["cspec"])
            if key not in parts_map:
                parts_map[key] = {"cat": r["ctype"], "part": r["cname"],
                                  "spec": r["cspec"] or None,
                                  "prices": {}, "raw": []}
            ent = parts_map[key]
            # 溯源：记录被合并进本行的官网原文（品类|备件名|变体|国家），封顶 20 条
            raw = [r["cat"], r["part"], r["variant"] or "", r["country"]]
            if raw not in ent["raw"] and len(ent["raw"]) < 20:
                ent["raw"].append(raw)
            cand = {
                "price": r["price"], "currency": r["currency"], "cny": r["cny"],
                "spec": r["spec"], "color": r["color"], "edition": r["edition"],
                "material_fee": r["material_fee"], "labor_fee": r["labor_fee"],
                "source_url": r["source_url"], "tax_included": r["tax_included"],
                "captured_at": r["captured_at"],
                "has_labor_split": r["has_labor_split"], "labor_note": r["labor_note"],
                "labor_source_url": r["labor_source_url"], "is_seed": r["is_seed"],
                "rate_source": r["rate_source"], "rate_as_of": r["rate_as_of"],
                # —— 机型级取证链接（一机一链）：前端只展示 model_url_verified=1 的为"本机型精确链接"
                "source_url_kind": r["source_url_kind"],
                "model_name": r["model_name"], "model_url": r["model_url"],
                "model_url_kind": r["model_url_kind"],
                "model_url_locator": r["model_url_locator"],
                "model_url_verified": r["model_url_verified"],
                "model_page_url": r["model_page_url"],
                "part_raw": r["part"], "part_variant": r["variant"] or None,
                "part_type_raw": r["cat"]}
            prev = ent["prices"].get(r["country"])
            if prev is None:
                cand["variants"] = 1
                cand["cny_min"] = cand["cny_max"] = r["cny"]
                cand["variant_spread"] = None
                ent["prices"][r["country"]] = cand
            else:
                # 同国多价（颜色/限定版变体）：保留最低 CNY 价，但把价差如实暴露，
                # 避免"取 min"把限定版更贵的事实静默丢掉。
                nv = prev.get("variants", 1) + 1
                lo, hi = prev.get("cny_min"), prev.get("cny_max")
                cv = r["cny"]
                if cv is not None:
                    lo = cv if lo is None else min(lo, cv)
                    hi = cv if hi is None else max(hi, cv)
                ck, pk = cand.get("cny"), prev.get("cny")
                keep_new = ck is not None and (pk is None or ck < pk)
                tgt = cand if keep_new else prev
                tgt["variants"] = nv
                tgt["cny_min"], tgt["cny_max"] = lo, hi
                tgt["variant_spread"] = [lo, hi] if (lo is not None and hi is not None and hi > lo) else None
                if keep_new:
                    ent["prices"][r["country"]] = cand
        if not parts_map:
            continue
        g_countries = sorted(set(r["country"] for r in rows
                                 if (r["spec"] or "") == sp and (r["color"] or "") == co
                                 and (r["edition"] or "") == ed))
        groups.append({"spec": sp or None, "color": co or None, "edition": ed or None,
                       "countries": g_countries,
                       "parts": [parts_map[k] for k in sorted(parts_map.keys())]})
    d = {"quarter": quarter, "brand": brand, "base_model": base_model or model,
         "specs": sorted(set(r["spec"] or "" for r in rows if r["spec"])),
         "colors": sorted(set(r["color"] or "" for r in rows if r["color"])),
         "editions": sorted(set(r["edition"] or "" for r in rows if r["edition"])),
         "countries": countries, "groups": groups}
    # 机型存在但本季无任何备件价：回查机型取证元数据，供前端解释"无数据"原因（如官方 code14）
    if not groups:
        try:
            cc = conn()
            qm = """SELECT m.name, m.model_url, m.model_url_kind, m.model_url_locator,
                           m.model_url_verified, m.model_page_url, m.model_url_checked_at
                    FROM models m JOIN brands b ON b.id=m.brand_id WHERE 1=1"""
            margs = []
            if brand: qm += " AND b.name=?"; margs.append(brand)
            if base_model: qm += " AND m.base_model=?"; margs.append(base_model)
            elif model: qm += " AND m.name=?"; margs.append(model)
            if spec: qm += " AND m.spec=?"; margs.append(spec)
            if color: qm += " AND m.color=?"; margs.append(color)
            qm += " LIMIT 1"
            mr = cc.execute(qm, margs).fetchone()
            cc.close()
            if mr:
                d["no_data"] = True
                d["model_meta"] = {k: mr[k] for k in
                    ("name", "model_url", "model_url_kind", "model_url_locator",
                     "model_url_verified", "model_page_url", "model_url_checked_at")}
        except Exception:
            pass
    return d


def api_price_history(brand=None, base_model=None, cat=None, spec=None, color=None, quarter=None,
                      category=None):
    """价格走势：某 (品牌, 基础机型, 规范品类[, 规格, 颜色]) 跨所有季度的逐国时间序列。

    用于前端「是否值得等」决策辅助——展示同一备件在不同季度的 CNY 变化。

    `category` 是**产品品类**（phone/tablet/watch/...）：`base_model` 通常已能唯一定位机型，
    故此处默认不过滤（保持既有行为）；但不传 `base_model` 做品牌级查询时，
    手机与平板的同名备件会混进同一条时间序列，此时应显式传 category。
    """
    c = conn()
    if not quarter:
        quarter = db.this_quarter()
    q = """SELECT ps.quarter, m.country_code country, ps.cny_price cny, ps.price,
                  ps.currency, ps.material_fee, ps.labor_fee, ps.source_url,
                  ps.tax_included, ps.captured_at,
                  ps.has_labor_split, ps.labor_note, ps.labor_source_url, ps.is_seed,
                  ps.rate_source, ps.rate_as_of, ps.source_url_kind,
                  m.name model_name, COALESCE(m.category,'phone') model_category,
                  m.model_url, m.model_url_kind,
                  m.model_url_locator, m.model_url_verified, m.model_page_url
           FROM price_snapshots ps
           JOIN parts p ON p.id=ps.part_id
           JOIN models m ON m.id=p.model_id
           JOIN brands b ON b.id=m.brand_id
           WHERE 1=1"""
    args = []
    if brand:
        q += " AND b.name=?"; args.append(brand)
    if base_model:
        q += " AND m.base_model=?"; args.append(base_model)
    if cat:
        q += " AND (COALESCE(p.canonical_type, p.part_type)=? OR p.part_type=?)"; args.extend([cat, cat])
    if spec:
        q += " AND m.spec=?"; args.append(spec)
    if color:
        q += " AND m.color=?"; args.append(color)
    if category:
        q += " AND COALESCE(m.category,'phone')=?"; args.append(category)
    q += " ORDER BY m.country_code, ps.quarter"
    rows = rows_to_dict(c.execute(q, args))
    c.close()
    quarters = sorted(set(r["quarter"] for r in rows))
    countries = sorted(set(r["country"] for r in rows))
    data = {}
    for r in rows:
        data.setdefault(r["country"], []).append({
            "quarter": r["quarter"], "cny": r["cny"], "price": r["price"],
            "currency": r["currency"], "material_fee": r["material_fee"],
            "labor_fee": r["labor_fee"], "source_url": r["source_url"],
            "tax_included": r["tax_included"], "captured_at": r["captured_at"],
            "has_labor_split": r["has_labor_split"], "labor_note": r["labor_note"],
            "labor_source_url": r["labor_source_url"], "is_seed": r["is_seed"],
            "rate_source": r["rate_source"], "rate_as_of": r["rate_as_of"],
            "source_url_kind": r["source_url_kind"],
            "model_name": r["model_name"], "model_category": r["model_category"],
            "model_url": r["model_url"],
            "model_url_kind": r["model_url_kind"],
            "model_url_locator": r["model_url_locator"],
            "model_url_verified": r["model_url_verified"],
            "model_page_url": r["model_page_url"]})
    return {"brand": brand, "base_model": base_model, "cat": cat, "category": category,
            "spec": spec, "color": color, "quarters": quarters,
            "countries": countries, "data": data, "focus_quarter": quarter}


def api_third_party(brand=None, cat=None, quarter=None):
    """官方价 vs 第三方兼容件参考价（决策辅助：判断官方价是否合理）。

    official_avg_cny = 该品牌该品类在季度的各国 CNY 均价；ref_cny 来自 third_party_prices
    （演示库为按品类倍率估算，真实数据应来自兼容件聚合）。
    """
    c = conn()
    if not quarter:
        quarter = db.this_quarter()
    row = c.execute(
        """SELECT AVG(ps.cny_price) avg FROM price_snapshots ps
           JOIN parts p ON p.id=ps.part_id
           JOIN models m ON m.id=p.model_id
           JOIN brands b ON b.id=m.brand_id
           WHERE ps.quarter=? AND b.name=?
             AND (COALESCE(p.canonical_type, p.part_type)=? OR p.part_type=?)""",
        (quarter, brand, cat, cat)).fetchone()
    official = round(row["avg"], 2) if row and row["avg"] is not None else None
    tp = c.execute(
        "SELECT ref_cny, note FROM third_party_prices WHERE brand=? AND part_type=? AND quarter=?",
        (brand, cat, quarter)).fetchone()
    ref = round(tp["ref_cny"], 2) if tp and tp["ref_cny"] is not None else None
    factor = round(ref / official, 2) if (ref and official) else None
    c.close()
    return {"brand": brand, "cat": cat, "quarter": quarter,
            "official_avg_cny": official, "ref_cny": ref, "factor": factor,
            "note": (tp["note"] if tp else ""),
            "is_demo_estimate": True}


def api_tier_matrix(tier=None, country=None, quarter=None, category=None):
    """模式②：同档位内，各品牌按「规范件名(+规格)」的 CNY 均价（跨品牌公平比价）。

    ⚠ 分组键是 canonical_name（规范件名），不是 canonical_type（品类）。
    按品类平均会把 ¥10 的「摄像头镜片」和 ¥2600 的「后置潜望长焦摄像头」
    混算成一个没有意义的"摄像头均价"——镜头/镜片/盖/环是不同实物，必须分开。

    ⚠ **必须限定产品品类 `category`（默认 phone）**：2026-09-18 起库里收了平板/手表/耳机/手环，
    「屏幕」这个件名在手机和平板上都存在，但**平板的屏幕和手机的屏幕不是同一个东西**，
    混算均价/做跨品牌对比都没有意义。这与上面"按件名分组"是同一条原则，
    只是维度更高一层：先限定品类，再按件名分组。

    另外只保留 >=2 个品牌都有数据的备件（未指定国家时），因为模式②的前提
    就是"跨品牌可比"；某品牌独占的备件留在表里只会制造空列。

    同一基础机型若有多个规格(SKU)，按"参考规格"(覆盖国家最广)取一个代表价，
    避免把同机型的 128GB/512GB 两个价格都算进均值导致失真。
    """
    c = conn()
    if not quarter:
        quarter = db.this_quarter()
    q = """SELECT b.name brand, m.base_model, m.spec, m.color, m.country_code country,
                  COALESCE(p.canonical_name, p.name) pname,
                  COALESCE(p.canonical_spec, '') pspec,
                  COALESCE(p.canonical_type, p.part_type) cat, ps.cny_price cny,
                  ps.is_seed, ps.rate_source
           FROM price_snapshots ps
           JOIN parts p ON p.id=ps.part_id
           JOIN models m ON m.id=p.model_id
           JOIN brands b ON b.id=m.brand_id
           WHERE ps.quarter=? AND m.tier=? AND COALESCE(m.category,'phone')=?"""
    cat = (category or "phone").strip() or "phone"
    args = [quarter, tier, cat]
    if country:
        q += " AND m.country_code=?"; args.append(country)
    q += " ORDER BY b.name, pname"
    rows_all = rows_to_dict(c.execute(q, args))
    c.close()
    # P1-2：剔除演示/种子数据，避免污染跨品牌均值（apple 等仅含 seed）
    rows = [r for r in rows_all if not r.get("is_seed")]
    # 每个 (brand, base_model) 的参考配置 = 覆盖国家最多的 (规格,颜色)
    conf_countries = {}
    for r in rows:
        conf_countries.setdefault((r["brand"], r["base_model"]), {})\
                       .setdefault((r["spec"] or "", r["color"] or ""), set()).add(r["country"])
    ref_of = {k: max(v, key=lambda s: len(v[s])) for k, v in conf_countries.items()}
    # 取参考配置代表价，按 (brand, base_model, 备件) 去重，避免同机型多 SKU 重复计入均值
    picked = {}
    for r in rows:
        key = (r["brand"], r["base_model"], r["pname"], r["pspec"])
        ref = ref_of.get((r["brand"], r["base_model"]), ("", ""))
        if (r["spec"] or "", r["color"] or "") == ref:
            picked[key] = r
        elif key not in picked:        # 参考配置缺该国时兜底取任一配置
            picked[key] = r
    cells = {}      # label(规范件名+规格) -> brand -> [sum, n]
    cat_of = {}     # label -> 规范品类（供前端分组显示）
    mcount = {}     # brand -> set(base_model)
    for (brand, base_model, pname, pspec), r in picked.items():
        label = pname + (" " + pspec if pspec else "")
        cells.setdefault(label, {}).setdefault(brand, [0, 0])
        if r["cny"] is not None:
            cells[label][brand][0] += r["cny"]
            cells[label][brand][1] += 1
        cat_of.setdefault(label, r["cat"] or "其他")
        mcount.setdefault(brand, set()).add(base_model)
    # 未指定国家时要求 >=2 个品牌都有数据（"跨品牌可比"的前提）；
    # 指定国家后已无跨品牌含义，放宽为 >=1。
    min_brands = 1 if country else 2
    out_rows = []
    for label, bd in cells.items():
        vals = {b: (v[0] / v[1] if v[1] else None) for b, v in bd.items()}
        if len([v for v in vals.values() if v is not None]) < min_brands:
            continue
        out_rows.append({"label": label, "cat": cat_of.get(label, "其他"), "cells": vals})
    # 品类按"该品类内最贵备件"降序；品类内备件按均价降序
    def _cat_rank(cat):
        vs = [v for r in out_rows if r["cat"] == cat
              for v in r["cells"].values() if v is not None]
        return -(max(vs) if vs else 0.0)
    cats = sorted({r["cat"] for r in out_rows}, key=_cat_rank)
    out_rows.sort(key=lambda r: (cats.index(r["cat"]),
                                 -max([v for v in r["cells"].values() if v is not None] or [0.0])))
    brands = sorted({b for r in out_rows for b in r["cells"]})
    return {"quarter": quarter, "tier": tier, "country": country, "category": cat,
            "brands": brands, "cats": cats, "rows": out_rows,
            "models_per_brand": {b: len(s) for b, s in mcount.items()}}


PRICE_BAND_TOL = 0.15      # 同档相对容差
PRICE_BAND_MIN_PARTS = 5   # 一个档至少含几个不同备件才认定


def api_price_bands(brand=None, model=None, quarter=None):
    """定价结构：同一机型在各国，是否存在「多个不同备件挤在同一价位」的统一定价档。

    背景（2026-09-17 实证）：OPPO 德国把 **9/10 个备件**定价在 €55–70 窄带，
    且**不同机型、不同配件价格完全相同**（A5 2025 与 A6 Pro 5G 的卡托都是 €62）
    → 属"档位定价"，不是"零件成本定价"。把 €62 挂在「卡托」名下会被误读成
    "卡托成本 €62"，进而得出"德国卡托比中国贵 5000%"这种数学正确、归因错误的结论。

    本接口把该结构**如实呈现**（不删改任何价格），供前端说明
    "这个价格实际涵盖哪些备件"，从而避免把档位价当作零件价解读。

    判据：同机型内，>=5 个不同备件的价格落在 15% 相对容差内。
    实测区分度（oppo）：德国 9/10 机型命中，中国 11/274（4%），后者均为真实的
    配件同价（如后盖上下组件），非误报。
    """
    c = conn()
    if not quarter:
        quarter = db.this_quarter()
    q = """SELECT m.country_code country, ps.currency cur,
                  COALESCE(p.canonical_name, p.name) cname,
                  COALESCE(p.canonical_spec, '') cspec,
                  COALESCE(p.canonical_type, p.part_type) ctype,
                  ps.price price
           FROM price_snapshots ps
           JOIN parts p ON p.id=ps.part_id
           JOIN models m ON m.id=p.model_id
           JOIN brands b ON b.id=m.brand_id
           WHERE b.name=? AND ps.quarter=? AND ps.price>0
             AND COALESCE(ps.is_seed,0)=0"""
    args = [brand, quarter]
    if model:
        q += " AND (m.base_model=? OR m.model_key=?)"
        args += [model, model]
    q += " ORDER BY m.country_code, ps.price"
    rows = rows_to_dict(c.execute(q, args))
    c.close()

    by_country = {}
    for r in rows:
        d = by_country.setdefault(r["country"], {"cur": r["cur"], "parts": {}})
        # 按 (规范件名, 规格) 去重：同名不同规格价格不同，合并会失真；
        # 但同规格多颜色只保留一条（颜色不参与定价）。
        d["parts"].setdefault((r["cname"], r["cspec"]),
                              {"name": r["cname"], "spec": r["cspec"],
                               "type": r["ctype"], "price": r["price"]})

    out = []
    for cc, d in sorted(by_country.items()):
        items = sorted(d["parts"].values(), key=lambda x: x["price"])
        if not items:
            continue
        bands = []          # 贪心聚类：与上一项相对差 <= tol 视为同档
        for it in items:
            if bands and abs(it["price"] - bands[-1][-1]["price"]) <= bands[-1][-1]["price"] * PRICE_BAND_TOL:
                bands[-1].append(it)
            else:
                bands.append([it])
        total = len(items)
        keep = []
        for b in bands:
            if len(b) < PRICE_BAND_MIN_PARTS:
                continue
            keep.append({
                "lo": round(b[0]["price"], 2), "hi": round(b[-1]["price"], 2),
                "size": len(b), "share": round(len(b) / total, 3),
                "members": [{"name": m["name"], "spec": m["spec"],
                             "type": m["type"], "price": m["price"]} for m in b],
            })
        out.append({
            "country": cc, "currency": d["cur"], "n_parts": total,
            "range_ratio": round(items[-1]["price"] / items[0]["price"], 1) if items[0]["price"] else None,
            "bands": keep,
        })
    return {"brand": brand, "model": model, "quarter": quarter,
            "tol": PRICE_BAND_TOL, "min_parts": PRICE_BAND_MIN_PARTS,
            "countries": out}


def api_model_links(brand=None, country=None, only_bad=False):
    """机型级取证链接覆盖（一机一链审计）。

    kind 含义：
      model_api            该链接是仅返回本机型价格的官方接口（vivo / xiaomi / oppo）
      category_api_locator 官网只提供品类级接口（Apple），但本机型价格就在该响应内，
                           locator 给出精确 JSON 路径，打开即可核对本机型
      model_text_fragment  官网为整表静态页（三星），链接用浏览器文本片段直接定位到本机型行
      brand_entry          仅品牌入口页（未落实机型级定位）—— 不得当作本机型精确链接
    verified=1 表示已实际请求该链接并确认内容命中本机型。
    """
    c = conn()
    q = """SELECT b.name brand, m.country_code country, m.name model,
                  m.model_url, m.model_url_kind kind, m.model_url_locator locator,
                  m.model_url_verified verified, m.model_url_checked_at checked_at,
                  m.model_page_url page_url, m.source_url legacy_source_url
           FROM models m JOIN brands b ON b.id=m.brand_id WHERE 1=1"""
    args = []
    if brand:
        q += " AND b.name=?"; args.append(brand)
    if country:
        q += " AND m.country_code=?"; args.append(country)
    if only_bad:
        q += " AND (m.model_url IS NULL OR m.model_url_verified IS NOT 1)"
    rows = rows_to_dict(c.execute(q + " ORDER BY b.name, m.country_code, m.name", args))
    # 汇总：按 品牌/国家 统计覆盖率与形态
    summary = {}
    for r in rows:
        k = f"{r['brand']}/{r['country']}"
        s = summary.setdefault(k, {"brand": r["brand"], "country": r["country"], "n": 0,
                                   "n_url": 0, "n_verified": 0, "kinds": {}})
        s["n"] += 1
        if r["model_url"]:
            s["n_url"] += 1
        if r["verified"] == 1:
            s["n_verified"] += 1
        s["kinds"][r["kind"] or "none"] = s["kinds"].get(r["kind"] or "none", 0) + 1
    c.close()
    for s in summary.values():
        s["verified_rate"] = round(s["n_verified"] / s["n"] * 100, 2) if s["n"] else 0.0
    return {"summary": sorted(summary.values(), key=lambda x: (x["brand"], x["country"])),
            "n_rows": len(rows), "rows": rows[:3000]}


# ============ 触发式重新抓取（POST /api/crawl，需 token） ============
# token：默认占位值，公网暴露时务必用环境变量 CRAWL_TOKEN 覆盖为强随机串。
CRAWL_TOKEN = os.environ.get("CRAWL_TOKEN", "spareparts-crawl-2026")
# 抓取进程兜底超时（秒）：超过仍未退出则强制回收任务状态，避免一直显示 running。
# 可用环境变量 CRAWL_TIMEOUT 覆盖（默认 30 分钟，足够覆盖单品牌×国家的真实抓取耗时）。
CRAWL_TIMEOUT = int(os.environ.get("CRAWL_TIMEOUT", "1800"))
CRAWL_JOBS = {}
_CRAWL_SEQ = 0
_CRAWL_LOCK = threading.Lock()

def _crawl_worker(job_id):
    """后台等待抓取子进程结束并回收状态。

    用轮询 proc.poll() 代替 proc.wait() 阻塞：子进程若因 Playwright 优雅关闭卡死
    不退出，本 worker 会在 CRAWL_TIMEOUT 后强制 kill 并标记 status=timeout，
    保证任务状态绝不会永远停在 running。
    """
    job = CRAWL_JOBS.get(job_id)
    if not job:
        return
    proc = job["proc"]
    deadline = time.time() + CRAWL_TIMEOUT
    rc = None
    while True:
        rc = proc.poll()
        if rc is not None:
            break
        if time.time() >= deadline:
            try:
                proc.kill()
            except Exception:
                pass
            job["error"] = "抓取进程 %d 秒未退出，已强制回收（可能 Playwright 关闭卡死）" % CRAWL_TIMEOUT
            break
        time.sleep(5)
    if rc is not None:
        job["status"] = "done" if rc == 0 else "failed"
        job["returncode"] = rc
    else:
        job["status"] = "timeout"
    job["finished_at"] = datetime.now().isoformat(timespec="seconds")
    lf = job.get("_logf")
    if lf:
        try:
            lf.close()
        except Exception:
            pass

def _brand_in_db(name):
    c = conn()
    try:
        return bool(c.execute("SELECT 1 FROM brands WHERE name=?", (name,)).fetchone())
    finally:
        c.close()

def _country_in_db(code):
    c = conn()
    try:
        return bool(c.execute("SELECT 1 FROM countries WHERE code=?", (code,)).fetchone())
    finally:
        c.close()

def launch_crawl(brand, country, force=False):
    """派发一次异步抓取。返回 (job_dict, error_msg)；job_dict 已剔除不可序列化的 proc。

    force=True：带上 --force，忽略本季断点续跑跳过、对全部机型重新取价。
    用于「本季已落库但数据陈旧/损坏」的区域（如 xiaomi/cn 2026-09-03 的旧行）。
    """
    global _CRAWL_SEQ
    with _CRAWL_LOCK:
        for j in CRAWL_JOBS.values():
            if j["status"] == "running":
                return None, "已有抓取任务在运行（job_id=%s），请等待完成后再派发" % j["id"]
        _CRAWL_SEQ += 1
        job_id = "crawl-%d" % _CRAWL_SEQ
        out_dir = ROOT / "output"
        out_dir.mkdir(exist_ok=True)
        log_path = out_dir / ("_crawl_%s.log" % job_id)
        log_f = open(str(log_path), "w", encoding="utf-8", buffering=1)
        cmd = [sys.executable, str(ROOT / "crawler" / "run.py")]
        if brand:
            cmd += ["--brand", brand]
        if country:
            cmd += ["--country", country]
        if force:
            cmd += ["--force"]
        try:
            proc = subprocess.Popen(cmd, cwd=str(ROOT),
                                    stdout=log_f, stderr=subprocess.STDOUT)
        except Exception as e:
            log_f.close()
            return None, "启动抓取进程失败：%s" % e
        job = {"id": job_id, "brand": brand or "*", "country": country or "*",
               "pid": proc.pid, "proc": proc, "_logf": log_f, "status": "running",
               "started_at": datetime.now().isoformat(timespec="seconds"),
               "log": str(log_path), "returncode": None, "finished_at": None,
               "error": None}
        CRAWL_JOBS[job_id] = job
        threading.Thread(target=_crawl_worker, args=(job_id,), daemon=True).start()
        return {k: v for k, v in job.items() if k not in ("proc", "_logf")}, None

def api_crawl_status():
    out = []
    for j in CRAWL_JOBS.values():
        d = {k: v for k, v in j.items() if k not in ("proc", "_logf")}
        try:
            with open(j["log"], "r", encoding="utf-8", errors="replace") as f:
                d["tail"] = "".join(f.readlines()[-20:])
        except Exception:
            d["tail"] = ""
        out.append(d)
    out.sort(key=lambda x: x.get("started_at", ""), reverse=True)
    return {"jobs": out, "token_hint": "在请求头 X-Crawl-Token 或 query ?token= 携带"}

ROUTES = {
    "/api/crawl/status": lambda qs: api_crawl_status(),
    "/api/overview": lambda qs: api_overview(),
    "/api/model_links": lambda qs: api_model_links(qs.get("brand", [None])[0],
                                                   qs.get("country", [None])[0],
                                                   qs.get("only_bad", ["0"])[0] in ("1", "true")),
    "/api/brands": lambda qs: api_brands(),
    "/api/countries": lambda qs: api_countries(),
    "/api/quarters": lambda qs: api_quarters(),
    "/api/list": lambda qs: api_list(qs.get("brand", [None])[0], qs.get("country", [None])[0]),
    "/api/matrix": lambda qs: api_matrix(qs.get("quarter", [None])[0], qs.get("category", [None])[0]),
    "/api/trend": lambda qs: api_part_series(None, None, None, None) if False else
        api_part_series(qs.get("brand", [None])[0], qs.get("country", [None])[0],
                        qs.get("model", [None])[0], qs.get("part", [None])[0]),
    "/api/alerts": lambda qs: api_alerts(qs.get("quarter", [None])[0]),
    "/api/health": lambda qs: api_health(),
    "/api/anomalies": lambda qs: api_anomalies(),
    "/api/runs": lambda qs: api_runs(),
    "/api/models": lambda qs: api_models(qs.get("brand", [None])[0], qs.get("country", [None])[0]),
    "/api/parts": lambda qs: api_parts(qs.get("brand", [None])[0], qs.get("country", [None])[0],
                                       qs.get("model", [None])[0]),
    "/api/part_series": lambda qs: api_part_series(qs.get("brand", [None])[0], qs.get("country", [None])[0],
                                                   qs.get("model", [None])[0], qs.get("part", [None])[0]),
    "/api/tiers": lambda qs: api_tiers(),
    "/api/fx": lambda qs: api_fx(qs.get("quarter", [None])[0]),
    "/api/brand_models": lambda qs: api_brand_models(qs.get("brand", [None])[0]),
    "/api/model_compare": lambda qs: api_model_compare(qs.get("brand", [None])[0], qs.get("base_model", [None])[0], qs.get("model", [None])[0], qs.get("spec", [None])[0], qs.get("color", [None])[0], qs.get("quarter", [None])[0]),
    "/api/tier_matrix": lambda qs: api_tier_matrix(qs.get("tier", [None])[0], qs.get("country", [None])[0], qs.get("quarter", [None])[0], qs.get("category", [None])[0]),
    "/api/price_bands": lambda qs: api_price_bands(qs.get("brand", [None])[0], qs.get("model", [None])[0], qs.get("quarter", [None])[0]),
    "/api/price_history": lambda qs: api_price_history(qs.get("brand", [None])[0], qs.get("base_model", [None])[0], qs.get("cat", [None])[0], qs.get("spec", [None])[0], qs.get("color", [None])[0], qs.get("quarter", [None])[0], qs.get("category", [None])[0]),
    "/api/third_party": lambda qs: api_third_party(qs.get("brand", [None])[0], qs.get("cat", [None])[0], qs.get("quarter", [None])[0]),
}

_CONTENT = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    def _send(self, obj, code=200, ctype="application/json; charset=utf-8"):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8") if not isinstance(obj, bytes) else obj
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path in ROUTES:
            try:
                self._send(ROUTES[u.path](qs))
            except Exception as e:
                self._send({"error": str(e)}, 500)
            return
        # 静态文件：web/
        if WEB_ROOT.exists():
            rel = (u.path.lstrip("/") or "index.html")
            fp = (WEB_ROOT / rel).resolve()
            if str(fp).startswith(str(WEB_ROOT)) and fp.exists() and fp.is_file():
                ctype = _CONTENT.get(fp.suffix, "application/octet-stream")
                self._send(fp.read_bytes(), 200, ctype)
                return
            # SPA 兜底：未知非 api 路径返回 index.html
            idx = (WEB_ROOT / "index.html").resolve()
            if idx.exists() and u.path != "/":
                self._send(idx.read_bytes(), 200, "text/html; charset=utf-8")
                return
        self._send({"error": "not found", "path": u.path}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path == "/api/crawl":
            token = qs.get("token", [None])[0] or self.headers.get("X-Crawl-Token")
            if token != CRAWL_TOKEN:
                self._send({"error": "token 无效"}, 403)
                return
            brand = (qs.get("brand", [""])[0] or "").strip() or None
            country = (qs.get("country", [""])[0] or "").strip() or None
            if brand and not _brand_in_db(brand):
                self._send({"error": "未知品牌：%s（可用值见 /api/brands 的 name 字段）" % brand}, 400)
                return
            if country and not _country_in_db(country):
                self._send({"error": "未知国家：%s（可用值见 /api/countries 的 code 字段）" % country}, 400)
                return
            job, err = launch_crawl(brand, country,
                                    force=(qs.get("force", [""])[0] or "").lower()
                                    in ("1", "true", "yes"))
            if err:
                self._send({"error": err}, 409)
                return
            self._send(job, 202)
            return
        self._send({"error": "not found", "path": u.path}, 404)

    def log_message(self, *a):
        pass


def main():
    db.init_db()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[api] 备件价格监控中台启动 http://localhost:{PORT}  (Ctrl+C 停止)")
    print(f"[api] 触发抓取 token = {CRAWL_TOKEN}  （公网暴露请用环境变量 CRAWL_TOKEN 覆盖为强随机串）")
    server.serve_forever()


if __name__ == "__main__":
    main()
