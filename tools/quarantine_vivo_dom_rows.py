"""tools/quarantine_vivo_dom_rows.py - 隔离 vivo 的 DOM 旧口径价行（可逆）。

背景（2026-09-17）：
  vivo 三区域（ae/my/tr）的抓取从「DOM 点选」改为「官方 queryPriceByProductId 接口」。
  改的原因不是慢，是**DOM 路径在编数据**——已实锤：

    ae/X200 FE 的 2026-09-02 旧快照共 10 条，与 ae/X300 Pro 的**正确接口值逐字段相同**
    （2542 / 944 / 482 / 347 / 192 / 97 / 74 / 58 / 58 / 35），而 X300 Pro 自身当时
    落库的 Display 是 638 —— 也就是说 X200 FE 那一整张表是「偷」X300 Pro 的。
    同源证据：ae/X300 的旧 Display=244 恰好等于 T1 5G 的当前值；
    my 全区域 150 台里有 35 组「不同机型整表价 100% 相同」；
    换用接口后 X300 Pro 的 my/tr/ae 三区域读数与 KB 人工校准 5/5、4/4 完全一致。

  结论：DOM 旧口径的每一行都不可信，且官方接口现在也不返回这些行（部件名不同或
  该机型接口返回 queryByCrm=false），无法二次核验 → 只能下线，不能留在大盘里。

处置口径（最小且可逆）：
  归档 vivo **captured_at 早于接口刷新日**的全部 price_snapshots 行，主表只保留
  接口口径的行。models / parts 结构不动（parts 失去快照后不参与任何价格查询，
  留着可让 --restore 原样还原）。

  ⚠️ 不删任何数据：行整体搬进 price_snapshots_legacy_archive，可 --restore 还原；
     执行前自动整库备份。

用法：
  python tools/quarantine_vivo_dom_rows.py --dry-run
  python tools/quarantine_vivo_dom_rows.py --execute
  python tools/quarantine_vivo_dom_rows.py --status
  python tools/quarantine_vivo_dom_rows.py --restore
"""
import argparse
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "spare_parts.db"
BRAND = "vivo"
T_S = "price_snapshots_legacy_archive"
# 接口刷新日：>= 此日期的行是接口口径，< 的是 DOM 旧口径
CUTOFF = "2026-09-17"


def _plan(cur, brand=None, country=None, cutoff=None):
    """返回待归档的 snapshot id 列表 + 按区域/机型聚合的摘要。

    brand/country/cutoff 缺省时沿用模块常量（vivo 默认口径），
    这样同一个工具也能直接用于其他"整条口径作废"的场景（如 samsung/tr 的 DOM 垃圾行）。
    """
    brand = brand or BRAND
    cutoff = cutoff or CUTOFF
    where = "b.name = ? and substr(s.captured_at, 1, 10) < ?"
    params = [brand, cutoff]
    if country:
        where += " and m.country_code = ?"
        params.append(country)
    ids = [r[0] for r in cur.execute(
        f"""select s.id from price_snapshots s
            join parts p on p.id = s.part_id
            join models m on m.id = p.model_id
            join brands b on b.id = m.brand_id
            where {where}
            order by s.id""", params)]
    summ = cur.execute(
        f"""select m.country_code, count(distinct m.id) n_models, count(*) n_rows
            from price_snapshots s
            join parts p on p.id = s.part_id
            join models m on m.id = p.model_id
            join brands b on b.id = m.brand_id
            where {where}
            group by m.country_code order by m.country_code""", params).fetchall()
    return ids, summ


def status(cur, brand=None, country=None, cutoff=None):
    has = cur.execute("select count(*) from sqlite_master where type='table' and name=?",
                      (T_S,)).fetchone()[0] > 0
    print(f"[archive] {T_S} = "
          f"{cur.execute(f'select count(*) from {T_S}').fetchone()[0] if has else '（表不存在）'}")
    ids, summ = _plan(cur, brand, country, cutoff)
    print(f"[main]    {brand or BRAND} 待隔离旧口径行 = {len(ids)}")
    for cc, n_m, n_r in summ:
        print(f"            {cc}: {n_m} 台 / {n_r} 行")
    # "口径内行"必须与 _plan 同口径（同样带 country 过滤），否则全品牌计数会
    # 混入其他区域，看数的人会以为"还有这么多行没清"
    keep_where = "b.name=? and substr(s.captured_at,1,10) >= ?"
    keep_params = [brand or BRAND, cutoff or CUTOFF]
    if country:
        keep_where += " and m.country_code=?"
        keep_params.append(country)
    print(f"          口径内行（captured_at >= {cutoff or CUTOFF}）= " + str(cur.execute(
        f"""select count(*) from price_snapshots s join parts p on p.id=s.part_id
            join models m on m.id=p.model_id join brands b on b.id=m.brand_id
            where {keep_where}""", keep_params).fetchone()[0]))


def backup():
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = ROOT / f"spare_parts.db.bak-quarantine-vivo-{ts}"
    shutil.copy2(DB, dst)
    print(f"[backup] {dst.name}")
    return dst


def execute(cur, brand=None, country=None, cutoff=None):
    ids, summ = _plan(cur, brand, country, cutoff)
    if not ids:
        print("[execute] 没有需要隔离的旧口径行")
        return 0
    cur.execute(f"create table if not exists {T_S} as select * from price_snapshots where 0")
    qs = ",".join("?" * len(ids))
    # insert or ignore：归档表按 id 主键，万一重复执行不会因主键冲突中断
    # （正常路径下 _plan 第二次已查不到行，这里是防呆）
    before = cur.execute(f"select count(*) from {T_S} where id in ({qs})", ids).fetchone()[0]
    cur.execute(f"insert or ignore into {T_S} select * from price_snapshots where id in ({qs})", ids)
    cur.execute(f"delete from price_snapshots where id in ({qs})", ids)
    for cc, n_m, n_r in summ:
        print(f"[execute]   {cc}: {n_m} 台 / {n_r} 行 → 归档")
    print(f"[execute] 隔离完成：{len(ids)} 行移入 {T_S}"
          f"（其中 {before} 行归档表已有，跳过重复写入；主表只剩接口口径）")
    return len(ids)


def restore(cur):
    has = cur.execute("select count(*) from sqlite_master where type='table' and name=?",
                      (T_S,)).fetchone()[0] > 0
    if not has:
        print("[restore] 归档表不存在，无可还原")
        return 0
    cur.execute(f"insert or ignore into price_snapshots select * from {T_S}")
    n = cur.rowcount
    print(f"[restore] 已从归档还原 {n} 行到 price_snapshots（归档表保留，便于再次核对）")
    return n


def main(mode, brand=None, country=None, cutoff=None):
    c = sqlite3.connect(DB)
    cur = c.cursor()
    if mode == "status":
        status(cur, brand, country, cutoff)
    elif mode == "dry-run":
        status(cur, brand, country, cutoff)
        print("\n[dry-run] 未改动任何数据；确认无误后跑 --execute")
    elif mode == "execute":
        backup()
        execute(cur, brand, country, cutoff)
        c.commit()
    elif mode == "restore":
        backup()
        restore(cur)
        c.commit()
    c.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    for m in ("dry-run", "execute", "status", "restore"):
        ap.add_argument(f"--{m}", action="store_true")
    ap.add_argument("--brand", default=None, help="默认 vivo")
    ap.add_argument("--country", default=None, help="不填=该品牌全部区域")
    ap.add_argument("--cutoff", default=None, help="captured_at 早于此日(YYYY-MM-DD)的行；默认 2026-09-17")
    a = ap.parse_args()
    main(next(m for m in ("execute", "restore", "status", "dry-run")
              if getattr(a, m.replace("-", "_"))),
         a.brand, a.country, a.cutoff)
