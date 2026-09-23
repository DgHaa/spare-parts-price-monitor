"""smoke_crawl_enum_guard.py - 真实抓取 × 临时库：验证枚举写入口断言不会误伤真数据。

== 为什么要这个 ==

2026-09-23 给一批枚举列加了"写入口断言"（db.validate_category / validate_tier /
validate_model_url_kind ...）。断言的价值在于**能拦下写错的值**，但风险也在这里：
如果某条真实抓取链路传入的恰好是白名单之外的值，断言会直接抛异常，
把整条链路打断 —— 那是比"写错值"更严重的回归。

"核对过取值来源都合法"是**静态**证据，不够。本工具给**动态**证据：
把 db.DB_PATH 指向真实库的**临时副本**，跑一次真实的抓取链路，
再断言两件事：
  1) 抓取正常完成（没被断言打断）
  2) 抓完后临时库里各枚举列的非法值计数全为 0

全程不碰生产库（只读它做副本），可安全重复运行。

== 用法 ==

    python tools/smoke_crawl_enum_guard.py                 # 默认 samsung/cn（API 直采，最快）
    python tools/smoke_crawl_enum_guard.py --brand samsung --country de

注意：需要能访问对应品牌官网；离线环境下会因网络失败而退出（不代表断言有问题）。
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

import db  # noqa: E402


def enum_checks():
    return [
        ("models", "category", db.VALID_CATEGORIES),
        ("models", "tier", db.VALID_TIERS),
        ("models", "model_url_kind", db.VALID_MODEL_URL_KINDS),
        ("price_snapshots", "source_url_kind", db.VALID_SNAPSHOT_URL_KINDS),
        ("maintenance_queue", "status", db.VALID_ISSUE_STATUSES),
        ("brands", "recipe_mode", db.VALID_RECIPE_MODES),
    ]


def count_invalid(con):
    total, detail = 0, []
    for table, col, valid in enum_checks():
        marks = ",".join("?" * len(valid))
        n = con.execute(
            f"SELECT COUNT(*) FROM {table} "
            f"WHERE {col} IS NOT NULL AND {col} NOT IN ({marks})",
            tuple(sorted(valid))).fetchone()[0]
        # rate_source 是前缀模式
        total += n
        if n:
            detail.append(f"{table}.{col}={n}")
    for table in ("exchange_rates", "price_snapshots"):
        n = con.execute(
            f"SELECT COUNT(*) FROM {table} WHERE rate_source IS NOT NULL "
            f"AND rate_source<>? AND rate_source NOT LIKE ?",
            (db.RATE_SOURCE_STATIC, db.RATE_SOURCE_LIVE_PREFIX + "%")).fetchone()[0]
        total += n
        if n:
            detail.append(f"{table}.rate_source={n}")
    return total, detail


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--brand", default="samsung")
    ap.add_argument("--country", default="cn")
    args = ap.parse_args()

    src_db = ROOT / "spare_parts.db"
    if not src_db.exists():
        print(f"[FATAL] 找不到生产库：{src_db}")
        return 2

    tmp_dir = Path(tempfile.mkdtemp(prefix="smoke_crawl_enum_"))
    tmp_db = tmp_dir / "t.db"
    shutil.copy(src_db, tmp_db)
    db.DB_PATH = tmp_db          # 关键：把整个 db 层的写入重定向到临时副本
    print(f"生产库副本（只读来源）：{src_db}")
    print(f"写入目标（临时库）：{tmp_db}\n")

    from crawler import samsung_api as SA  # noqa: E402

    def snap():
        c = sqlite3.connect(str(tmp_db))
        r = (c.execute("SELECT COUNT(*) FROM price_snapshots").fetchone()[0],
             c.execute("SELECT COUNT(*) FROM models").fetchone()[0])
        c.close()
        return r

    before = snap()
    rec = SA.load_rec(args.brand, args.country)
    if not rec:
        print(f"[skip] {args.brand}/{args.country} 无 KB 记录")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return 2
    mode = (rec.get("query") or {}).get("mode")
    print(f"KB 记录 mode = {mode}")

    status, n, reason = SA.crawl_and_write(args.brand, args.country, args.country,
                                           rec, force=True)
    after = snap()

    print(f"\n抓取结果：status={status} 写入={n} 条"
          + (f" | {reason}" if reason else ""))
    print(f"快照行数：{before[0]} -> {after[0]}（机型 {before[1]} -> {after[1]}）")

    con = sqlite3.connect(str(tmp_db))
    print("\n抓取后枚举非法值计数（应全为 0）：")
    for table, col, valid in enum_checks():
        marks = ",".join("?" * len(valid))
        n_bad = con.execute(
            f"SELECT COUNT(*) FROM {table} "
            f"WHERE {col} IS NOT NULL AND {col} NOT IN ({marks})",
            tuple(sorted(valid))).fetchone()[0]
        print(f"   {table + '.' + col:<36} {n_bad}")
    total, detail = count_invalid(con)
    print(f"   {'合计（含 rate_source 前缀列）':<36} {total}"
          + (f" → {detail}" if detail else ""))
    con.close()
    shutil.rmtree(tmp_dir, ignore_errors=True)

    ok = total == 0 and status in (db.STATUS_SUCCESS, db.STATUS_PARTIAL, db.STATUS_RESUMED)
    print("\n结论：" + ("PASS —— 真实抓取未被写入口断言误伤 ✓" if ok
                        else "FAIL —— 见上方输出 ✗"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
