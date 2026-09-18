"""把 apple/de 的旧快照移入归档表，以便用修好的金额解析器重抓。

背景：`normalize.parse_amount` 旧实现无条件 `replace(",", "")`，把德语小数逗号
当成千分位，使 `€488,99` 被读成 48899（**放大 100 倍**）。库中 apple/de 因此存在
离谱值（如 iPhone 17「其他损坏」€72899 = ¥569406）。

本工具只做"可逆归档 + 删除"，不改数值、不猜修正倍数；真实数据由随后的重抓产出。

用法：
    python tools/archive_apple_de_badfix.py --dry-run
    python tools/archive_apple_de_badfix.py --execute
    python tools/archive_apple_de_badfix.py --restore
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
ARCH = "price_snapshots_apple_de_badfix"
sys.stdout.reconfigure(encoding="utf-8")


def sel(c):
    return """SELECT ps.* FROM price_snapshots ps
              JOIN parts p ON p.id=ps.part_id
              JOIN models m ON m.id=p.model_id
              JOIN brands b ON b.id=m.brand_id
              WHERE b.name='apple' AND m.country_code='de'"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--restore", action="store_true")
    a = ap.parse_args()

    c = sqlite3.connect(DB)
    n = c.execute(f"SELECT COUNT(*) FROM ({sel(c)})").fetchone()[0]
    models = c.execute(f"""SELECT COUNT(DISTINCT m.id) FROM price_snapshots ps
        JOIN parts p ON p.id=ps.part_id JOIN models m ON m.id=p.model_id
        JOIN brands b ON b.id=m.brand_id WHERE b.name='apple' AND m.country_code='de'""").fetchone()[0]
    print(f"apple/de 待归档：{n} 条快照 / {models} 个机型")

    if a.dry_run:
        print("(dry-run，未改动)")
        c.close()
        return

    if a.restore:
        if not c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (ARCH,)).fetchone():
            print(f"归档表 {ARCH} 不存在")
            c.close()
            return
        cols = [r[1] for r in c.execute(f"PRAGMA table_info({ARCH})")]
        colsql = ",".join(cols)
        c.execute(f"INSERT OR IGNORE INTO price_snapshots({colsql}) SELECT {colsql} FROM {ARCH}")
        c.commit()
        print(f"已从 {ARCH} 恢复 {c.total_changes} 行")
        c.close()
        return

    if not a.execute:
        ap.error("需指定 --dry-run / --execute / --restore")

    bk = DB.with_suffix(f".db.bak-applede-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(DB, bk)
    print("已备份 →", bk.name)
    c.execute(f"DROP TABLE IF EXISTS {ARCH}")
    c.execute(f"CREATE TABLE {ARCH} AS {sel(c)}")
    k = c.execute(f"SELECT COUNT(*) FROM {ARCH}").fetchone()[0]
    c.execute("""DELETE FROM price_snapshots WHERE id IN
                 (SELECT ps.id FROM price_snapshots ps JOIN parts p ON p.id=ps.part_id
                  JOIN models m ON m.id=p.model_id JOIN brands b ON b.id=m.brand_id
                  WHERE b.name='apple' AND m.country_code='de')""")
    c.commit()
    print(f"已归档 {k} 条到 {ARCH}，并从主表删除（可用 --restore 回滚）")
    left = c.execute("SELECT COUNT(*) FROM price_snapshots ps JOIN parts p ON p.id=ps.part_id "
                     "JOIN models m ON m.id=p.model_id JOIN brands b ON b.id=m.brand_id "
                     "WHERE b.name='apple' AND m.country_code='de'").fetchone()[0]
    print("主表剩余 apple/de 快照:", left)
    c.close()


if __name__ == "__main__":
    main()
