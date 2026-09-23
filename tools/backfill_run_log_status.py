"""backfill_run_log_status.py - 把历史 run_logs 里的 skipped 按证据重新归类。

== 为什么要做 ==

2026-09-23 之前，`run_logs.status` 把三种完全不同的情况都记成 `skipped`：

    resumed      断点续跑：本季机型均已抓，本轮无新增      → 正常，有数据
    unavailable  官网不提供备件价 / 需真机代理             → 官方无价，非我方缺口
    skipped      未收录（无 KB 记录）                      → 配置缺失

后果：事后审计分不清"正常续跑"和"该区域根本没数据"，界面也只能统统显示"跳过"。
新代码（crawler/run.py、crawler/samsung_api.py）已分别写入 `resumed` / `unavailable`；
本工具把**存量历史行**按同样判据补齐，让覆盖度与运行历史从今天起口径一致。

== 判据（顺序即 crawler 里的短路顺序）==

    1) KB 中该 brand×country 的记录**全部** blocked/unavailable → unavailable
       （crawler 在取价之前就短路了，所以这一条优先于"有没有价行"）
    2) 该 brand×country 在该季度**有价行**                    → resumed
    3) 其余（无 KB 记录 / 判不出来）                          → 保持 skipped 不动

刻意**不改** `anomaly_flag`：monitor.py 会对"最新一次运行且 failed/partial 且
anomaly_flag=1"的区域补写待修工单，回填历史 anomaly 会凭空造出一批工单。

用法：
  python tools/backfill_run_log_status.py            # 默认只读：打印归类计划与计数
  python tools/backfill_run_log_status.py --apply    # 先自动备份，再写库
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vendor"))
sys.path.insert(0, str(ROOT))

from db import STATUS_RESUMED, STATUS_SKIPPED, STATUS_UNAVAILABLE  # noqa: E402

DB = ROOT / "spare_parts.db"
BACKUP_DIR = ROOT / "backups"

# ⚠️ 这是 **KB 配方的 status**，与 db.STATUS_*（run_logs.status）是两套不同词汇表，
# 取值只是恰好同名。勿合并——否则会把"官网不提供"与"KB 标记不可用"两个语义耦合。
KB_NOT_AVAILABLE = ("blocked", "unavailable")


def kb_region_unavailable(brand: str, country: str) -> bool:
    """KB 里该区域的所有记录是否都是人工研判的「拿不到」。"""
    try:
        import executor  # vendor/executor.py（运行时的真源）
        recs = executor.load_records(brand, country) or []
    except Exception:
        return False
    if not recs:
        return False
    st = {r.get("status") for r in recs}
    return bool(st) and st <= set(KB_NOT_AVAILABLE)


def plan(con: sqlite3.Connection):
    """返回 [(id, brand, country, quarter, old, new, why)]，只含需要变更的行。"""
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, brand, country, quarter, rows_written, error_text "
        "FROM run_logs WHERE status=? ORDER BY brand, country, id",
        (STATUS_SKIPPED,)).fetchall()

    out = []
    # 缓存，避免同一 brand×country 反复查库/读 KB
    unav_cache: dict[tuple, bool] = {}
    hasprice_cache: dict[tuple, int] = {}

    for r in rows:
        key = (r["brand"], r["country"])
        if key not in unav_cache:
            unav_cache[key] = kb_region_unavailable(r["brand"], r["country"])
        if unav_cache[key]:
            out.append((r["id"], r["brand"], r["country"], r["quarter"],
                        STATUS_SKIPPED, STATUS_UNAVAILABLE,
                        "KB 研判该区域官网不提供备件价"))
            continue

        pkey = (r["brand"], r["country"], r["quarter"])
        if pkey not in hasprice_cache:
            hasprice_cache[pkey] = con.execute(
                """SELECT COUNT(*) FROM price_snapshots ps
                   JOIN parts p ON p.id = ps.part_id
                   JOIN models m ON m.id = p.model_id
                   JOIN brands b ON b.id = m.brand_id
                   WHERE b.name=? AND m.country_code=? AND ps.quarter=?""",
                (r["brand"], r["country"], r["quarter"])).fetchone()[0]
        if hasprice_cache[pkey] > 0:
            out.append((r["id"], r["brand"], r["country"], r["quarter"],
                        STATUS_SKIPPED, STATUS_RESUMED,
                        f"该季度已有 {hasprice_cache[pkey]} 条价行 → 属断点续跑"))
        # 其余保持 skipped（未收录 / 判不出来），不动
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="写库（默认只读预览）")
    ap.add_argument("--db", default=str(DB))
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"[FATAL] 找不到数据库：{args.db}")
        return 2

    con = sqlite3.connect(args.db)
    changes = plan(con)
    total_skipped = con.execute(
        "SELECT COUNT(*) FROM run_logs WHERE status=?", (STATUS_SKIPPED,)).fetchone()[0]
    before_dist = con.execute(
        "SELECT status, COUNT(*) FROM run_logs GROUP BY status ORDER BY status").fetchall()
    con.close()

    print("当前 run_logs 状态分布：")
    for st, n in before_dist:
        print(f"  {st:<12} {n}")
    print()
    print(f"历史 status='skipped' 共 {total_skipped} 行；拟重新归类 {len(changes)} 行：")
    agg: dict[tuple, int] = {}
    for _id, b, c, _q, old, new, _w in changes:
        agg[(new,)] = agg.get((new,), 0) + 1
    for (new,), n in sorted(agg.items()):
        print(f"  skipped → {new:<12} {n} 行")
    print(f"  保持 skipped 不动（未收录/判不出来）  {total_skipped - len(changes)} 行")

    by_scope: dict[tuple, int] = {}
    for _id, b, c, _q, _o, new, _w in changes:
        by_scope[(b, c, new)] = by_scope.get((b, c, new), 0) + 1
    print("\n明细（brand/country → 新状态 × 行数）：")
    for (b, c, new), n in sorted(by_scope.items()):
        print(f"  {b:<9}{c:<5}→ {new:<12} {n}")

    if not args.apply:
        con.close()
        print("\n（只读预览，未写库；加 --apply 生效，会先自动备份）")
        return 0

    con.close()
    # 写库前自动备份（整库在线备份）
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = BACKUP_DIR / f"spare_parts_before_runlog_status_backfill_{ts}.db"
    src = sqlite3.connect(args.db)
    out = sqlite3.connect(str(dst))
    with out:
        src.backup(out)
    src.close()
    out.close()
    print(f"\n[backup] {dst}")

    con = sqlite3.connect(args.db)
    try:
        with con:
            for _id, _b, _c, _q, _o, new, _w in changes:
                con.execute("UPDATE run_logs SET status=? WHERE id=? AND status=?",
                            (new, _id, STATUS_SKIPPED))
        # 复核
        left = con.execute("SELECT status, COUNT(*) FROM run_logs GROUP BY status").fetchall()
    finally:
        con.close()
    print("[done] 写库完成，当前 run_logs 状态分布：")
    for st, n in sorted(left):
        print(f"  {st:<12} {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
