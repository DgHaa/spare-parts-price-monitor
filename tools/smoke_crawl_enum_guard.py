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
    """(表, 列, 合法集合, 是否允许空串)。与 tools/verify_quarterly_run._enum_specs 同源口径。"""
    return [
        ("models", "category", db.VALID_CATEGORIES, False),
        ("models", "tier", db.VALID_TIERS, False),
        ("models", "model_url_kind", db.VALID_MODEL_URL_KINDS, False),
        ("price_snapshots", "source_url_kind", db.VALID_SNAPSHOT_URL_KINDS, False),
        ("maintenance_queue", "status", db.VALID_ISSUE_STATUSES, False),
        ("brands", "recipe_mode", db.VALID_RECIPE_MODES, False),
        # 第三轮：部件品类（比价分组键）/ 语种 / 币种
        ("parts", "part_type", db.VALID_PART_TYPES, False),
        ("parts", "canonical_type", db.VALID_PART_TYPES, False),
        ("parts", "lang", db.VALID_LANGS, False),
        ("parts", "norm_rule", db.VALID_NORM_RULES, False),
        ("part_alias", "part_type", db.VALID_PART_TYPES, False),
        ("part_alias", "canonical_type", db.VALID_PART_TYPES, True),
        ("countries", "currency", db.VALID_CURRENCIES, False),
        ("price_snapshots", "currency", db.VALID_CURRENCIES, False),
    ]


def _invalid_one(con, table, col, valid, allow_empty):
    marks = ",".join("?" * len(valid))
    extra = f" AND {col}<>''" if allow_empty else ""
    return con.execute(
        f"SELECT COUNT(*) FROM {table} "
        f"WHERE {col} IS NOT NULL{extra} AND {col} NOT IN ({marks})",
        tuple(sorted(valid))).fetchone()[0]


def count_invalid(con):
    total, detail = 0, []
    for table, col, valid, allow_empty in enum_checks():
        n = _invalid_one(con, table, col, valid, allow_empty)
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
    ap.add_argument("--fresh", action="store_true",
                    help="抓取前先删掉该 brand/country 本季快照。"
                         "不加时重抓同季度走 UNIQUE(part_id,quarter) 的 UPDATE 分支，"
                         "快照行数不会增长（看起来像『什么都没写』，易被误读为护栏误伤）；"
                         "加 --fresh 才能得到『净增 N 行』的强证据。")
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
    if args.fresh:
        c = sqlite3.connect(str(tmp_db))
        c.execute(
            """DELETE FROM price_snapshots WHERE part_id IN (
                   SELECT p.id FROM parts p JOIN models m ON m.id=p.model_id
                   JOIN brands b ON b.id=m.brand_id
                   WHERE b.name=? AND m.country_code=? AND price_snapshots.quarter=?)""",
            (args.brand, args.country, db.this_quarter()))
        c.commit()
        c.close()
        before = snap()
        print(f"[fresh] 已清空 {args.brand}/{args.country} 本季快照 → {before[0]} 行")
    rec = SA.load_rec(args.brand, args.country)
    if not rec:
        print(f"[skip] {args.brand}/{args.country} 无 KB 记录")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return 2
    mode = (rec.get("query") or {}).get("mode")
    print(f"KB 记录 mode = {mode}")

    # 本冒烟工具只覆盖 samsung_api 配方（它不依赖浏览器、可在本机直接跑通）。
    # 其他配方（api_reborn / form_select_cascade / vivo_api / xiaomi_api）走
    # crawler/run.py 的通用路径，需要 Playwright 与真实网络，请用：
    #     python -m crawler.run --brand <b> --country <cc>
    # 或直接跑 tools/test_enum_vocabularies.py 的静态绑定断言。
    # ⚠️ 不能"顺手"用 samsung_api 去跑非 samsung_api 的 KB 记录：会因类型不匹配
    # 报出『未知 samsung_api 类型: None』并返回 failed——那会被误读成护栏误伤。
    if mode != "samsung_api":
        print(f"[skip] {args.brand}/{args.country} 的 mode={mode!r} 不在本工具覆盖范围"
              f"（仅 samsung_api）。请改用 `python -m crawler.run --brand "
              f"{args.brand} --country {args.country}` 做端到端冒烟。")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return 2

    status, n, reason = SA.crawl_and_write(args.brand, args.country, args.country,
                                           rec, force=True)
    after = snap()

    print(f"\n抓取结果：status={status} 写入={n} 条"
          + (f" | {reason}" if reason else ""))
    print(f"快照行数：{before[0]} -> {after[0]}（净增 {after[0] - before[0]}；"
          f"机型 {before[1]} -> {after[1]}）")
    print("注：无 --fresh 时重抓同季度走 UPDATE，净增为 0 属预期，不代表没写。")

    con = sqlite3.connect(str(tmp_db))
    print("\n抓取后枚举非法值计数（应全为 0）：")
    for table, col, valid, allow_empty in enum_checks():
        n_bad = _invalid_one(con, table, col, valid, allow_empty)
        print(f"   {table + '.' + col:<36} {n_bad}")
    total, detail = count_invalid(con)
    print(f"   {'合计（含 rate_source 前缀列）':<36} {total}"
          + (f" → {detail}" if detail else ""))
    con.close()
    shutil.rmtree(tmp_dir, ignore_errors=True)

    ok = total == 0 and status in (db.STATUS_SUCCESS, db.STATUS_PARTIAL, db.STATUS_RESUMED)
    if args.fresh:
        ok = ok and (after[0] > before[0])   # 净增 > 0 才证明写入真的走通了
    print("\n结论：" + ("PASS —— 真实抓取未被写入口断言误伤 ✓" if ok
                        else "FAIL —— 见上方输出 ✗"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
