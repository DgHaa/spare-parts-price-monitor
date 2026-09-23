"""verify_quarterly_run.py - 季度全量抓取后的跨品牌完整性体检。

背景：2026-09-23 发生「人工费 50000」事故（JSON 里的 "50.000" 被文本解析器
当成千分位 → ×1000），当时没有任何工具能在跑批后自动发现这类**静默**损坏——
既没有报错，也没有落在日志里，只能靠人眼看界面才发现。本工具把那次事故的
三类特征固化成断言，让下一次跑批能自己喊出来：

  1) 量级异常：人工费相对同类中位数 / 相对物料费 / 绝对上限 任一越界；
  2) 静默失联：某 brand×country 有已发现机型但**本季 0 条价格**
     （= 节点选错 / 接口改版 / 静默 0 机型的典型特征，日志看起来一切正常）；
  3) 结构卫生：孤儿快照、重复快照、非正价格、跨区同价串味；
  4) 状态词汇表：run_logs.status 实际取值必须 ⊆ db.VALID_STATUSES
     （应用层枚举的可观测兜底，见 check_status_vocabulary 说明）。

用法：
  python tools/verify_quarterly_run.py
  python tools/verify_quarterly_run.py --baseline backups/spare_parts_before_quarterly_xxx.db
  python tools/verify_quarterly_run.py --json reports/quarterly_verify.json

退出码：0 = 全部通过；1 = 存在 ERROR 级问题（可用于计划任务串联）。
"""
import argparse
import json
import os
import sqlite3
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
import db  # noqa: E402  status 词汇表单一来源（VALID_STATUSES / STATUS_*）

# 人工费绝对上限（当地货币）——只用于兜住「离谱到不可能是真实定价」的值，
# 阈值刻意放宽，避免把高定价市场误判。真正灵敏的是中位数倍数与物料费比值。
LABOR_CAP = {
    "CNY": 3000, "MYR": 2000, "AED": 2000, "MXN": 15000, "TRY": 40000,
    "EUR": 1000, "JPY": 100000, "USD": 1000, "KRW": 500000,
}
# 中位数倍数上限：×1000 放大必然击穿
LABOR_MEDIAN_MULT = 100
# 人工费 / 物料费 比值上限：真实维修里人工通常不会比物料贵 20 倍
LABOR_MATERIAL_RATIO = 20
# 单件备件折算人民币的合理区间
CNY_PRICE_MIN, CNY_PRICE_MAX = 1, 100000

SEV_ERROR = "ERROR"
SEV_WARN = "WARN"
SEV_INFO = "INFO"


class Report:
    def __init__(self):
        self.rows = []      # (severity, code, message)
        self.tables = []

    def add(self, sev, code, msg):
        self.rows.append((sev, code, msg))

    def has_error(self):
        return any(r[0] == SEV_ERROR for r in self.rows)


def connect(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def brand_country_coverage(con):
    """每个 brand×country 的覆盖：机型数 / 有价机型数 / 快照数 / 今日快照数。"""
    q = """
    SELECT b.name AS brand, m.country_code AS cc,
           COUNT(DISTINCT m.id) AS models,
           COUNT(DISTINCT CASE WHEN ps.id IS NOT NULL THEN m.id END) AS priced_models,
           COUNT(ps.id) AS snaps,
           SUM(CASE WHEN date(ps.captured_at) = date('now','localtime') THEN 1 ELSE 0 END) AS snaps_today,
           MIN(date(ps.captured_at)) AS oldest,
           MAX(date(ps.captured_at)) AS newest
    FROM models m
    JOIN brands b ON b.id = m.brand_id
    LEFT JOIN parts p ON p.model_id = m.id
    LEFT JOIN price_snapshots ps ON ps.part_id = p.id
    GROUP BY b.name, m.country_code
    ORDER BY b.name, m.country_code
    """
    return [dict(r) for r in con.execute(q)]


def price_ranges(con):
    q = """
    SELECT b.name AS brand, ps.currency AS cur,
           COUNT(*) AS n,
           MIN(ps.price) AS pmin, MAX(ps.price) AS pmax,
           MIN(ps.labor_fee) AS lmin, MAX(ps.labor_fee) AS lmax,
           MAX(ps.cny_price) AS cnymax
    FROM price_snapshots ps
    JOIN parts p ON p.id = ps.part_id
    JOIN models m ON m.id = p.model_id
    JOIN brands b ON b.id = m.brand_id
    GROUP BY b.name, ps.currency
    ORDER BY b.name, ps.currency
    """
    return [dict(r) for r in con.execute(q)]


def check_labor(con, rep):
    """人工费量级三连查：绝对上限 / 中位数倍数 / 物料费比值。"""
    caps = ", ".join(f"'{k}'" for k in LABOR_CAP)
    q = f"""
    SELECT b.name AS brand, ps.currency AS cur, ps.labor_fee AS lf,
           ps.material_fee AS mf, m.country_code AS cc, m.name AS model, p.name AS part
    FROM price_snapshots ps
    JOIN parts p ON p.id = ps.part_id
    JOIN models m ON m.id = p.model_id
    JOIN brands b ON b.id = m.brand_id
    WHERE ps.labor_fee IS NOT NULL AND ps.currency IN ({caps})
    """
    buckets = {}
    for r in con.execute(q):
        buckets.setdefault((r["brand"], r["cur"]), []).append(dict(r))

    for (brand, cur), items in sorted(buckets.items()):
        vals = [i["lf"] for i in items]
        med = statistics.median(vals)
        cap = LABOR_CAP.get(cur, 10 ** 9)

        hard = [i for i in items if i["lf"] > cap]
        if hard:
            s = hard[0]
            rep.add(SEV_ERROR, "LABOR_CAP",
                    f"{brand}/{cur} 人工费超上限 {cap}：{len(hard)} 行，"
                    f"最大 {max(i['lf'] for i in hard):.1f}（例 {s['cc']} {s['model']} / {s['part']}）")

        if med and med > 0:
            mult = [i for i in items if i["lf"] > LABOR_MEDIAN_MULT * med]
            if mult:
                s = mult[0]
                rep.add(SEV_ERROR, "LABOR_MEDIAN",
                        f"{brand}/{cur} 人工费中位数 {med:.1f}，但有 {len(mult)} 行超过其 "
                        f"{LABOR_MEDIAN_MULT} 倍（最大 {max(i['lf'] for i in mult):.1f}，"
                        f"例 {s['cc']} {s['model']}）——疑似 ×1000 量级放大")

        # 比值越界必须**同时**满足量级离群，否则会误报：
        # 2026-09-23 实测 OPPO 墨西哥对「卡托/数据线」这类配件收固定最低人工费
        # 240 MXN，而卡托物料本身只有 4.11 MXN（同一机型部件类是 650 MXN），
        # 240/4.11 = 58× 属真实定价。只有「比值大 且 远超同类中位数」才是 ×1000
        # 的特征（真实事故：人工 50000 / 物料 339，中位数 50）。
        ratio = [i for i in items
                 if (i["mf"] or 0) > 0
                 and i["lf"] > LABOR_MATERIAL_RATIO * i["mf"]
                 and med > 0 and i["lf"] > 10 * med]
        if ratio:
            s = ratio[0]
            rep.add(SEV_ERROR, "LABOR_MATERIAL_RATIO",
                    f"{brand}/{cur} 人工费既 > {LABOR_MATERIAL_RATIO}×物料费、又 > 10×同类中位数"
                    f"（{med:.0f}）：{len(ratio)} 行"
                    f"（例 {s['cc']} {s['model']}：人工 {s['lf']:.0f} / 物料 {s['mf']:.0f}）")


def check_structural(con, rep):
    # 非正 / 空价格
    n = con.execute("SELECT COUNT(*) FROM price_snapshots WHERE price IS NULL OR price <= 0").fetchone()[0]
    if n:
        rep.add(SEV_WARN, "PRICE_NONPOS", f"price 为 NULL 或 <=0 的快照：{n} 行")
    # 折算人民币越界
    n = con.execute(
        "SELECT COUNT(*) FROM price_snapshots WHERE cny_price IS NOT NULL "
        "AND (cny_price < ? OR cny_price > ?)", (CNY_PRICE_MIN, CNY_PRICE_MAX)).fetchone()[0]
    if n:
        rep.add(SEV_ERROR, "CNY_OUT_OF_BAND", f"cny_price 越界（<{CNY_PRICE_MIN} 或 >{CNY_PRICE_MAX}）：{n} 行")
    # 孤儿快照
    n = con.execute(
        "SELECT COUNT(*) FROM price_snapshots ps LEFT JOIN parts p ON p.id = ps.part_id WHERE p.id IS NULL"
    ).fetchone()[0]
    if n:
        rep.add(SEV_ERROR, "ORPHAN_SNAP", f"孤儿快照（part_id 无对应备件）：{n} 行")
    # 重复快照
    n = con.execute(
        "SELECT COUNT(*) FROM (SELECT part_id, quarter, COUNT(*) c FROM price_snapshots "
        "GROUP BY part_id, quarter HAVING c > 1)"
    ).fetchone()[0]
    if n:
        rep.add(SEV_ERROR, "DUP_SNAP", f"(part_id, quarter) 重复快照：{n} 组")
    # 孤儿机型 / 孤儿备件
    n = con.execute("SELECT COUNT(*) FROM models WHERE brand_id NOT IN (SELECT id FROM brands)").fetchone()[0]
    if n:
        rep.add(SEV_ERROR, "ORPHAN_MODEL", f"孤儿机型（brand_id 无效）：{n} 行")
    # 参考价 / seed 残留
    n = con.execute("SELECT COUNT(*) FROM price_snapshots WHERE is_reference = 1").fetchone()[0]
    if n:
        rep.add(SEV_INFO, "REF_ROWS", f"参考价行（非本国官方价）：{n} 行")
    n = con.execute("SELECT COUNT(*) FROM price_snapshots WHERE is_seed = 1").fetchone()[0]
    if n:
        rep.add(SEV_WARN, "SEED_ROWS", f"seed 演示数据残留：{n} 行")


def check_silent_failure(cov, rep):
    """有已发现机型却本季 0 条价格 —— 静默失联的最高危信号。"""
    for r in cov:
        if r["models"] > 0 and r["snaps"] == 0:
            rep.add(SEV_ERROR, "SILENT_NO_PRICE",
                    f"{r['brand']}/{r['cc']} 发现 {r['models']} 台机型但本季 0 条价格"
                    f"（疑似节点选错 / 接口改版 / 静默 0 机型）")


def check_crosstalk(con, rep):
    """跨区同价串味：同一备件名在不同国家出现完全相同的原币数字。

    这是 2026-09-22 OPPO「忽略 area 参数的遗留端点」事故的特征签名，
    保留该断言防止回归。
    """
    q = """
    SELECT p.canonical_name AS part, ps.price AS price, ps.currency AS cur,
           COUNT(DISTINCT m.country_code) AS cc_n,
           GROUP_CONCAT(DISTINCT m.country_code) AS ccs
    FROM price_snapshots ps
    JOIN parts p ON p.id = ps.part_id
    JOIN models m ON m.id = p.model_id
    WHERE ps.price > 0 AND p.canonical_name IS NOT NULL AND p.canonical_name != ''
    GROUP BY p.canonical_name, ps.price, ps.currency
    HAVING cc_n >= 3
    """
    hits = [dict(r) for r in con.execute(q)]
    if hits:
        s = hits[0]
        rep.add(SEV_WARN, "CROSSTALK",
                f"跨区同价可疑：{len(hits)} 组（同一备件名在 ≥3 个国家出现相同原币数字，"
                f"例「{s['part']}」={s['price']} {s['cur']} @ {s['ccs']}）")


def compare_baseline(base_path, cur_cov, rep):
    if not base_path or not os.path.exists(base_path):
        rep.add(SEV_INFO, "BASELINE_SKIP", "未提供或找不到基线库，跳过对比")
        return []
    con = connect(base_path)
    try:
        base_cov = brand_country_coverage(con)
    finally:
        con.close()
    bmap = {(r["brand"], r["cc"]): r for r in base_cov}
    cmap = {(r["brand"], r["cc"]): r for r in cur_cov}
    diff = []
    for k in sorted(set(bmap) | set(cmap)):
        b = bmap.get(k, {"models": 0, "snaps": 0, "priced_models": 0})
        c = cmap.get(k, {"models": 0, "snaps": 0, "priced_models": 0})
        d = {"brand": k[0], "cc": k[1],
             "models": c["models"] - b["models"],
             "priced_models": c["priced_models"] - b["priced_models"],
             "snaps": c["snaps"] - b["snaps"]}
        if d["models"] or d["snaps"]:
            diff.append(d)
        if b["snaps"] > 0 and c["snaps"] == 0:
            rep.add(SEV_ERROR, "BASELINE_DROP",
                    f"{k[0]}/{k[1]} 快照数从 {b['snaps']} 掉到 0")
        elif b["snaps"] > 0 and c["snaps"] < b["snaps"] * 0.5:
            rep.add(SEV_WARN, "BASELINE_SHRINK",
                    f"{k[0]}/{k[1]} 快照数下降超 50%：{b['snaps']} → {c['snaps']}")
    return diff


def check_status_vocabulary(con, rep):
    """run_logs.status 白名单巡检：实际取值必须 ⊆ db.VALID_STATUSES。

    这是 db.py 应用层枚举的**可观测兜底**。status 列没有 DB 级 CHECK 约束
    （SQLite 不支持 ALTER TABLE ADD CONSTRAINT，需整表重建，故暂缓），
    写入层已由 db.log_run() 的 validate_status() 断言拦截，但以下情况仍可能漏进来：
      - 历史遗留数据（约束引入之前写入的）
      - 绕过 log_run 的直接 SQL 写入

    为什么必须报 ERROR 而不是 WARN：非法值在下游是**静默**的 ——
      api/server.py 覆盖率推导会把未知值归入 empty（"从未抓到"）→ 虚增我方缺口；
      output/gen_improvement_report.py 的 status IN ('failed','partial') 匹配不到
      → 真正的故障从报告里消失。二者都不会自己喊出来，只能靠本巡检暴露。
    """
    try:
        rows = con.execute(
            "SELECT status, COUNT(*) n FROM run_logs GROUP BY status ORDER BY n DESC").fetchall()
    except sqlite3.OperationalError as e:
        rep.add(SEV_ERROR, "STATUS_TABLE_MISSING", f"无法读取 run_logs：{e}")
        return {}
    seen = {r["status"]: r["n"] for r in rows}
    if not seen:
        rep.add(SEV_INFO, "STATUS_EMPTY", "run_logs 无任何记录")
        return seen
    bad = {s: n for s, n in seen.items() if s not in db.VALID_STATUSES}
    if bad:
        detail = "、".join(f"{s!r}×{n}" for s, n in
                          sorted(bad.items(), key=lambda x: -x[1]))
        rep.add(SEV_ERROR, "STATUS_INVALID",
                f"run_logs.status 出现非法值：{detail}；"
                f"合法值 {sorted(db.VALID_STATUSES)}"
                f"（非法值会被覆盖率推导静默归入 empty，并让改进报告漏报 failed）")
    else:
        rep.add(SEV_INFO, "STATUS_VOCAB",
                "run_logs.status 白名单通过：" +
                "、".join(f"{s}={n}" for s, n in seen.items()))
    return seen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(ROOT, "spare_parts.db"))
    ap.add_argument("--baseline", default=None)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"[FATAL] 找不到数据库：{args.db}")
        return 2

    con = connect(args.db)
    rep = Report()
    try:
        cov = brand_country_coverage(con)
        ranges = price_ranges(con)
        check_labor(con, rep)
        check_structural(con, rep)
        check_silent_failure(cov, rep)
        check_crosstalk(con, rep)
        check_status_vocabulary(con, rep)
        diff = compare_baseline(args.baseline, cov, rep)
    finally:
        con.close()

    print("=" * 96)
    print("覆盖明细（brand×country）")
    print("=" * 96)
    print(f"{'brand':<9}{'cc':<5}{'机型':>6}{'有价机型':>9}{'快照':>8}{'今日新抓':>9}  {'最早':<12}{'最新':<12}")
    for r in cov:
        print(f"{r['brand']:<9}{r['cc']:<5}{r['models']:>6}{r['priced_models']:>9}"
              f"{r['snaps']:>8}{r['snaps_today'] or 0:>9}  {str(r['oldest']):<12}{str(r['newest']):<12}")

    print()
    print("=" * 96)
    print("取值区间（brand×币种）")
    print("=" * 96)
    print(f"{'brand':<9}{'币种':<6}{'条数':>7}  {'价格区间':<26}{'人工费区间':<24}{'CNY上限':>10}")
    for r in ranges:
        pr = f"{r['pmin']:.2f} ~ {r['pmax']:.2f}" if r["pmin"] is not None else "-"
        lr = f"{r['lmin']:.2f} ~ {r['lmax']:.2f}" if r["lmin"] is not None else "-"
        cm = f"{r['cnymax']:.2f}" if r["cnymax"] is not None else "-"
        print(f"{r['brand']:<9}{r['cur']:<6}{r['n']:>7}  {pr:<26}{lr:<24}{cm:>10}")

    if diff:
        print()
        print("=" * 96)
        print("与基线对比（仅列出有变化的 brand×country）")
        print("=" * 96)
        print(f"{'brand':<9}{'cc':<5}{'机型Δ':>8}{'有价机型Δ':>11}{'快照Δ':>9}")
        for d in diff:
            print(f"{d['brand']:<9}{d['cc']:<5}{d['models']:>8}{d['priced_models']:>11}{d['snaps']:>9}")

    print()
    print("=" * 96)
    print("体检结论")
    print("=" * 96)
    if not rep.rows:
        print("  无任何异常项 ✓")
    for sev, code, msg in rep.rows:
        mark = {"ERROR": "✗", "WARN": "!", "INFO": "i"}[sev]
        print(f"  {mark} [{sev:<5}] {code:<22} {msg}")

    errs = sum(1 for r in rep.rows if r[0] == SEV_ERROR)
    warns = sum(1 for r in rep.rows if r[0] == SEV_WARN)
    print()
    print(f"  合计：ERROR {errs} / WARN {warns} / INFO {len(rep.rows) - errs - warns}")
    print("  结果：" + ("存在 ERROR，需人工介入 ✗" if errs else "全部通过 ✓"))

    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"coverage": cov, "ranges": ranges, "diff": diff,
                       "findings": [{"severity": s, "code": c, "message": m} for s, c, m in rep.rows]},
                      f, ensure_ascii=False, indent=2)
        print(f"  明细已写出：{args.json}")

    return 1 if rep.has_error() else 0


if __name__ == "__main__":
    sys.exit(main())
