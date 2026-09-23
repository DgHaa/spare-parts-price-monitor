"""test_run_log_status.py - run_logs 状态语义的隔离回归测试。

背景：2026-09-23 把 `skipped` 一拆为三（resumed / unavailable / skipped），
并把「自动发现 0 机型」由 skipped 改判 failed。本测试在不碰真实库的前提下
（全部写入**临时库**）钉住四条判据：

  1) 无 KB 记录                      → skipped（未收录）
  2) 多品类汇总优先级                → failed > partial > success > resumed > unavailable > skipped
  3) 「自动发现 0 机型」             → failed 且 anomaly_flag=1（原先误记 skipped + anomaly=0）
  4) 纯 unavailable 的多品类区域     → unavailable（不再退化成 skipped）

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
