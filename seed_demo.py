"""seed_demo.py - 灌入演示数据，让中台开箱即见内容。

数据来源：
  - OPPO Ace2（6 国）与 vivo X300 Pro（3 国）：取自 skill 校准 JSON 的【真实】官网价（仅落在最新季度 2026Q3）。
  - 其余品牌（小米/Apple/三星/Google）：演示价（按品类+品牌系数+代际生成，仅用于 UI 演示，
    运行 run_quarterly.py 真实爬虫后会被覆盖）。
  - 为支持「价格走势/历史趋势」演示，所有品牌额外生成 2025Q4~2026Q2 三个历史季度（按比例回落）。

本轮增强（对应合理性分析报告 ①②④）：
  - price_snapshots 现填充 material_fee(物料费)/labor_fee(人工费)/source_url(官网来源深链)/
    captured_at(抓取时间)/tax_included(官网展示价是否含税)；
  - 新增 third_party_prices（兼容件参考价，按 brand×part_type×quarter 存官方溢价倍数估算），
    用于「官方价 vs 第三方兼容件」决策辅助。
  - 扩充小件品类（听筒/扬声器/振动马达/侧键/指纹模组），提升覆盖度。

运行：python seed_demo.py
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db

KB = Path(r"C:/Users/Dong/.workbuddy/skills/spare-parts-price/references/kb")
CAL = Path(r"C:/Users/Dong/.workbuddy/skills/spare-parts-price/references/calibration")

# 历史季度（最新排在最后）；TREND 为相对最新价的倍率（过去更贵，体现“等等更便宜”）
QUARTERS = ["2025Q4", "2026Q1", "2026Q2", "2026Q3"]
TREND = {"2025Q4": 1.12, "2026Q1": 1.06, "2026Q2": 1.03, "2026Q3": 1.00}
LATEST = QUARTERS[-1]
NOW = datetime.now().isoformat(timespec="seconds")

Q_END = {"Q1": "03-31", "Q2": "06-30", "Q3": "09-30", "Q4": "12-31"}


def captured_at_of(quarter):
    y, q = quarter.split("Q")
    return f"{y}-{Q_END['Q'+q]}"


def cny_of(price, currency, quarter):
    return round(price * db.get_rate(quarter, currency), 2) if price is not None else None


# 各币种区域人工费（官方维修常单列）；CNY 置 0 表示“人工未单列/含在服务包”
LABOR = {"EUR": 39, "JPY": 4000, "CNY": 0, "MYR": 60, "TRY": 250, "AED": 90, "MXN": 350}
# 全部国家官网展示价默认含税（含当地 VAT/消费税）
TAX_INCLUDED = 1
# 官方维修/支持入口。
# 策略：优先用【已 WebFetch 逐个核验可达】的国别页；核验 404 或本环境无法访问的，
# 回退到【已核验可达】的品牌级主页。演示价请以官网实时查询为准。
# 核验结论（2026-08-29）：oppo/vivo 各国别页均可达；samsung 仅 de 可达(ae/jp/my/tr 404)；
#   apple 各国别 repair 页均 404(仅全球页可达)；google 本环境屏蔽→品牌页；xiaomi cn 404→品牌页。
SRC_COUNTRY = {
    ("oppo", "ae"): "https://www.oppo.com/ae/support/",
    ("oppo", "de"): "https://www.oppo.com/de/support/",
    ("oppo", "jp"): "https://www.oppo.com/jp/support/",
    ("oppo", "mx"): "https://www.oppo.com/mx/support/",
    ("oppo", "my"): "https://www.oppo.com/my/support/",
    ("oppo", "tr"): "https://www.oppo.com/tr/support/",
    ("vivo", "ae"): "https://www.vivo.com/ae/support/",
    ("vivo", "my"): "https://www.vivo.com/my/support/",
    ("vivo", "tr"): "https://www.vivo.com/tr/support/",
    ("samsung", "de"): "https://www.samsung.com/de/support/repair/",
}
SRC_HUB = {
    "samsung": "https://www.samsung.com/us/support/repair/",
    "apple":   "https://support.apple.com/repair",
    "xiaomi":  "https://www.mi.com/service/",
    "google":  "https://store.google.com/repair",
    "oppo":    "https://www.oppo.com/en/support/",
    "vivo":    "https://www.vivo.com/en/support/",
}


def src_of(brand, country):
    return SRC_COUNTRY.get((brand, country)) or SRC_HUB.get(brand, "")
# 兼容件参考价相对官方物料价的倍率（按品类，越小越“第三方便宜”）
TP_FACTOR = {"屏幕": 0.32, "电池": 0.40, "主板": 0.55, "后盖": 0.35, "摄像头": 0.45,
             "充电口": 0.42, "听筒": 0.45, "扬声器": 0.45, "振动马达": 0.50,
             "侧键": 0.50, "指纹模组": 0.48, "其他": 0.45}

# 规范品类（含小件）+ 基准 CNY 价
CATS = [("屏幕", 2200), ("电池", 150), ("主板", 3200), ("后盖", 350), ("摄像头", 450),
        ("充电口", 120), ("听筒", 80), ("扬声器", 90), ("振动马达", 70), ("侧键", 60), ("指纹模组", 110)]
BRAND_MULT = {"xiaomi": 0.85, "apple": 1.3, "samsung": 1.15, "google": 1.1}
CUR_OF = {"cn": "CNY", "de": "EUR", "jp": "JPY", "ae": "AED", "my": "MYR", "tr": "TRY"}
CN_OF = {"cn": "中国", "de": "德国", "jp": "日本", "ae": "阿联酋", "my": "马来西亚", "tr": "土耳其"}


def spec_mult(model):
    if "512GB" in model:
        return 1.06
    if "256GB" in model:
        return 1.03
    return 1.0


def model_mult(model):
    s = (model or "").lower()
    m = re.search(r"\d{2}", s)
    gen = int(m.group(0)) if (m and 10 <= int(m.group(0)) <= 30) else 20
    bump = 1.0
    if any(k in s for k in ["ultra", "pro max", "fold", "mate", "find x", "pura", "保时捷", "至臻", "rsr"]):
        bump = 1.18
    elif any(k in s for k in ["pro", "plus", "max", "note"]):
        bump = 1.10
    elif any(k in s for k in ["redmi", "lite", "se", "neo", "青春", "畅享", "play", "a 系列"]):
        bump = 0.82
    gen_f = 0.92 + max(0, gen - 20) * 0.012
    return round(gen_f * bump, 3)


def seed_model(brand, country, country_name, currency, model, quarter, trend, parts, bump=None,
               source_url="", conn=None):
    """parts: [(part_name, price)]，price 为该 region 本位币（已含趋势）。"""
    bump = bump or {}
    bid = db.upsert_brand(brand, "seed", conn=conn)
    db.upsert_country(country, country_name, currency, "", conn=conn)
    mid = db.upsert_model(bid, country, model, model, source_url, db.classify_tier(model), conn=conn)
    n = 0
    for pname, price in parts:
        if price is None:
            continue
        price2 = round(price * bump.get(pname, 1.0), 2)
        pid = db.upsert_part(mid, pname, None, conn=conn)
        cny = cny_of(price2, currency, quarter)
        material = price2                       # 物料费=备件本身
        labor = LABOR.get(currency, 0)
        db.insert_snapshot(pid, quarter, price2, currency, cny, material, labor,
                           source_url or src_of(brand, country),
                           captured_at_of(quarter), TAX_INCLUDED, conn=conn)
        n += 1
    return n


def load_oppo_ace2_parts():
    d = json.loads((CAL / "oppo_tr_api.json").read_text(encoding="utf-8"))
    pr = d.get("prices") or []
    if not pr:
        return []
    return [(p["partName"], float(p["partPrice"])) for p in pr[0].get("parts", [])]


def load_vivo_parts():
    d = json.loads((KB / "vivo.json").read_text(encoding="utf-8"))
    out = {}
    for c, recs in d.get("countries", {}).items():
        r = recs[0]
        sm = r.get("sample", {})
        cur = r.get("currency")
        parsed = []
        for row in sm.get("rows", []):
            raw = row.get("price", "")
            m = re.search(r"([\d][\d.,]*)", raw.replace(",", ""))
            val = float(m.group(1)) if m else None
            parsed.append((row.get("part"), val))
        out[c] = (cur, parsed)
    return out


def main():
    db.init_db()
    # 单连接贯穿整个灌库，避免频繁开关连接触发环境资源限制
    conn = db.get_conn()
    conn.executescript("""
        DELETE FROM maintenance_queue;
        DELETE FROM run_logs;
        DELETE FROM third_party_prices;
        DELETE FROM price_snapshots;
        DELETE FROM parts;
        DELETE FROM models;
        DELETE FROM brands;
        DELETE FROM countries;
    """)
    conn.commit()
    for q in QUARTERS:
        db.fetch_rates(q)

    # 累计 (brand, part_type, quarter) -> [cny...] 用于第三方参考价中位数
    acc = {}
    total = 0

    # 1) OPPO Ace2 真实价（仅最新季度）；历史季度按比例回落
    #    注：OPPO Ace2 仅在中国大陆发售，故只落 cn，避免"该国无售"机型残留
    #    （其余国别(德/土/墨/马/日/阿)的 OPPO 真实数据由 crawler.run 的 api_json 模式抓取）。
    oppo_parts = load_oppo_ace2_parts()
    oppo_regions = {"cn": ("中国", "CNY")}
    for c, (cn, cur) in oppo_regions.items():
        for q in QUARTERS:
            trend = TREND[q]
            parts = [(pn, round(pr / db.get_rate(LATEST, cur) * db.get_rate(q, cur) / trend))
                     for pn, pr in oppo_parts] if q != LATEST else oppo_parts
            n = seed_model("oppo", c, cn, cur, "OPPO Ace2", q, trend, parts,
                           source_url=src_of("oppo", c), conn=conn)
            total += n
            db.log_run("oppo", c, q, NOW, NOW, "success", n, "", 0, "", conn=conn)
            for pn, pr in oppo_parts:
                cat = db.normalize_category(pn)
                acc.setdefault(("oppo", cat, q), []).append(cny_of(pr, cur, q))

    # 2) vivo X300 Pro 真实样本价（my/tr/ae，仅最新季度）
    vivo = load_vivo_parts()
    vivo_cn = {"my": "马来西亚", "tr": "土耳其", "ae": "阿联酋"}
    for c, (cur, parts) in vivo.items():
        if c not in vivo_cn or not parts:
            continue
        for q in QUARTERS:
            if q == LATEST:
                pp = parts
            else:
                pp = [(pn, round(pr / db.get_rate(LATEST, cur) * db.get_rate(q, cur) / TREND[q]))
                      for pn, pr in parts if pr]
            n = seed_model("vivo", c, vivo_cn[c], cur, "X300 Pro", q, TREND[q], pp,
                           source_url=src_of("vivo", c), conn=conn)
            total += n
            db.log_run("vivo", c, q, NOW, NOW, "success", n, "", 0, "", conn=conn)
            for pn, pr in parts:
                if not pr:
                    continue
                cat = db.normalize_category(pn)
                acc.setdefault(("vivo", cat, q), []).append(cny_of(pr, cur, q))

    # 3) 演示数据：小米/Apple/三星/Google（全季度）
    demo = {
        "xiaomi": {"cn": ["Xiaomi 13 Pro 8GB内存 陶瓷黑 128GB", "Redmi Note 13 5G 8GB 256GB"]},
        "apple": {"de": ["iPhone 16 Pro Max", "iPhone 15"], "jp": ["iPhone 16 Pro Max", "iPhone 15"],
                  "ae": ["iPhone 16 Pro Max", "iPhone 15"], "my": ["iPhone 16 Pro Max", "iPhone 15"]},
        "samsung": {"de": ["Galaxy S24 Ultra 256GB 钛黑", "Galaxy S24 Ultra 256GB 钛灰", "Galaxy S24 Ultra 512GB", "Galaxy S23 128GB", "Galaxy S23 256GB"],
                    "tr": ["Galaxy S24 Ultra 256GB 钛黑", "Galaxy S24 Ultra 256GB 钛灰", "Galaxy S24 Ultra 512GB", "Galaxy S23 128GB", "Galaxy S23 256GB"],
                    "my": ["Galaxy S24 Ultra 256GB", "Galaxy S24 Ultra 512GB", "Galaxy S23 128GB", "Galaxy S23 256GB"],
                    "jp": ["Galaxy S24 Ultra 256GB", "Galaxy S24 Ultra 512GB", "Galaxy S23 128GB", "Galaxy S23 256GB"],
                    "ae": ["Galaxy S24 Ultra 256GB", "Galaxy S24 Ultra 512GB", "Galaxy S23 128GB", "Galaxy S23 256GB"]},
        "google": {"de": ["Pixel 9 Pro"], "jp": ["Pixel 9 Pro"], "ae": ["Pixel 9 Pro"],
                   "my": ["Pixel 9 Pro"], "tr": ["Pixel 9 Pro"]},
    }
    for brand, cmap in demo.items():
        m = BRAND_MULT[brand]
        for c, models in cmap.items():
            cur = CUR_OF[c]
            for model in models:
                for q in QUARTERS:
                    trend = TREND[q]
                    parts = []
                    for cat, base in CATS:
                        vary = spec_mult(model) if brand == "samsung" else 1.0
                        cny_base = base * m * vary * model_mult(model) * trend
                        price_cur = round(cny_base / db.get_rate(q, cur)) if cur != "CNY" else round(cny_base)
                        parts.append((cat, price_cur))
                    bump = {"后盖": 1.08} if "钛灰" in model else {}
                    n = seed_model(brand, c, CN_OF[c], cur, model, q, trend, parts, bump=bump, conn=conn)
                    total += n
                    db.log_run(brand, c, q, NOW, NOW, "success", n, "", 0, "", conn=conn)
                    for cat, base in CATS:
                        vary = spec_mult(model) if brand == "samsung" else 1.0
                        cny_base = base * m * vary * model_mult(model) * trend
                        acc.setdefault((brand, cat, q), []).append(round(cny_base, 2))

    # 4) 第三方兼容件参考价（按 brand×part_type×quarter 中位数 × 倍率）
    for (brand, cat, q), vals in acc.items():
        if not vals:
            continue
        med = median(vals)
        ref = round(med * TP_FACTOR.get(cat, 0.45), 2)
        db.upsert_third_party(brand, cat, q, ref,
                              "演示估算：兼容件参考价≈官方物料价×%.0f%%" % round(TP_FACTOR.get(cat, 0.45) * 100),
                              conn=conn)

    # 5) 待修工单（演示监控闭环）
    db.log_run("google", "de", LATEST, NOW, NOW, "failed", 0, "", 1,
               "Google 估价页需真实出口 IP（沙箱被墙），待本机代理补完定位器", conn=conn)
    db.add_issue("google", "de", "Google 估价页需真实出口 IP（沙箱被墙），待本机代理补完定位器", conn=conn)
    conn.commit(); conn.close()
    print(f"\n[seed] 共写入 {total} 条价（{len(QUARTERS)} 个季度）+ 第三方参考价 + 运行日志")
    print(f"[seed] 最新季度 {LATEST}；OPPO/vivo 最新季度为真实校准价，其余为演示价。")


if __name__ == "__main__":
    main()
