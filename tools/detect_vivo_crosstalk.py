"""检测 vivo 库内的"整表串值"：两台不同机型出现完全相同的价表（部件名→价 全等）。

串值特征（2026-09-17 实锤）：DOM 点选路径未等表格刷新就读，把上一台机型的价写成本台的。
已证实例：ae/X200 FE 的 10 条价与 ae/X300 Pro 的正确接口值逐字段相同，而 X300 Pro 当时
自身落库的是另一组值 —— 即 X200 FE 那一条是"偷"来的，不是巧合同价。

真实同价只可能出现在容量/配色变体之间（如 X300 Pro 与 X300 Pro 的 512G 版），
所以按整表指纹比对能高召回地暴露串值，再人工判定。

  用法：python tools/detect_vivo_crosstalk.py [--brand vivo] [--country ae]
"""
import argparse
import sqlite3
from collections import defaultdict
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "spare_parts.db"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brand", default="vivo")
    ap.add_argument("--country", default=None)
    a = ap.parse_args()

    c = sqlite3.connect(str(DB))
    sql = """select m.country_code, m.name, p.name, s.price
             from price_snapshots s
             join parts p on p.id = s.part_id
             join models m on m.id = p.model_id
             join brands b on b.id = m.brand_id
             where b.name = ?"""
    params = [a.brand]
    if a.country:
        sql += " and m.country_code = ?"
        params.append(a.country)
    sql += " order by m.country_code, m.name"

    tbl = defaultdict(dict)
    for cc, mdl, part, price in c.execute(sql, params):
        tbl[(cc, mdl)][part] = price

    fp = defaultdict(list)
    for key, parts in tbl.items():
        if len(parts) < 4:            # 太短的表不足以判定
            continue
        # 指纹 = 排序后的 (部件, 价) 全表；价可能是小数，统一 round 2 位
        sig = tuple(sorted((k, round(float(v), 2)) for k, v in parts.items()))
        fp[sig].append(key)

    groups = [v for v in fp.values() if len(v) > 1]
    print(f"[scan] {a.brand} 机型数={len(tbl)}  整表同指纹组={len(groups)}")
    for g in sorted(groups, key=lambda x: (-len(x), x)):
        n = len(tbl[g[0]])
        print(f"\n  ● {n} 条价完全相同，涉 {len(g)} 台：")
        for cc, mdl in g:
            print(f"      {cc}/{mdl}")
        sample = sorted(tbl[g[0]].items(), key=lambda kv: -float(kv[1]))[:4]
        print("      样例: " + ", ".join(f"{k}={v}" for k, v in sample))
    c.close()


if __name__ == "__main__":
    main()
