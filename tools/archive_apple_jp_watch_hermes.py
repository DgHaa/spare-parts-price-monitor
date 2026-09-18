"""把 apple/jp 手表页的「Hermès 误判」垃圾价行移入归档表（可逆）。

背景：executor 的 form_select_cascade 解析器用
`price_re = /[€$¥£]|円|AED|RM|MYR|TRY|EUR|JPY|CNY/i` 判断"这一行是不是价格行"。
其中 **`RM`（马来西亚林吉特）在 `re.I` 下命中了 "Hermès" 里的 "rm"**，于是：

    - 机型名行「Apple Watch Hermès Series 10 GPS + Cellular 46mm」被当成价格行
    - `extract_price` 从中抽出机型序号 → 价格 = 10.0 JPY（Series 10→10、Ultra 3→2 …）
    - 上一行机型名被当成部件名 → 出现「部件名=デバイスの種類」「部件名=另一个机型名」

影响：apple/jp watch 共 170 行（42 行「デバイスの種類」+ 128 行机型名当部件名），
其余 11 个「区域×品类」组合不受影响（只有 watch 页有 Hermès 机型）。

修法已在 executor 侧落地（币种代码加词首边界 `\b` + `(?![A-Za-z])`），
本工具只清理**已经落库的坏行**，不改数值、不猜修正值；干净数据已由抓链产出。

用法：
    python tools/archive_apple_jp_watch_hermes.py --dry-run
    python tools/archive_apple_jp_watch_hermes.py --execute
    python tools/archive_apple_jp_watch_hermes.py --restore
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "spare_parts.db"
ARCH = "price_snapshots_apple_jp_watch_hermes"
sys.stdout.reconfigure(encoding="utf-8")

# 判定为垃圾行的部件名：
#   1) 'デバイスの種類'（页面上的下拉标题，不是备件）
#   2) 以 'Apple Watch' 开头的（把机型名当成了部件名）
SEL = """SELECT ps.* FROM price_snapshots ps
         JOIN parts p ON p.id=ps.part_id
         JOIN models m ON m.id=p.model_id
         JOIN brands b ON b.id=m.brand_id
         WHERE b.name='apple' AND m.country_code='jp' AND m.category='watch'
           AND (p.name='デバイスの種類' OR p.name LIKE 'Apple Watch%')"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--restore", action="store_true")
    a = ap.parse_args()

    if not (a.dry_run or a.execute or a.restore):
        ap.error("需指定 --dry-run / --execute / --restore 之一")

    if a.dry_run or a.execute:
        c = sqlite3.connect(DB)
        c.row_factory = sqlite3.Row
        rows = c.execute(SEL).fetchall()
        print(f"命中垃圾价行 = {len(rows)}")
        from collections import Counter
        cnt = Counter()
        for r in rows:
            pid = r["part_id"]
            nm = c.execute("SELECT name FROM parts WHERE id=?", (pid,)).fetchone()[0]
            cnt[nm[:46]] += 1
        for nm, n in cnt.most_common():
            print(f"    {nm:<48} {n}")
        if a.dry_run:
            print("\n[dry-run] 未改动数据库")
            return
        shutil.copy2(DB, DB.with_suffix(f".db.bak_jpwatch_{datetime.now():%Y%m%d_%H%M%S}"))
        cols = [d[1] for d in c.execute(f"PRAGMA table_info({ARCH})")] if c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (ARCH,)).fetchone() else []
        if not cols:
            c.execute(f"CREATE TABLE {ARCH} AS SELECT * FROM price_snapshots WHERE 0")
        ids = [r["id"] for r in rows]
        c.executemany(f"INSERT INTO {ARCH} SELECT * FROM price_snapshots WHERE id=?",
                      [(i,) for i in ids])
        c.executemany("DELETE FROM price_snapshots WHERE id=?", [(i,) for i in ids])
        # 顺带清理因此产生的孤儿 parts（已无任何快照）
        c.execute("""DELETE FROM parts WHERE id IN (
                       SELECT p.id FROM parts p
                       JOIN models m ON m.id=p.model_id JOIN brands b ON b.id=m.brand_id
                       WHERE b.name='apple' AND m.country_code='jp' AND m.category='watch'
                         AND (p.name='デバイスの種類' OR p.name LIKE 'Apple Watch%')
                         AND NOT EXISTS (SELECT 1 FROM price_snapshots ps WHERE ps.part_id=p.id))""")
        c.commit()
        print(f"\n[execute] 已归档并删除 {len(ids)} 行 → 表 {ARCH}（备份已生成）")
        left = c.execute(SEL).fetchone()
        print(f"[verify] 剩余命中 = {'0' if left is None else '仍有!'}")

    if a.restore:
        c = sqlite3.connect(DB)
        n = c.execute(f"SELECT COUNT(*) FROM {ARCH}").fetchone()[0]
        c.execute(f"INSERT OR IGNORE INTO price_snapshots SELECT * FROM {ARCH}")
        c.commit()
        print(f"[restore] 已从 {ARCH} 恢复 {n} 行")


if __name__ == "__main__":
    main()
