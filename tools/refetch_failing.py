"""修复 vivo/xiaomi 失败机型的 DB 价格：用官方机型级 API 当前价整体重插（删当前季度旧快照）。

vivo:  model_url 的 queryPriceByProductId?id={data-id} 的 price 字段（链接正确，DB 价过期）
       例外：非手机机型(如 tr 的 seyahat şarj 旅行充电器)官方下拉无此项 → API 无价，跳过并标记 brand_entry。
xiaomi: shop_band_wx_price?class_id={cid} 的 materials[].sale_price（DB 抓的是渲染表"物料+偏移"，
       非恒定偏移，必须整体用官方价覆盖）。943 台并发抓取以缩短时长。

用法:
  python -u tools/refetch_failing.py           # DRY-RUN，只对比
  python -u tools/refetch_failing.py --apply    # 写入（删旧快照+官方价重插）
"""
import sqlite3, json, re, urllib.request, urllib.parse, sys, argparse, time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "spare_parts.db"
sys.path.insert(0, str(ROOT))
import db as D  # noqa: E402
from crawler.model_links import norm, match_official_name  # noqa: E402

MI_CLASS_LIST = "https://api2.service.order.mi.com/repair_price/shop_class_info?keyword=&callback=CALLBACK"
MI_PRICE = "https://api2.service.order.mi.com/repair_price/shop_band_wx_price?class_id={cid}&callback=cb"
HTTP_TIMEOUT = 12
XM_WORKERS = 4


def direct_get(url, referer=None):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": referer or ""})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return {"status": resp.status, "text": resp.read().decode("utf-8", "replace")}
    except Exception as e:
        return {"status": 0, "error": f"{type(e).__name__}: {str(e)[:120]}"}


def xiaomi_class_map():
    r = direct_get(MI_CLASS_LIST, referer="https://www.mi.com/service/materialprice")
    if r["status"] != 200:
        return {}
    m = re.search(r"\((.*)\)", r["text"], re.S)
    if not m:
        return {}
    data = json.loads(m.group(1)); acc = {}
    def walk(o, d=0):
        if d > 8: return
        if isinstance(o, dict):
            nm = next((o.get(k) for k in ("name","class_name","className","title","label","goods_name","product_name") if isinstance(o.get(k), str) and o[k].strip()), None)
            cid = next((str(o.get(k)).strip() for k in ("class_id","classId","id","goods_id","goodsId") if isinstance(o.get(k),(int,str)) and str(o.get(k)).strip().isdigit()), None)
            if nm and cid: acc[nm] = cid
            for v in o.values(): walk(v, d+1)
        elif isinstance(o, list):
            for v in o: walk(v, d+1)
    walk(data); return acc


def xiaomi_api(cid):
    for attempt in range(1, 4):
        r = direct_get(MI_PRICE.format(cid=cid), referer="https://www.mi.com/service/materialprice")
        if r["status"] == 200:
            m = re.search(r"cb\((.*)\)", r["text"], re.S)
            if m:
                d = json.loads(m.group(1)); body = d.get("data", {})
                mats = body.get("materials") if isinstance(body, dict) else None
                if mats:
                    return [(x.get("shop_material_class_name"), float(x["sale_price"])) for x in mats if x.get("sale_price") is not None], f"code={d.get('code')}"
        if r["status"] == 429:
            time.sleep(6); continue
        time.sleep(0.4)
    return None, f"HTTP {r['status']}"


def vivo_api(mid, cc):
    url = f"https://www.vivo.com/{cc}/support/queryPriceByProductId?id={mid}"
    for attempt in range(1, 3):
        r = direct_get(url, referer=f"https://www.vivo.com/{cc}/support/accessory")
        if r["status"] == 200:
            j = json.loads(r["text"])
            lst = (((j.get("data") or {}).get("sparePartVO") or {}).get("sparePartsVoList") or [])
            out = []
            for p in lst:
                raw = p.get("price")
                if raw is None:
                    continue
                m = re.search(r"-?\d[\d,]*\.?\d*", str(raw))   # 价格可能带币种后缀，如 "1390TRY"
                if not m:
                    continue
                out.append((p.get("name"), float(m.group(0).replace(",", ""))))
            return out, f"{len(lst)} parts"
        elif r["status"] == 429:
            time.sleep(5); continue
        time.sleep(0.3)
    return None, f"HTTP {r['status']}"


def conn():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c


def cur_currency(c, model_id, quarter):
    row = c.execute("""SELECT ps.currency FROM price_snapshots ps JOIN parts pt ON pt.id=ps.part_id
                       WHERE pt.model_id=? AND ps.quarter=? LIMIT 1""", (model_id, quarter)).fetchone()
    return row["currency"] if row else None


def refresh(c, model_id, quarter, api_list, currency, api_url, dry):
    if dry:
        return len(api_list)
    rate, src, asof = D.get_rate_meta(quarter, currency)
    c.execute("DELETE FROM price_snapshots WHERE part_id IN (SELECT id FROM parts WHERE model_id=?) AND quarter=?",
              (model_id, quarter))
    for name, price in api_list:
        pid = D.upsert_part(model_id, name, conn=c)
        cny = price * rate if rate else None
        D.insert_snapshot(pid, quarter, price, currency, cny, source_url=api_url, source_url_kind="model_api",
                          has_labor_split=0, labor_note="官网未单列人工费，仅提供含人工的总维修价（来源见取证链接）",
                          labor_source_url=api_url, rate_source=src, rate_as_of=asof, conn=c)
    c.execute("""UPDATE models SET model_url=?, model_url_kind='model_api', model_url_verified=1,
                 model_url_checked_at=CURRENT_TIMESTAMP, source_url=? WHERE id=?""",
              (api_url, api_url, model_id))
    return len(api_list)


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--apply", action="store_true")
    ap.add_argument("--vivo-only", action="store_true", help="只处理 vivo（跳过小米，避免重复打 mi.com）")
    args = ap.parse_args(); dry = not args.apply
    quarter = D.this_quarter()
    if args.vivo_only:
        cmap = {}; nmap = {}
    else:
        cmap = xiaomi_class_map(); nmap = {norm(k): (k, v) for k, v in cmap.items()}
    c = conn(); total = 0; skipped = 0; failed = 0
    print(f"季度={quarter}  {'[DRY-RUN]' if dry else '[APPLY]'}  xiaomi class_map={len(nmap)}", flush=True)

    # ---- 小米/cn：全部机型（渲染表价不可信，必须整体用官方 sale_price 覆盖） ----
    if args.vivo_only:
        print("\n[xiaomi/cn] 跳过（--vivo-only）", flush=True)
        xm = []; jobs = []
    else:
        xm = c.execute("""SELECT m.id, m.name FROM models m JOIN brands b ON b.id=m.brand_id
                          WHERE b.name='xiaomi' AND m.country_code='cn'""").fetchall()
        jobs = []
        for m in xm:
            hit, by = match_official_name({"name": m["name"], "base_model": m["name"]}, nmap)
            if not hit:
                skipped += 1; continue
            cid = hit[1]
            cur = cur_currency(c, m["id"], quarter) or "CNY"
            jobs.append((m["id"], m["name"], cid, cur))
        print(f"\n[xiaomi/cn] 共 {len(xm)} 台，命中 {len(jobs)} 台将并发重抓官方价（workers={XM_WORKERS}）", flush=True)

    def worker(job):
        mid, name, cid, cur = job
        api, info = xiaomi_api(cid)
        return mid, name, cid, cur, api, info

    done = 0
    failed_jobs = []
    with ThreadPoolExecutor(max_workers=XM_WORKERS) as ex:
        futs = {ex.submit(worker, j): j for j in jobs}
        for fut in as_completed(futs):
            mid, name, cid, cur, api, info = fut.result()
            done += 1
            if not api:
                failed_jobs.append((mid, name, cid, cur))
                if done % 50 == 0:
                    print(f"  ...{done}/{len(jobs)} 失败累计 {len(failed_jobs)}", flush=True)
                continue
            n = refresh(c, mid, quarter, api, cur, MI_PRICE.format(cid=cid), dry)
            total += 1
            if not dry and total % 50 == 0:
                c.commit()   # 增量提交，避免长事务被中断时全丢
            if done % 100 == 0:
                print(f"  ...{done}/{len(jobs)} 已刷新 {total} 失败 {len(failed_jobs)}", flush=True)
    # 重试失败项（多为 429/限流），顺序退避
    if failed_jobs:
        print(f"  [retry] 重试 {len(failed_jobs)} 台失败项（退避 8s）...", flush=True)
        still = 0
        for mid, name, cid, cur in failed_jobs:
            ok = False
            for _ in range(3):
                api, info = xiaomi_api(cid)
                if api:
                    ok = True; break
                time.sleep(3)
            if ok:
                refresh(c, mid, quarter, api, cur, MI_PRICE.format(cid=cid), dry)
                total += 1
            else:
                still += 1
        failed = still
    else:
        failed = 0
    if not dry:
        c.commit()
    print(f"[xiaomi/cn] 刷新 {total} 台，最终失败 {failed} 台，跳过(无cid) {skipped} 台", flush=True)

    # ---- vivo：仅 T2 标记 MISMATCH 的机型 ----
    rep = json.load(open(ROOT/"output"/"verify_t2.json", encoding="utf-8"))
    vivos = [(c2["country"], s["model"]) for c2 in rep["combos"] if c2["brand"] == "vivo"
             for s in c2["samples"] if s.get("verdict") == "MISMATCH"]
    print(f"\n[vivo] T2 标记 MISMATCH: {vivos}", flush=True)
    vtotal = 0
    for cc, name in vivos:
        row = c.execute("""SELECT m.id, m.model_url FROM models m JOIN brands b ON b.id=m.brand_id
                           WHERE b.name='vivo' AND m.country_code=? AND m.name=?""",
                        (cc, name)).fetchone()
        if not row:
            print(f"  [skip] 未找到 {cc} {name}", flush=True); skipped += 1; continue
        m = re.search(r"id=(\d+)", row["model_url"] or "")
        if not m:
            print(f"  [skip] {cc} {name} 无 data-id", flush=True); skipped += 1; continue
        api, info = vivo_api(m.group(1), cc)
        if not api:
            print(f"  [skip] {cc} {name} 官方 API 无价({info}) → 标记 brand_entry，不伪造", flush=True)
            if not dry:
                c.execute("""UPDATE models SET model_url_kind='brand_entry', model_url_verified=0
                             WHERE id=?""", (row["id"],))
            skipped += 1; continue
        cur = cur_currency(c, row["id"], quarter) or "CNY"
        n = refresh(c, row["id"], quarter, api, cur,
                    f"https://www.vivo.com/{cc}/support/queryPriceByProductId?id={m.group(1)}", dry)
        vtotal += 1
        print(f"  [{'将刷新' if dry else '已刷新'}] {cc} {name}: {n} 个部件 (api {info})", flush=True)

    if not dry:
        c.commit()
    c.close()
    print(f"\n完成。xiaomi 刷新 {total} 台 + vivo 刷新 {vtotal} 台，跳过 {skipped} 台。"
          + ("（DRY-RUN，未写入）" if dry else "（已写入 DB）"), flush=True)


if __name__ == "__main__":
    main()
