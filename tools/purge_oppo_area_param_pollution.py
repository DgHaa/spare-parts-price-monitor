"""清退 OPPO 遗留端点「area 参数被忽略」造成的虚假价格数据。

问题（2026-09-22 实测）：
    sgp-sow-cms.oppo.com/oppo-server/cnw/v1/GetPartPrice?area={cc} 这个遗留端点
    **服务端完全忽略 area 参数**，对 ae/de/jp/mx/my/tr 返回逐字节相同的中国大陆
    CNY 价目表（6 国 1844 个公共 (机型,备件) 键的价格集合指纹全部相等）。
    旧爬虫把「请求的 area」当成「返回的币种」，于是同一个 2690 被写成
    2690 AED / 2690 EUR / 2690 MXN / 2690 TRY，再乘当地汇率折 CNY：
        2690 EUR x 7.8 = 20982 CNY（虚高 7.8 倍）
        2690 TRY x 0.21 =  565 CNY（虚低 4.8 倍）
    同一原始数字被汇率放大成 37 倍 → 前台显示「+3614%」的假价差，
    「最低国/最高国」结论完全失真。

本脚本做的事：
    1. 备份整个 DB 到 backups/
    2. 把命中的污染行完整搬运进 price_snapshots_quarantine（附原因与时间），不销毁证据
    3. 从 price_snapshots 删除这些行
    4. 清理因此变成「零快照」的孤儿 parts（只删本次清退涉及的，不碰其他）
    5. 打印覆盖率影响

!! 与 tools/archive_oppo_legacy.py 的关系（必须先读）!!
    执行前已核实：这 13836 行**早已被 archive_oppo_legacy.py 复制进
    price_snapshots_legacy_archive**（id 完全重合），但当时的「从主表移除」没有生效，
    所以污染价一直挂在线上——这正是本次要补的一刀。
    因此同一批行现在存在于**两个**证据表里，**不要分别还原**（会重复）；本次清退的
    唯一批准回滚路径是本脚本的 --rollback。同时已给 archive_oppo_legacy.py 的
    restore 加了闸门：默认跳过这批已知污染快照，防止有人整体还原时把假价复活。

用法：
    python tools/purge_oppo_area_param_pollution.py            # 只读预览（默认）
    python tools/purge_oppo_area_param_pollution.py --apply    # 实际执行
    python tools/purge_oppo_area_param_pollution.py --rollback --i-know-this-restores-fake-prices
"""
import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "spare_parts.db")
BACKUP_DIR = os.path.join(os.path.dirname(DB), "backups")

# 只认遗留端点本体；'%GetPartPrice%' 会误伤 GetPartPriceProductInfo（机型列表端点，不含价）
BAD = "%cnw/v1/GetPartPrice?%"
REASON = "OPPO 遗留端点 area 参数被服务端忽略，返回中国大陆 CNY 价目表被误标为本地币种"

# 前台横幅文案（brands.price_caveat）。必须写明这是**抓取 bug 已修复**，
# 而不是 OPPO 的定价特征——否则用户会把「各国同数」当成正常的品牌行为。
NEW_CAVEAT = (
    "OPPO 各区域价目表曾出现「同一备件各国原币数字相同」，原因是我方爬虫调用了"
    "忽略 area 参数的遗留端点 /cnw/v1/GetPartPrice——各国拿到的其实都是中国大陆 CNY "
    "价目表（2690 被写成 2690 AED/EUR/MXN/TRY），并非 OPPO 的定价特征。"
    "该端点已于 2026-09-22 加硬阻断，其 13836 行价格数据当轮清退。"
    "【2026-09-23 续查】该端点还遗留了 1123 个**污染机型条目**：其中 186 台实为一加"
    "OnePlus 机型、72 台为中国专供版本（兰博基尼版/火星探索版等），另有 OPPO 智能电视、"
    "OPPO 手环等中国产品线；它们不属于 de/ae/tr/mx/my/jp 任何区域，与真实可抓机型"
    "按（区域+机型名）比对重叠为 0，已一并清退。"
    "现机型表取自 REBORN 的 getProductInfo（**区域产品目录全集**，如德国 192 台），"
    "价格取自 getPartPriceNew，均为**当地官方价**，按「CNY 折算价」正常跨国比较。"
    "各国覆盖差异是**真实**的：OPPO 各市场的产品线本就不同（如德国仅上架部分机型），"
    "且部分机型在当地目录中但官方未公布备件价——此类一律如实留白，"
    "不做推算、不借他国价填充。"
)

QUARANTINE_COLS = [
    "id", "part_id", "quarter", "price", "currency", "cny_price", "material_fee",
    "labor_fee", "source_url", "captured_at", "tax_included", "has_labor_split",
    "labor_note", "labor_source_url", "is_seed", "rate_source", "rate_as_of",
    "source_url_kind",
]


def q(sql):
    return sql


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    ap.add_argument("--i-know-this-restores-fake-prices", action="store_true",
                    help="确认已知隔离行是虚假价格，仍要还原（仅用于复查取证）")
    a = ap.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    if a.rollback:
        if not conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                            "AND name='price_snapshots_quarantine'").fetchone():
            print("隔离表不存在，无法回滚")
            return 1
        if not a.i_know_this_restores_fake_prices:
            print("""[拒绝回滚] 隔离表里是**已知虚假价格**：它们源自忽略 area 参数的 OPPO 遗留端点，
各国返回的其实都是中国大陆 CNY 价目表，却被标成了 AED/EUR/MXN/TRY。
还原回去会让比价页重新出现 37 倍假价差。
隔离行的作用是**留存证据**，不是待恢复的数据。
确要复查取证（例如比对原始响应），加 --i-know-this-restores-fake-prices 显式确认。
""")
            return 2
        n = conn.execute("SELECT COUNT(*) FROM price_snapshots_quarantine").fetchone()[0]
        cols = ",".join(QUARANTINE_COLS)
        conn.execute(f"INSERT OR IGNORE INTO price_snapshots ({cols}) "
                     f"SELECT {cols} FROM price_snapshots_quarantine")
        conn.commit()
        print(f"已从隔离表恢复 {n} 行到 price_snapshots（注意：这些是已知虚假价格）")
        return 0

    # ---- 预览 ----
    total = conn.execute("SELECT COUNT(*) FROM price_snapshots").fetchone()[0]
    bad = conn.execute("SELECT COUNT(*) FROM price_snapshots WHERE source_url LIKE ?",
                       (BAD,)).fetchone()[0]
    print(f"price_snapshots 总行数 = {total}")
    print(f"命中污染端点行数       = {bad}  ({bad / total * 100:.1f}%)")
    print("\n命中行的 source_url 去重（前 8 条）:")
    for r in conn.execute("SELECT source_url, COUNT(*) n FROM price_snapshots "
                          "WHERE source_url LIKE ? GROUP BY source_url LIMIT 8", (BAD,)):
        print(f"  n={r['n']:<5} {r['source_url'][:110]}")

    orphan = conn.execute("""
        SELECT COUNT(*) FROM parts p WHERE EXISTS (
            SELECT 1 FROM price_snapshots ps WHERE ps.part_id=p.id AND ps.source_url LIKE ?)
          AND NOT EXISTS (
            SELECT 1 FROM price_snapshots ps2 WHERE ps2.part_id=p.id AND ps2.source_url NOT LIKE ?)
    """, (BAD, BAD)).fetchone()[0]
    print(f"\n删后变为零快照的孤儿 parts = {orphan}")

    print("\n各机型级覆盖影响:")
    for r in conn.execute("""
        SELECT m.country_code cc, COUNT(DISTINCT m.id) n FROM models m
        JOIN parts p ON p.model_id=m.id JOIN price_snapshots ps ON ps.part_id=p.id
        WHERE ps.source_url LIKE ? GROUP BY cc ORDER BY cc""", (BAD,)):
        print(f"  {r['cc']:>3}  将失去数据的机型数 = {r['n']}")

    if not a.apply:
        print("\n[只读预览] 未做任何修改。确认无误后加 --apply 执行。")
        return 0

    # ---- 执行 ----
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = os.path.join(BACKUP_DIR, f"spare_parts_before_oppo_purge_{ts}.db")
    conn.close()
    shutil.copy2(DB, bak)
    print(f"\n已备份 DB -> {os.path.relpath(bak, os.path.dirname(DB))}")

    conn = sqlite3.connect(DB)
    cols = ",".join(QUARANTINE_COLS)
    cur = conn.cursor()

    def run(sql, args=()):
        """执行并返回**本次**影响行数。

        必须用 cursor.rowcount —— conn.total_changes 是「连接生命周期内累计值」，
        连续执行多条写语句时会越滚越大，打印出来会虚高（例如删 13836 行报成 41508），
        极易被误读成「删多了」而触发无谓的惊慌回滚。
        """
        cur.execute(sql, args)
        return cur.rowcount

    run(f"""CREATE TABLE IF NOT EXISTS price_snapshots_quarantine (
        {", ".join(c + " " + ("INTEGER PRIMARY KEY" if c == "id" else
                              ("REAL" if c in ("price", "cny_price", "material_fee", "labor_fee")
                               else "TEXT")) for c in QUARANTINE_COLS)},
        quarantine_reason TEXT, quarantined_at TEXT)""")
    moved = run(f"INSERT OR REPLACE INTO price_snapshots_quarantine ({cols}, "
                f"quarantine_reason, quarantined_at) "
                f"SELECT {cols}, ?, ? FROM price_snapshots WHERE source_url LIKE ?",
                (REASON, datetime.now().isoformat(timespec="seconds"), BAD))
    print(f"已隔离 {moved} 行 -> price_snapshots_quarantine")

    deleted = run("DELETE FROM price_snapshots WHERE source_url LIKE ?", (BAD,))
    print(f"已从 price_snapshots 删除 {deleted} 行")

    # 孤儿 parts 判定必须走隔离表：快照此时已从 price_snapshots 删除，
    # 用 price_snapshots 反查会恒为空、一行也删不掉（第一版踩过这个坑）。
    nparts = run("""
        DELETE FROM parts WHERE id IN (
            SELECT part_id FROM price_snapshots_quarantine)
          AND NOT EXISTS (SELECT 1 FROM price_snapshots ps WHERE ps.part_id=parts.id)""")
    print(f"已清理孤儿 parts {nparts} 个")

    # 同步更正前台提示文案。原文案把「抓取 bug」写成了「OPPO 的品牌定价特征」：
    # 「OPPO 官方全球价表对多数备件返回统一基准价…各有币种下显示相同数字」——
    # 实为遗留端点忽略 area、各国拿到的都是 CN 价目表所致。留着会误导用户以为数据正常。
    ncaveat = run("UPDATE brands SET price_caveat=? WHERE name='oppo'", (NEW_CAVEAT,))
    if ncaveat:
        print(f"已更新 brands.price_caveat（oppo）")

    conn.commit()
    left = conn.execute("SELECT COUNT(*) FROM price_snapshots").fetchone()[0]
    print(f"\n剩余 price_snapshots = {left}（清退前 {total}）")
    print("如需回滚：python tools/purge_oppo_area_param_pollution.py --rollback")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
