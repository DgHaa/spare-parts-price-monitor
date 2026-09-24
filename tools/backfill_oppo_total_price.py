"""一次性回填：把 OPPO 快照的 price 口径从「裸备件费」改为「官网预估总价（备件费+人工费）」。

背景（2026-09-24）
-----------------
OPPO 官网 getPartPriceNew 的报价口径是「预估价格 = 备件费 + 人工费」，但抓取层
此前只把 retailPrice 写进 price（裸备件费），而三星/苹果/小米存的都是含人工总价。
后果有二：
  ① 跨品牌不可比：OPPO 显得系统性偏便宜（少算人工）。
  ② 跨国比价假象：OPPO 各区报价风格不同——de/jp 的 laborCostAmount 恒为 0
     （整区不单列人工，retailPrice 即打包价），mx/tr/ae/my 单列人工费。
     拿德国的打包价去比别人家的裸件价，廉价件被放大到 +1225% 之类的假价差。
实测 de/mx 由 6.27x 收敛到 1.46x、de/tr 由 2.84x 到 1.59x（n=49~68）。

本脚本做两件事
--------------
A. price < material_fee + labor_fee（labor_fee>0 的行）：按该行**原汇率**重算
   price 与 cny_price（rate = cny_price / price，保持同一汇率时点，不重算汇率）。
B. labor_fee == 0 的行（de/jp 全区域）：0 是「未单列」的哨兵值而非「人工免费」，
   置 labor_fee=NULL、has_labor_split=0，并按执行器口径补 labor_note。
   （若按 0 展示，前台会写「🔧人工费 0 EUR」，等于替官网宣称德国免人工费——编造。）

幂等性
------
A 的判据是 `ABS(price - material_fee) < 0.005`（回填后 price > material，不再命中）；
B 的判据是 `labor_fee = 0`（回填后为 NULL，不再命中）。故可重复执行。

用法
----
    python tools/backfill_oppo_total_price.py            # 预演（不改库）
    python tools/backfill_oppo_total_price.py --apply    # 执行（先自动备份 DB）
"""
import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import db  # noqa: E402

LABOR_NOTE_BUNDLED = ("官网未单列人工费（laborCostAmount=0），此处为该区打包价"
                      "（含安装），非裸备件费；不做拆分推算")


def _scope(con):
    """回填范围：OPPO 品牌的全部快照（全季度）。"""
    return con.execute("""
        SELECT COUNT(*) FROM price_snapshots ps
        JOIN parts p ON p.id = ps.part_id
        JOIN models m ON m.id = p.model_id
        JOIN brands b ON b.id = m.brand_id
        WHERE b.name = 'oppo'
    """).fetchone()[0]


def _survey(con):
    """A/B 两类待改行数 + 口径违例数。"""
    base = """
        FROM price_snapshots ps
        JOIN parts p ON p.id = ps.part_id
        JOIN models m ON m.id = p.model_id
        JOIN brands b ON b.id = m.brand_id
        WHERE b.name = 'oppo'
    """
    a = con.execute(
        "SELECT COUNT(*) " + base +
        " AND ps.labor_fee IS NOT NULL AND ps.labor_fee > 0"
        " AND ps.material_fee IS NOT NULL"
        " AND ABS(ps.price - ps.material_fee) < 0.005").fetchone()[0]
    b = con.execute(
        "SELECT COUNT(*) " + base +
        " AND ps.labor_fee = 0 AND ps.has_labor_split = 1").fetchone()[0]
    viol = con.execute(
        "SELECT COUNT(*) " + base +
        " AND ps.material_fee IS NOT NULL"
        " AND ABS(ps.price - (ps.material_fee + COALESCE(ps.labor_fee, 0))) > 0.005"
    ).fetchone()[0]
    return a, b, viol


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写库（默认仅预演）")
    args = ap.parse_args()

    path = Path(db.DB_PATH)
    if not path.exists():
        print(f"[err] 找不到数据库 {path}")
        return 1
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row

    total = _scope(con)
    a, b, viol_before = _survey(con)
    print(f"OPPO 快照总数            : {total}")
    print(f"A 类 待补人工（labor>0） : {a}")
    print(f"B 类 哨兵 0 待改未单列   : {b}")
    print(f"口径违例（改前）         : {viol_before}")

    print("\n改动样例（前 5 行）：")
    for r in con.execute("""
        SELECT m.country_code cc, p.name pn, ps.quarter q, ps.price, ps.material_fee mf,
               ps.labor_fee lf, ps.currency cur, ps.cny_price cny
        FROM price_snapshots ps JOIN parts p ON p.id = ps.part_id
        JOIN models m ON m.id = p.model_id JOIN brands b ON b.id = m.brand_id
        WHERE b.name = 'oppo' AND ps.labor_fee > 0 AND ps.material_fee IS NOT NULL
          AND ABS(ps.price - ps.material_fee) < 0.005 LIMIT 5"""):
        rate = (r["cny"] / r["price"]) if r["price"] else None
        newp = r["mf"] + r["lf"]
        newcny = round(newp * rate, 2) if rate else None
        print("  [%s] %-3s %-16s %s -> %s %s（+人工 %s）  CNY %s -> %s" % (
            r["q"], r["cc"], (r["pn"] or "")[:16], r["price"], newp, r["cur"], r["lf"],
            round(r["cny"], 2) if r["cny"] else None, newcny))

    if not args.apply:
        print("\n（预演模式，未改动数据库；加 --apply 执行）")
        con.close()
        return 0

    if a == 0 and b == 0:
        print("\n无需改动（已回填过）。")
        con.close()
        return 0

    bak = path.with_name(f"{path.stem}.bak_{time.strftime('%Y%m%d_%H%M%S')}{path.suffix}")
    shutil.copy2(path, bak)
    print(f"\n[backup] {bak}")

    cur = con.cursor()
    cur.execute("""
        UPDATE price_snapshots SET
            price    = material_fee + labor_fee,
            cny_price = CASE WHEN price IS NULL OR price = 0 THEN NULL
                             ELSE (material_fee + labor_fee) * (cny_price / price) END
        WHERE id IN (
            SELECT ps.id FROM price_snapshots ps
            JOIN parts p ON p.id = ps.part_id
            JOIN models m ON m.id = p.model_id
            JOIN brands b ON b.id = m.brand_id
            WHERE b.name = 'oppo' AND ps.labor_fee > 0 AND ps.material_fee IS NOT NULL
              AND ABS(ps.price - ps.material_fee) < 0.005)
    """)
    n_a = cur.rowcount
    cur.execute("""
        UPDATE price_snapshots SET labor_fee = NULL, has_labor_split = 0, labor_note = ?
        WHERE id IN (
            SELECT ps.id FROM price_snapshots ps
            JOIN parts p ON p.id = ps.part_id
            JOIN models m ON m.id = p.model_id
            JOIN brands b ON b.id = m.brand_id
            WHERE b.name = 'oppo' AND ps.labor_fee = 0 AND ps.has_labor_split = 1)
    """, (LABOR_NOTE_BUNDLED,))
    n_b = cur.rowcount
    con.commit()

    a2, b2, viol = _survey(con)
    print(f"[apply] A 类改动 {n_a} 行，B 类改动 {n_b} 行")
    print(f"复查：A 待改 {a2}（应为 0）、B 待改 {b2}（应为 0）、口径违例 {viol}（应为 0）")
    con.close()
    return 0 if (a2 == 0 and b2 == 0 and viol == 0) else 2


if __name__ == "__main__":
    sys.exit(main())
