"""定向回填：小米/cn 本季仍为 brand_entry（未用官方 API 价覆盖）的缺口机型。

与 tools/refetch_failing.py 的区别：
  - 只处理「当前仍是 brand_entry」的机型（默认约 135 台），不重抓已核实的 808 台；
  - 串行抓取 + 强 429 退避（尊重 Retry-After，缺省指数退避 15/30/60s，最多 6 次重试），
    规避上次"4 并发撞 429 漏 135 台"的问题；
  - 每填一台即 commit，中断可续跑（已 verified 的机型下次自动跳过）。

写入逻辑完全复用 refetch_failing.refresh：删本季旧快照 → 插官方 sale_price（source_url_kind='model_api'）
→ models 标 model_url_kind='model_api'/verified=1。

用法:
  python -u tools/backfill_xiaomi_gap.py           # DRY-RUN，只打印将处理的机型
  python -u tools/backfill_xiaomi_gap.py --apply    # 写入
"""
import sqlite3, json, re, time, sys, argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import db as D  # noqa: E402
from crawler.model_links import norm, match_official_name  # noqa: E402

# 复用 refetch_failing 的 class_map 与 refresh 写入逻辑
from tools.refetch_failing import xiaomi_class_map as _cmap, refresh, cur_currency  # noqa: E402

MI_PRICE = "https://api2.service.order.mi.com/repair_price/shop_band_wx_price?class_id={cid}&callback=cb"
HTTP_TIMEOUT = 15
XM_WORKERS = 1          # 串行，最大限度避免 429
MAX_RETRY = 6
BACKOFF = [15, 30, 60, 90, 120, 180]


def direct_get(url, referer=None):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": referer or ""})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return {"status": resp.status, "text": resp.read().decode("utf-8", "replace"),
                    "retry_after": resp.headers.get("Retry-After")}
    except Exception as e:
        return {"status": 0, "error": f"{type(e).__name__}: {str(e)[:120]}"}


def xiaomi_api_robust(cid):
    """带强 429 退避的官方价抓取。返回 (list[(name,price)]|None, info)。"""
    last = ""
    for attempt in range(1, MAX_RETRY + 1):
        r = direct_get(MI_PRICE.format(cid=cid), referer="https://www.mi.com/service/materialprice")
        if r["status"] == 200:
            m = re.search(r"cb\((.*)\)", r["text"], re.S)
            if m:
                try:
                    d = json.loads(m.group(1)); body = d.get("data", {})
                    mats = body.get("materials") if isinstance(body, dict) else None
                    if mats:
                        return [(x.get("shop_material_class_name"), float(x["sale_price"]))
                                for x in mats if x.get("sale_price") is not None], f"code={d.get('code')}"
                except Exception as e:
                    last = f"parse:{e}"
            else:
                last = "no cb() wrapper"
        elif r["status"] == 429:
            ra = r.get("retry_after")
            try:
                wait = int(ra) if ra and str(ra).isdigit() else BACKOFF[min(attempt - 1, len(BACKOFF) - 1)]
            except Exception:
                wait = BACKOFF[min(attempt - 1, len(BACKOFF) - 1)]
            print(f"    [429] cid={cid} 退避 {wait}s (attempt {attempt}/{MAX_RETRY})", flush=True)
            time.sleep(wait)
            continue
        else:
            last = f"HTTP {r['status']}"
            time.sleep(2)
    return None, last


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(); dry = not args.apply
    quarter = D.this_quarter()
    cmap = _cmap()
    nmap = {norm(k): (k, v) for k, v in cmap.items()}

    c = sqlite3.connect(ROOT / "spare_parts.db"); c.row_factory = sqlite3.Row
    rows = c.execute("""SELECT DISTINCT m.id, m.name, m.base_model
        FROM models m JOIN brands b ON b.id=m.brand_id
        JOIN parts pt ON pt.model_id=m.id
        JOIN price_snapshots ps ON ps.part_id=pt.id
        WHERE b.name='xiaomi' AND m.country_code='cn' AND ps.quarter=? AND ps.source_url_kind='brand_entry'
        ORDER BY m.name""", (quarter,)).fetchall()
    print(f"季度={quarter}  {'[DRY-RUN]' if dry else '[APPLY]'}  缺口(brand_entry) {len(rows)} 台", flush=True)

    jobs = []
    for m in rows:
        hit, by = match_official_name({"name": m["name"], "base_model": m["base_model"] or m["name"]}, nmap)
        if not hit:
            print(f"  [skip-无cid] {m['name']}", flush=True); continue
        jobs.append((m["id"], m["name"], hit[1], cur_currency(c, m["id"], quarter) or "CNY"))
    print(f"可回填 {len(jobs)} 台\n", flush=True)
    if dry:
        for mid, name, cid, cur in jobs[:30]:
            print(f"  {name} -> cid={cid}")
        if len(jobs) > 30:
            print(f"  ...(其余 {len(jobs)-30} 台省略)")
        c.close()
        return

    done = filled = 0; still = []
    for mid, name, cid, cur in jobs:
        api, info = xiaomi_api_robust(cid)
        done += 1
        if api:
            refresh(c, mid, quarter, api, cur, MI_PRICE.format(cid=cid), dry=False)
            c.commit()  # 逐台提交，可续跑
            filled += 1
            if done % 20 == 0 or done == len(jobs):
                print(f"  ...{done}/{len(jobs)} 已填 {filled}", flush=True)
        else:
            still.append((name, info))
            print(f"  [fail] {name}: {info}", flush=True)
    c.close()
    print(f"\n完成：回填 {filled}/{len(jobs)} 台；仍失败 {len(still)} 台", flush=True)
    if still:
        print("仍失败清单：")
        for name, info in still:
            print(f"  {name}  ({info})")


if __name__ == "__main__":
    main()
