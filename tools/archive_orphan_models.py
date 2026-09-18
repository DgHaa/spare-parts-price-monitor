"""tools/archive_orphan_models.py - 归档"零价行"的残留机型（可逆）。

背景（2026-09-17，samsung/tr）：
  土站的旧 DOM 口径把页面上的 **型号代码**（`SM-S928 (2024)`）当成机型名建了 93 行 models，
  同时把另一张"系列级统一费用"表（Seri→Ücret）也当成机型表，于是库里出现
  机型名=「Galaxy S Serisi」、部件名=「Ücret」的假数据。
  改用服务端 HTML 表后（fetch_tr），机型名取官方 `Model Adı`（`Galaxy S24 Ultra`），
  旧的 93 行代码名 models 全部变成**没有任何价行**的孤儿，却仍会出现在机型列表里虚增计数。

判据（刻意收窄，避免误伤）：
  1) 限定 brand + country；
  2) 机型名匹配 `--name-like`（缺省 `SM-%`，即"用型号代码当机型名"这一特征）；
  3) **且**该机型一条价行都没有。
  三个条件同时满足才归档 —— 光"零价行"不够：如 vivo 的 `T1 Pro 5G` 官方确实存在、
  只是没公布备件价，那种行必须保留（它代表"机型存在但无价"）。

  全程可逆：models/parts/price_snapshots 三表一起进 `*_legacy_archive`，可 --restore。

用法：
  python tools/archive_orphan_models.py --dry-run --brand samsung --country tr
  python tools/archive_orphan_models.py --execute --brand samsung --country tr
  python tools/archive_orphan_models.py --restore
"""
import argparse
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "spare_parts.db"
T_M, T_P, T_S = "models_legacy_archive", "parts_legacy_archive", "price_snapshots_legacy_archive"


def _plan(cur, brand, country, name_like):
    return [r[0] for r in cur.execute(
        """select m.id from models m
           join brands b on b.id = m.brand_id
           where b.name = ? and m.country_code = ? and m.name like ?
             and not exists (select 1 from parts p
                             join price_snapshots s on s.part_id = p.id
                             where p.model_id = m.id)
           order by m.id""", (brand, country, name_like))]


def show(cur, brand, country, name_like):
    ids = _plan(cur, brand, country, name_like)
    print(f"[plan] {brand}/{country} 名匹配 {name_like!r} 且零价行的机型 = {len(ids)}")
    for (nm,) in cur.execute(
            f"select name from models where id in ({','.join('?' * len(ids))}) limit 8", ids) if ids else []:
        print(f"        {nm}")
    return ids


def backup():
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = ROOT / f"spare_parts.db.bak-orphan-{ts}"
    shutil.copy2(DB, dst)
    print(f"[backup] {dst.name}")
    return dst


def main(mode, brand, country, name_like):
    c = sqlite3.connect(DB)
    cur = c.cursor()
    if mode == "restore":
        backup()
        for t, src in ((T_S, "price_snapshots"), (T_P, "parts"), (T_M, "models")):
            cur.execute(f"insert or ignore into {src} select * from {t}")
            print(f"[restore] {t} -> {src}: {cur.rowcount} 行")
        c.commit()
        c.close()
        return
    ids = show(cur, brand, country, name_like)
    if mode == "dry-run" or not ids:
        print("[dry-run] 未改动任何数据" if mode == "dry-run" else "[execute] 无需归档")
        c.close()
        return
    backup()
    for t, src in ((T_M, "models"), (T_P, "parts"), (T_S, "price_snapshots")):
        cur.execute(f"create table if not exists {t} as select * from {src} where 0")
    qs = ",".join("?" * len(ids))
    cur.execute(f"insert or ignore into {T_M} select * from models where id in ({qs})", ids)
    cur.execute(f"insert or ignore into {T_P} select * from parts where model_id in ({qs})", ids)
    cur.execute(f"insert or ignore into {T_S} select * from price_snapshots where part_id in "
                f"(select id from parts where model_id in ({qs}))", ids)
    cur.execute(f"delete from price_snapshots where part_id in "
                f"(select id from parts where model_id in ({qs}))", ids)
    cur.execute(f"delete from parts where model_id in ({qs})", ids)
    cur.execute(f"delete from models where id in ({qs})", ids)
    c.commit()
    print(f"[execute] 已归档 {len(ids)} 行孤儿机型（含其 parts/snapshots）到 *_legacy_archive")
    c.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    for m in ("dry-run", "execute", "restore"):
        ap.add_argument(f"--{m}", action="store_true")
    ap.add_argument("--brand", required=False, default="samsung")
    ap.add_argument("--country", required=False, default="tr")
    ap.add_argument("--name-like", default="SM-%")
    a = ap.parse_args()
    main(next(m for m in ("execute", "restore", "dry-run")
              if getattr(a, m.replace("-", "_"))), a.brand, a.country, a.name_like)
