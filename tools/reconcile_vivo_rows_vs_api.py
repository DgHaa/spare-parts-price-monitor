"""tools/reconcile_vivo_rows_vs_api.py - 用官方接口对账 vivo 全部价行（可逆）。

判据（比"按日期切"更本质）：
  **库里每一行 vivo 价，都必须能被当前官方接口复现。** 复现不了的行 = 无从核验，
  不应出现在大盘里。具体分三种不可复现：

    A. 机型不在备件页 SSR 列表里       → 该机型已下架/改名，整台不可复现
    B. 机型在列表但接口 queryByCrm=false / 价表为空 → 该机型官方未公布备件价
    C. 机型有价表，但某个部件名不在价表里 → 该部件是旧口径独有的行（如 ae/X300 Pro
       的 12G 主板变体、my/X300 Pro 的 128G+6G 主板 —— 这些是 2026-09-17 18:40
       那次探索性跑批留下的，官方接口现在不返回）

为什么需要 C：按 captured_at 日期切的规则会漏掉"同样是今天写的、但来自废弃口径"的行。

  用法：
    python tools/reconcile_vivo_rows_vs_api.py --dry-run
    python tools/reconcile_vivo_rows_vs_api.py --execute
    python tools/reconcile_vivo_rows_vs_api.py --restore
"""
import argparse
import asyncio
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, "C:/Users/Dong/.workbuddy/skills/spare-parts-price/scripts")
_VENDOR = ROOT / "vendor"  # 仓库内副本优先（最后插入 = 最先被 import）
if _VENDOR.exists():
    sys.path.insert(0, str(_VENDOR))

DB = ROOT / "spare_parts.db"
BRAND = "vivo"
COUNTRIES = ("ae", "my", "tr")
T_S = "price_snapshots_legacy_archive"


def db_rows(cur, cc):
    """{model_name: {part_name: snapshot_id}}"""
    out = {}
    for mdl, part, sid in cur.execute(
            """select m.name, p.name, s.id from price_snapshots s
               join parts p on p.id = s.part_id
               join models m on m.id = p.model_id
               join brands b on b.id = m.brand_id
               where b.name = ? and m.country_code = ?""", (BRAND, cc)):
        out.setdefault(mdl, {})[part] = sid
    return out


async def api_tables(cc):
    """{model_name: set(part_name)}；机型不在 SSR 列表或接口无价时为空集合。"""
    import executor
    rid, items = await asyncio.to_thread(executor.vivo_support_page, cc)
    names = [n for _, n in items]
    sem = asyncio.Semaphore(6)

    async def one(did, nm):
        async with sem:
            rows, _err, status = await asyncio.to_thread(
                executor.vivo_price_rows, rid, did, nm)
        return nm, ({r["part"] for r in rows} if status == "ok" else set())

    pairs = await asyncio.gather(*(one(d, n) for d, n in items))
    tbl = dict(pairs)
    for n in names:
        tbl.setdefault(n, set())
    return tbl


def reconcile(cur):
    """返回 (bad_sids, report_lines)。"""
    bad, lines = [], []
    for cc in COUNTRIES:
        have = db_rows(cur, cc)
        api = asyncio.run(api_tables(cc))
        n_a = n_b = n_c = 0
        for mdl, parts in sorted(have.items()):
            if mdl not in api:
                n_a += len(parts)
                bad.extend(parts.values())
                lines.append(f"  [A 机型不在备件页] {cc}/{mdl}: {len(parts)} 行")
                continue
            allowed = api[mdl]
            if not allowed:
                n_b += len(parts)
                bad.extend(parts.values())
                lines.append(f"  [B 官方无价表]     {cc}/{mdl}: {len(parts)} 行")
                continue
            miss = {p: sid for p, sid in parts.items() if p not in allowed}
            if miss:
                n_c += len(miss)
                bad.extend(miss.values())
                lines.append(f"  [C 部件不在价表]   {cc}/{mdl}: {len(miss)} 行 "
                             f"({', '.join(sorted(miss)[:4])})")
        print(f"[{cc}] 库内机型={len(have)}  接口有价机型="
              f"{sum(1 for v in api.values() if v)}  不可复现 A={n_a} B={n_b} C={n_c}",
              flush=True)
    return bad, lines


def backup():
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = ROOT / f"spare_parts.db.bak-reconcile-vivo-{ts}"
    shutil.copy2(DB, dst)
    print(f"[backup] {dst.name}")
    return dst


def main(mode):
    c = sqlite3.connect(DB)
    cur = c.cursor()
    print("[对账] 逐个区域拉官方接口比对……", flush=True)
    bad, lines = reconcile(cur)
    print(f"\n[结果] 不可复现行 = {len(bad)}")
    for ln in lines:
        print(ln)
    if mode == "execute" and bad:
        backup()
        cur.execute(f"create table if not exists {T_S} as select * from price_snapshots where 0")
        qs = ",".join("?" * len(bad))
        cur.execute(f"insert or ignore into {T_S} select * from price_snapshots where id in ({qs})", bad)
        cur.execute(f"delete from price_snapshots where id in ({qs})", bad)
        c.commit()
        print(f"[execute] 已隔离 {len(bad)} 行到 {T_S}")
    elif mode == "execute":
        print("[execute] 无需隔离")
    else:
        print("[dry-run] 未改动任何数据；确认无误后跑 --execute")
    c.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--execute", action="store_true")
    a = ap.parse_args()
    main("execute" if a.execute else "dry-run")
