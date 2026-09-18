#!/usr/bin/env python3
"""bench_reborn_concurrency.py — 验证 REBORN 并发取价：正确性 + 实际提速。

两种模式：

① 机型级（单区域）：对比同一批机型在 concurrency=1（等价改造前的顺序实现）与
   concurrency=N 下的结果一致性与墙钟耗时。
     python tools/bench_reborn_concurrency.py --country de --n 24 --conc 8
     python tools/bench_reborn_concurrency.py --country cn --n 30 --conc 6

② 区域级（多区域，验证 run_all 的区域并发）：同一组区域的"区域串行 vs 区域并发"
   对比，用于验证 run_all(RUN_ALL_CONCURRENCY)。
     python tools/bench_reborn_concurrency.py --countries cn,de,tr,mx,my,jp,ae --region-conc 2
   两阶段都只做「发现 + 取价」，**不写库**，故可安全重复跑。
"""
import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 项目根
# 注意导入顺序：crawler.core 在 import 时把 skill 的 scripts/ 目录注入 sys.path，
# 必须先导入它，后面才能 import executor / normalize。
from crawler.core import launch_browser  # noqa: E402,F401
from crawler.run import discover_and_price_via_reborn  # noqa: E402
import executor  # noqa: E402  （来自 skill scripts，crawler.core 已注入 sys.path）


def _fingerprint(discovered):
    """把结果压成可比对的指纹：{机型: [(备件, 价格), ...]}（价格保留原字符串精度）。"""
    fp = {}
    for nm, rows, _url in discovered:
        fp[nm] = sorted((r.get("part") or "", str(r.get("price"))) for r in rows)
    return fp


async def _run(rec, country, n, conc):
    t0 = time.perf_counter()
    out = await discover_and_price_via_reborn(None, rec, country,
                                              max_models=n, concurrency=conc)
    return out, time.perf_counter() - t0


async def _run_many(brand, countries, n, conc, region_conc):
    """跑一组区域，区域之间用 region_conc 限流；返回 ({区域: 结果}, 墙钟秒数)。

    region_conc=1 即"区域串行"（等价 run_all 改造前），>1 即区域并发。
    每个区域内部仍是 conc 机型并发——两层并发相乘才是真实在飞请求数。
    """
    t0 = time.perf_counter()
    sem = asyncio.Semaphore(region_conc)

    async def one(c):
        rec = executor.load_record(brand, c)
        if not rec:
            print(f"    [{c}] 无 KB 记录", flush=True)
            return c, []
        async with sem:  # 排队期间不发请求，且 region_conc 决定同时在跑几个区域
            tc = time.perf_counter()
            out = await discover_and_price_via_reborn(None, rec, c, max_models=n,
                                                      concurrency=conc)
            el = time.perf_counter() - tc
        print(f"    [{c}] {len(out)} 台 / {sum(len(r[1]) for r in out)} 行 / {el:.1f}s",
              flush=True)
        return c, out

    res = await asyncio.gather(*(one(c) for c in countries))
    return dict(res), time.perf_counter() - t0


def _multi_fingerprint(per_country):
    """{区域: {机型: [(备件, 价格), ...]}}——比机型级指纹多一层区域维度。"""
    return {c: _fingerprint(v) for c, v in per_country.items()}


def _cmp_fingerprint(fa, fb):
    """逐区域、逐机型、逐条价格比对，返回 (是否完全一致, 差异描述列表)。"""
    diffs = []
    if set(fa) != set(fb):
        diffs.append(f"区域集合不一致：{sorted(set(fa) ^ set(fb))}")
    for c in sorted(set(fa) & set(fb)):
        if set(fa[c]) != set(fb[c]):
            a, b = set(fa[c]), set(fb[c])
            diffs.append(f"[{c}] 机型集合不一致：+{sorted(b - a)[:3]} -{sorted(a - b)[:3]}")
            continue
        for nm in fa[c]:
            if fa[c][nm] != fb[c][nm]:
                diffs.append(f"[{c}/{nm}] 价格不一致")
    return (not diffs), diffs


async def _main_multi(args):
    countries = [c.strip() for c in args.countries.split(",") if c.strip()]
    n = args.n or None
    print(f"=== 区域级基准 {args.brand} · {len(countries)} 区域 {countries} · "
          f"每区域并发={args.conc} · 取价（不写库）===", flush=True)

    print(f"\n--- 阶段 1/2：区域**串行**（region_conc=1，等价改造前的 run_all）---", flush=True)
    seq, t_seq = await _run_many(args.brand, countries, n, args.conc, 1)
    print(f"  → 区域串行总耗时 {t_seq:.1f}s\n", flush=True)

    print(f"--- 阶段 2/2：区域**并发**（region_conc={args.region_conc}）---", flush=True)
    con, t_con = await _run_many(args.brand, countries, n, args.conc, args.region_conc)
    print(f"  → 区域并发总耗时 {t_con:.1f}s\n", flush=True)

    ok, diffs = _cmp_fingerprint(_multi_fingerprint(seq), _multi_fingerprint(con))
    fp_seq, fp_con = _multi_fingerprint(seq), _multi_fingerprint(con)
    n_seq = sum(len(v) for c in fp_seq for v in fp_seq[c].values())
    n_con = sum(len(v) for c in fp_con for v in fp_con[c].values())
    print("=== 结果一致性（区域级并发不得改变任何一条价格）===")
    print(f"  区域/机型/逐条价格完全一致 : {'OK' if ok else 'MISMATCH'}")
    print(f"  价行总数                   : 串行 {n_seq} / 并发 {n_con} "
          f"→ {'OK' if n_seq == n_con else 'MISMATCH'}")
    for d in diffs[:8]:
        print(f"    ✗ {d}")
    print("=== 提速 ===")
    print(f"  区域串行 {t_seq:.1f}s → 区域并发 {t_con:.1f}s"
          f"  =  {t_seq / max(t_con, 1e-9):.2f}×（区域并发度 {args.region_conc}）")
    return 0 if ok else 2


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brand", default="oppo")
    ap.add_argument("--country", default="de")
    ap.add_argument("--countries", default=None,
                    help="多区域模式：逗号分隔，如 cn,de,tr,mx,my,jp,ae（验证 run_all 区域并发）")
    ap.add_argument("--region-conc", type=int, default=2,
                    help="多区域模式的区域并发度（对应 run_all 的 RUN_ALL_CONCURRENCY）")
    ap.add_argument("--n", type=int, default=24,
                    help="每区域抽前 N 台做对比（0=全量）")
    ap.add_argument("--conc", type=int, default=8)
    ap.add_argument("--skip-serial", action="store_true",
                    help="只跑并发（全量计时用，省去串行基线）")
    args = ap.parse_args()

    if args.countries:
        return await _main_multi(args)

    rec = executor.load_record(args.brand, args.country)
    if not rec:
        print(f"[ERR] 无 KB 记录 {args.brand}/{args.country}")
        return 1
    if rec.get("query", {}).get("api", {}).get("fetch_mode") != "http":
        # 浏览器模式才需要 page；本脚本按 http 模式设计
        print(f"[WARN] {args.brand}/{args.country} 非 fetch_mode=http，"
              f"本脚本传 page=None 可能失败，仅作参考")

    print(f"=== 基准 {args.brand}/{args.country}  前 {args.n} 台 ===", flush=True)
    if args.skip_serial:
        con, t_con = await _run(rec, args.country, args.n, args.conc)
        print(f"\n--- 仅并发（concurrency={args.conc}）完成：{len(con)} 台 / {t_con:.1f}s ---\n",
              flush=True)
        print(f"  并发 {t_con:.1f}s（{t_con / max(1, len(con)):.2f}s/台）")
        print(f"  价行总数 {sum(len(r[1]) for r in con)}")
        return 0
    seq, t_seq = await _run(rec, args.country, args.n, 1)
    print(f"\n--- 串行（concurrency=1）完成：{len(seq)} 台 / {t_seq:.1f}s ---\n", flush=True)
    con, t_con = await _run(rec, args.country, args.n, args.conc)
    print(f"\n--- 并发（concurrency={args.conc}）完成：{len(con)} 台 / {t_con:.1f}s ---\n",
          flush=True)

    fs, fc = _fingerprint(seq), _fingerprint(con)
    ok_models = set(fs) == set(fc)
    diffs = []
    for nm in sorted(set(fs) & set(fc)):
        if fs[nm] != fc[nm]:
            diffs.append(nm)
    n_seq_rows = sum(len(v) for v in fs.values())
    n_con_rows = sum(len(v) for v in fc.values())

    print("=== 结果一致性 ===")
    print(f"  机型集合一致        : {'OK' if ok_models else 'MISMATCH'}"
          f"（串行 {len(fs)} / 并发 {len(fc)}）")
    print(f"  价行总数            : 串行 {n_seq_rows} / 并发 {n_con_rows}"
          f" → {'OK' if n_seq_rows == n_con_rows else 'MISMATCH'}")
    print(f"  逐条价格一致        : {'OK' if not diffs else 'MISMATCH ' + str(diffs[:5])}")

    print("=== 提速 ===")
    if t_con > 0:
        print(f"  串行 {t_seq:.1f}s（{t_seq / max(1, len(seq)):.2f}s/台）")
        print(f"  并发 {t_con:.1f}s（{t_con / max(1, len(con)):.2f}s/台）")
        print(f"  加速比 {t_seq / t_con:.2f}×（并发度 {args.conc}）")
    return 0 if (ok_models and not diffs) else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
