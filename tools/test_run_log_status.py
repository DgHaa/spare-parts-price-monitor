"""test_run_log_status.py - run_logs 状态语义的隔离回归测试。

背景：2026-09-23 把 `skipped` 一拆为三（resumed / unavailable / skipped），
并把「自动发现 0 机型」由 skipped 改判 failed。本测试在不碰真实库的前提下
（全部写入**临时库**）钉住四条判据：

  1) 无 KB 记录                      → skipped（未收录）
  2) 多品类汇总优先级                → failed > partial > success > resumed > unavailable > skipped
  3) 「自动发现 0 机型」             → failed 且 anomaly_flag=1（原先误记 skipped + anomaly=0）
  4) 纯 unavailable 的多品类区域     → unavailable（不再退化成 skipped）
  5) 状态词汇表加固                  → VALID_STATUSES 六值；log_run 拦非法值且不落库；
                                       summarize_status 边界；巡检白名单能抓非法、不误报合法

用法：python tools/test_run_log_status.py        # 全绿 exit 0，否则 exit 1
"""
from __future__ import annotations

import asyncio
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

import db  # noqa: E402

FAILS: list[str] = []


def check(name: str, got, want):
    ok = got == want
    print(f"  {'✓' if ok else '✗'} {name}: got={got!r} want={want!r}")
    if not ok:
        FAILS.append(name)


class FakePage:
    """最小 page 替身：只需异步 close()。"""

    def __init__(self):
        self.closed = 0

    async def close(self):
        self.closed += 1


def latest_status(con, brand, country):
    r = con.execute("SELECT status, rows_written, anomaly_flag FROM run_logs "
                    "WHERE brand=? AND country=? ORDER BY id DESC LIMIT 1",
                    (brand, country)).fetchone()
    return r


def setup_temp_db() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="spm_test_")) / "t.db"
    db.DB_PATH = tmp
    db.init_db()
    return tmp


def main() -> int:
    tmp = setup_temp_db()
    print(f"临时库：{tmp}\n")

    import crawler.run as R

    quarter = datetime.now().strftime("%Y") + "Q3"

    # ---- 1) 无 KB 记录 → skipped -------------------------------------------
    print("[1] 无 KB 记录 → skipped")
    asyncio.run(R.crawl_brand_country(None, "__no_such_brand__", "cn", "测试国",
                                      None, quarter))
    con = sqlite3.connect(str(db.DB_PATH))
    con.row_factory = sqlite3.Row
    r = latest_status(con, "__no_such_brand__", "cn")
    check("无 KB 记录 status", r["status"], "skipped")
    check("无 KB 记录 anomaly_flag", r["anomaly_flag"], 0)
    con.close()

    # ---- 2) 多品类汇总优先级 ------------------------------------------------
    print("\n[2] 多品类汇总优先级（apple/cn 有 phone/tablet/watch 三条记录）")
    real_cbc = R.crawl_brand_country
    scen = [
        (["unavailable", "unavailable", "unavailable"], "unavailable"),
        (["resumed", "resumed", "resumed"], "resumed"),
        (["resumed", "unavailable", "unavailable"], "resumed"),   # 混：取信息量大的
        (["failed", "success", "resumed"], "failed"),
        (["partial", "success", "resumed"], "partial"),
        (["success", "resumed", "resumed"], "success"),
        (["skipped", "skipped", "skipped"], "skipped"),
    ]
    for statuses, want in scen:
        seq = list(statuses)

        async def fake_cbc(browser, brand, country, *a, **k):
            return (seq.pop(0), 0, "stub")

        R.crawl_brand_country = fake_cbc
        asyncio.run(R.crawl_brand_country_all(None, "apple", "cn", "中国", None, quarter))
        con = sqlite3.connect(str(db.DB_PATH))
        con.row_factory = sqlite3.Row
        r = latest_status(con, "apple", "cn")
        con.close()
        check(f"汇总 {statuses} →", r["status"], want)
    R.crawl_brand_country = real_cbc

    # ---- 3) 自动发现 0 机型 → failed + anomaly=1 ---------------------------
    print("\n[3] 「自动发现 0 机型」→ failed 且 anomaly_flag=1")
    real_open, real_disc = R.open_page, R.discover_models
    fake_page = FakePage()

    async def fake_open(browser, rec, goto_url=None):
        return fake_page

    async def fake_disc(page, rec):
        return []

    R.open_page, R.discover_models = fake_open, fake_disc
    try:
        asyncio.run(R.crawl_brand_country(None, "apple", "cn", "中国", None, quarter))
    finally:
        R.open_page, R.discover_models = real_open, real_disc
    con = sqlite3.connect(str(db.DB_PATH))
    con.row_factory = sqlite3.Row
    r = latest_status(con, "apple", "cn")
    con.close()
    check("0 机型 status", r["status"], "failed")
    check("0 机型 anomaly_flag", r["anomaly_flag"], 1)
    check("0 机型 page 已关闭", fake_page.closed >= 1, True)

    # ---- 4) unavailable 区域不被误判 ---------------------------------------
    print("\n[4] KB 标记 unavailable 的区域 → unavailable")
    asyncio.run(R.crawl_brand_country(None, "xiaomi", "de", "德国", None, quarter))
    con = sqlite3.connect(str(db.DB_PATH))
    con.row_factory = sqlite3.Row
    r = latest_status(con, "xiaomi", "de")
    con.close()
    check("unavailable 区域 status", r["status"], "unavailable")
    check("unavailable 区域 anomaly_flag", r["anomaly_flag"], 0)

    # ---- 5) 状态词汇表加固（应用层枚举 + 写入口断言 + 巡检兜底）------------
    print("\n[5] 状态词汇表加固")
    check("VALID_STATUSES 数量", len(db.VALID_STATUSES), 6)
    check("VALID_STATUSES 内容", set(db.VALID_STATUSES),
          {"success", "partial", "failed", "resumed", "unavailable", "skipped"})
    check("STATUS_PRIORITY 与 VALID_STATUSES 等集",
          set(db.STATUS_PRIORITY), set(db.VALID_STATUSES))
    check("NEUTRAL_STATUSES ⊆ VALID_STATUSES",
          set(db.NEUTRAL_STATUSES) <= set(db.VALID_STATUSES), True)

    # 5a) 合法值一律放行
    for s in sorted(db.VALID_STATUSES):
        try:
            db.validate_status(s)
            ok = True
        except ValueError:
            ok = False
        check(f"validate_status({s!r}) 放行", ok, True)

    # 5b) 非法值必须抛 ValueError（fail fast）；能纠错时给出提示
    for bad, hint in [("succes", "success"), ("Success", "success"),
                      ("resummed", "resumed"), ("", None), (None, None)]:
        try:
            db.validate_status(bad)
            check(f"validate_status({bad!r}) 抛 ValueError", False, True)
        except ValueError as e:
            check(f"validate_status({bad!r}) 抛 ValueError", True, True)
            if hint:
                check(f"  纠错提示含 {hint!r}", hint in str(e), True)

    # 5c) log_run 是 run_logs 唯一写入口：非法状态必须在此拦下，且不落库
    con = sqlite3.connect(str(db.DB_PATH))
    before = con.execute("SELECT COUNT(*) FROM run_logs").fetchone()[0]
    try:
        db.log_run("__vocab__", "cn", quarter, "t0", "t1", "succes", 0)
        check("log_run 非法状态被拦截", False, True)
    except ValueError as e:
        check("log_run 非法状态被拦截", True, True)
        check("  异常信息含『非法 run_logs.status』", "非法 run_logs.status" in str(e), True)
    after = con.execute("SELECT COUNT(*) FROM run_logs").fetchone()[0]
    check("非法写入未落库（行数不变）", after, before)
    # 合法值仍可正常写入
    db.log_run("__vocab__", "cn", quarter, "t0", "t1", db.STATUS_RESUMED, 0)
    row = con.execute("SELECT status FROM run_logs WHERE brand='__vocab__'").fetchone()
    check("合法状态正常落库", row[0], db.STATUS_RESUMED)
    con.close()

    # 5d) summarize_status 边界语义
    check("summarize_status([]) 回退默认", db.summarize_status([]), db.STATUS_SKIPPED)
    check("全 unavailable → unavailable",
          db.summarize_status([db.STATUS_UNAVAILABLE] * 3), db.STATUS_UNAVAILABLE)
    check("unavailable+skipped 混合 → skipped（不把缺口说成『对方不提供』）",
          db.summarize_status([db.STATUS_UNAVAILABLE, db.STATUS_SKIPPED]), db.STATUS_SKIPPED)

    # 5e) 巡检白名单 = 软约束的可观测兜底：能抓非法、且不误报合法
    sys.path.insert(0, str(ROOT / "tools"))
    import verify_quarterly_run as V  # noqa: E402
    con = sqlite3.connect(str(db.DB_PATH))
    con.row_factory = sqlite3.Row
    con.execute("UPDATE run_logs SET status='succes' WHERE brand='__vocab__'")
    con.commit()
    rep_bad = V.Report()
    V.check_status_vocabulary(con, rep_bad)
    check("巡检捕获非法状态", any(c == "STATUS_INVALID" for _, c, _ in rep_bad.rows), True)
    check("巡检判为 ERROR（非仅 WARN）", rep_bad.has_error(), True)

    con.execute("UPDATE run_logs SET status=? WHERE brand='__vocab__'", (db.STATUS_RESUMED,))
    con.commit()
    rep_ok = V.Report()
    V.check_status_vocabulary(con, rep_ok)
    check("合法数据巡检通过（无 STATUS_INVALID 误报）",
          any(c == "STATUS_INVALID" for _, c, _ in rep_ok.rows), False)
    con.close()

    # 收尾：删临时目录
    shutil.rmtree(tmp.parent, ignore_errors=True)

    print("\n" + "=" * 70)
    if FAILS:
        print(f"结果：{len(FAILS)} 项未通过 ✗ → {FAILS}")
        return 1
    print("结果：全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
