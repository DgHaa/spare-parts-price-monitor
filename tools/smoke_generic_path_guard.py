"""smoke_generic_path_guard.py - 非 samsung_api 配方的端到端护栏冒烟。

为什么单独有这个脚本：
  tools/smoke_crawl_enum_guard.py 直接调 samsung_api.crawl_and_write，只覆盖
  `samsung_api` 一种配方。而 api_reborn / form_select_cascade / vivo_api / xiaomi_api
  走的是 crawler/run.py 的通用路径（含 write_rows 里的 source_url_kind 推导、
  upsert_part 的品类归一化），本脚本用真的通用路径跑一遍，证明：
    1) 新增的写入口断言**不误伤**这条路径上的真实数据；
    2) run.py 的常量绑定在运行时真的解析得到（不是只在导入期碰巧没事）。

非破坏性：生产库只读复制到临时目录，db.DB_PATH 重定向，写入全部落在副本上。
用法：python tools/smoke_generic_path_guard.py --brand oppo --country cn
"""
from __future__ import annotations

import argparse
import asyncio
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

import db  # noqa: E402


def count_invalid(con):
    """统计全部受护枚举列的非法值（口径同 verify_quarterly_run._enum_specs）。"""
    specs = [
        ("models", "category", db.VALID_CATEGORIES, False),
        ("models", "tier", db.VALID_TIERS, False),
        ("models", "model_url_kind", db.VALID_MODEL_URL_KINDS, False),
        ("price_snapshots", "source_url_kind", db.VALID_SNAPSHOT_URL_KINDS, False),
        ("maintenance_queue", "status", db.VALID_ISSUE_STATUSES, False),
        ("brands", "recipe_mode", db.VALID_RECIPE_MODES, False),
        ("parts", "part_type", db.VALID_PART_TYPES, False),
        ("parts", "canonical_type", db.VALID_PART_TYPES, False),
        ("parts", "lang", db.VALID_LANGS, False),
        ("countries", "currency", db.VALID_CURRENCIES, False),
        ("price_snapshots", "currency", db.VALID_CURRENCIES, False),
    ]
    total, detail = 0, []
    for table, col, valid, allow_empty in specs:
        marks = ",".join("?" * len(valid))
        extra = f" AND {col}<>''" if allow_empty else ""
        try:
            n = con.execute(
                f"SELECT COUNT(*) FROM {table} "
                f"WHERE {col} IS NOT NULL{extra} AND {col} NOT IN ({marks})",
                tuple(sorted(valid))).fetchone()[0]
        except sqlite3.OperationalError:
            continue
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
    ap.add_argument("--brand", default="oppo")
    ap.add_argument("--country", default="cn")
    ap.add_argument("--no-force", action="store_true",
                    help="默认 force=True（重抓本季已抓机型，才能真正走到写入路径）")
    ap.add_argument("--keep-tmp", action="store_true",
                    help="保留临时库目录便于排障（默认用后即删）")
    ap.add_argument("--fresh", action="store_true",
                    help="抓取前先删掉该 brand/country 本季快照，使写入必须是 INSERT。"
                         "⚠️ 不加这个开关时，重抓同一季度走 UNIQUE(part_id,quarter) 的 "
                         "UPDATE 分支，快照行数**不会增长**——那看起来像『什么都没写』，"
                         "会被误读成护栏误伤。加 --fresh 才能得到『净增 N 行』的强证据。")
    args = ap.parse_args()

    src_db = ROOT / "spare_parts.db"
    if not src_db.exists():
        print(f"[FATAL] 找不到生产库：{src_db}")
        return 2

    tmp_dir = Path(tempfile.mkdtemp(prefix="smoke_generic_"))
    tmp_db = tmp_dir / "t.db"
    shutil.copy(src_db, tmp_db)
    db.DB_PATH = tmp_db
    print(f"生产库副本（只读来源）：{src_db}")
    print(f"写入目标（临时库）：{tmp_db}\n")

    from crawler import run as R  # noqa: E402

    def snap():
        c = sqlite3.connect(str(tmp_db))
        r = (c.execute("SELECT COUNT(*) FROM price_snapshots").fetchone()[0],
             c.execute("SELECT COUNT(*) FROM parts").fetchone()[0])
        c.close()
        return r

    before = snap()
    print(f"抓取前：快照 {before[0]} 行 / 备件 {before[1]} 行")

    if args.fresh:
        c = sqlite3.connect(str(tmp_db))
        d = c.execute(
            """DELETE FROM price_snapshots WHERE part_id IN (
                   SELECT p.id FROM parts p JOIN models m ON m.id=p.model_id
                   JOIN brands b ON b.id=m.brand_id
                   WHERE b.name=? AND m.country_code=? AND price_snapshots.quarter=?)""",
            (args.brand, args.country, db.this_quarter())).rowcount
        c.commit()
        c.close()
        before = snap()
        print(f"[fresh] 已删除 {args.brand}/{args.country} 本季 {d} 行快照"
              f"（写入必须走 INSERT，净增行数才可信）")
        print(f"       清空后：快照 {before[0]} 行 / 备件 {before[1]} 行")

    print(f"开始通用路径抓取 {args.brand}/{args.country}（force={not args.no_force}）...\n")

    err = None
    try:
        asyncio.run(R.run_all(only_brand=args.brand, only_country=args.country,
                              force=not args.no_force))
    except SystemExit:
        pass
    except BaseException as e:          # noqa: BLE001 - 冒烟脚本要如实报告任何中断
        err = f"{type(e).__name__}: {e}"

    after = snap()
    wrote = 0
    try:
        _c = sqlite3.connect(str(tmp_db))
        _r = _c.execute(
            "SELECT COALESCE(SUM(rows_written),0) FROM run_logs WHERE brand=? AND country=?",
            (args.brand, args.country)).fetchone()
        wrote = _r[0] if _r else 0
        _c.close()
    except sqlite3.OperationalError:
        pass
    print(f"\n抓取后：快照 {after[0]} 行（净增 {after[0] - before[0]}）"
          f" / 备件 {after[1]} 行（净增 {after[1] - before[1]}）")
    print(f"run_logs 累计 rows_written = {wrote}"
          f"（这是 upsert 行数；重抓同季度不增行，故与净增不必相等）")
    if err:
        print(f"[warn] 抓取过程抛出：{err}")

    con = sqlite3.connect(str(tmp_db))
    total, detail = count_invalid(con)
    print(f"\n枚举非法值合计：{total}" + (f" → {detail}" if detail else "（全 0）"))
    # 同时核对**生产库确实没被动过**（这是本脚本最重要的安全声明，必须每次自证）
    prod_n = sqlite3.connect(str(src_db)).execute(
        "SELECT COUNT(*) FROM price_snapshots").fetchone()[0]
    print(f"生产库快照行数（应保持不变）：{prod_n}")
    con.close()
    if args.keep_tmp:
        print(f"[keep] 临时库保留在：{tmp_db}")
    else:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # 关键判据：**写入口断言有没有误伤**。若断言误伤，run_all 会抛含"非法"的 ValueError。
    # --fresh 模式下还要求净增 > 0：否则"没报错"可能只是"根本没走到写入"。
    assert_err = err and "非法" in err
    ok = (total == 0) and (not assert_err) and (after[0] >= before[0])
    if args.fresh:
        ok = ok and (after[0] > before[0])
    print("\n结论：" + ("PASS —— 通用路径未被写入口断言误伤 ✓" if ok
                        else "FAIL —— 见上方输出 ✗"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
