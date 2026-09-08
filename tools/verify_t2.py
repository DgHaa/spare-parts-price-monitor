"""tools/verify_t2.py - T2 价格提取正确性抽样验证

方法：对每个品牌×国家的样本机型，重新打开/请求其「官方源」(models.model_url 或 source_url)，
抽出现价，与库内 price_snapshots（最新季度，原币）逐价格比对。

设计要点：
- 不依赖跨语言部件名匹配（官方返回英文/中文不一，DB 存中文），改用「价格多重集」比对：
  把官方抽出的价格集合与 DB 价格集合排序后逐一容差匹配。集合一致 => 抽取正确且数据当前；
  不一致 => 要么官方已调价（陈旧），要么抽取 bug。二者可进一步区分（系统偏移 vs 个别差异）。
- OPPO/vivo/xiaomi：直连官方 HTTP 接口（快、稳）。
- Apple：浏览器 page_fetch 官方定价 API（该接口拒纯 HTTP），复用 harvest_apple 的抽取。
- Samsung：浏览器打开整表页，textContent 抽取本机型价（启发式，标 low-confidence）。
- 网络/限流失败标记为 skipped，绝不误报为 fail。

输出：output/verify_t2.json + 可读摘要（stdout）。
"""
import asyncio, json, sqlite3, sys, urllib.request, urllib.parse, re, os
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "crawler"))
DB = ROOT / "spare_parts.db"
SAMPLE_K = int(os.environ.get("T2_K", "2"))          # 每品牌×国抽样数
HTTP_TIMEOUT = 20

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


# ---------------------------------------------------------------- HTTP helpers
def http_get(url, referer=None, headers=None, timeout=HTTP_TIMEOUT):
    h = dict(UA)
    if referer:
        h["Referer"] = referer
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return {"status": r.status, "text": r.read().decode("utf-8", "replace"), "error": None}
    except Exception as e:
        return {"status": None, "text": "", "error": f"{type(e).__name__}: {str(e)[:120]}"}


def _num(x):
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = re.sub(r"[^0-9.\-]", "", str(x))
    try:
        return float(s) if s not in ("", "-", ".") else None
    except ValueError:
        return None


# ---------------------------------------------------------------- per-brand parse
def parse_oppo(text):
    d = json.loads(text)
    out = []
    for item in (d.get("data") or []):
        if isinstance(item, dict) and item.get("partPrice") is not None:
            p = _num(item.get("partPrice"))
            if p is not None:
                out.append((item.get("partName") or item.get("typeName") or "?", p))
    return out


def parse_vivo(text):
    d = json.loads(text)
    vo = (d.get("data") or {}).get("sparePartVO") or {}
    out = []
    for it in (vo.get("sparePartsVoList") or []):
        p = _num(it.get("price"))
        if p is not None:
            out.append((it.get("name") or "?", p))
    return out


def parse_xiaomi(text):
    m = re.match(r"^\s*[A-Za-z_]+\((.*)\)\s*;?\s*$", text, re.S)
    j = m.group(1) if m else text
    d = json.loads(j)
    out = []
    mats = (((d.get("data") or {}).get("materials")) or [])
    for it in mats:
        if not isinstance(it, dict):
            continue
        nm = it.get("shop_material_class_name") or it.get("material_name") or it.get("name")
        # out_warranty_price 多为 0（未填充），以 sale_price 为准
        pr = it.get("out_warranty_price")
        if _num(pr) in (None, 0):
            pr = it.get("sale_price")
        p = _num(pr)
        if nm and p is not None and p > 0:
            out.append((str(nm), p))
    return out


# ---------------------------------------------------------------- DB load
def db():
    return sqlite3.connect(str(DB))


def sample_models(brand, cc, k=SAMPLE_K):
    con = db()
    rows = con.execute(
        """SELECT m.id, m.name, m.model_url, m.model_url_kind, m.source_url, m.model_url_verified
           FROM models m JOIN brands b ON b.id=m.brand_id
           WHERE b.name=? AND m.country_code=?
           ORDER BY m.model_url_verified DESC, m.id LIMIT ?""", (brand, cc, k)).fetchall()
    con.close()
    return rows


def db_prices(model_id):
    con = db()
    q = con.execute("SELECT MAX(quarter) FROM price_snapshots ps JOIN parts pt ON pt.id=ps.part_id "
                    "WHERE pt.model_id=?", (model_id,)).fetchone()[0]
    if not q:
        con.close(); return [], None, None
    rows = con.execute(
        """SELECT ps.price, ps.currency FROM price_snapshots ps JOIN parts pt ON pt.id=ps.part_id
           WHERE pt.model_id=? AND ps.quarter=? ORDER BY ps.price""", (model_id, q)).fetchall()
    con.close()
    prices = [r[0] for r in rows if r[0] is not None]
    cur = rows[0][1] if rows else None
    return prices, q, cur


# ---------------------------------------------------------------- compare
def compare_prices(official, db_prices, tol=1.0):
    """返回 (matched, missing_in_official, extra_in_official)。容差 = max(1, 1%)。"""
    off = sorted([p for p in official if p is not None])
    dbp = sorted([p for p in db_prices if p is not None])
    used = [False] * len(off)
    matched = 0
    missing = []  # DB 有、官方没有
    for dp in dbp:
        t = max(tol, abs(dp) * 0.01)
        hit = -1
        for i, op in enumerate(off):
            if not used[i] and abs(op - dp) <= t:
                hit = i; break
        if hit >= 0:
            used[hit] = True; matched += 1
        else:
            missing.append(dp)
    extra = [off[i] for i in range(len(off)) if not used[i]]
    return matched, missing, extra


# ---------------------------------------------------------------- official extract
async def extract_official(brand, cc, row, browser):
    mid, name, model_url, kind, source_url, verified = row
    url = model_url or source_url
    if brand in ("oppo", "vivo", "xiaomi"):
        ref = {"oppo": f"https://www.oppo.com/{cc}/", "vivo": f"https://www.vivo.com/{cc}/support",
               "xiaomi": "https://www.mi.com/service/materialprice"}.get(brand)
        hdr = {"Accept": "*/*", "Referer": ref} if brand == "xiaomi" else None
        r = http_get(url, referer=ref, headers=hdr)
        if r["status"] != 200:
            return {"status": "skipped", "reason": f"HTTP {r['status']} {r['error']}", "prices": [], "parts": []}
        try:
            if brand == "oppo":
                parts = parse_oppo(r["text"])
            elif brand == "vivo":
                parts = parse_vivo(r["text"])
            else:
                parts = parse_xiaomi(r["text"])
        except Exception as e:
            return {"status": "skipped", "reason": f"parse-fail {type(e).__name__}: {e}", "prices": [], "parts": []}
        prices = [p for _, p in parts]
        return {"status": "ok", "reason": f"{len(parts)} parts", "prices": prices,
                "parts": [n for n, _ in parts]}
    if brand == "apple":
        from crawler.model_links import harvest_apple, norm, APPLE_LOCALE
        if browser is None:
            return {"status": "skipped", "reason": "no-browser", "prices": [], "parts": []}
        try:
            out, diag = await harvest_apple(browser, cc, [{"id": mid, "name": name}])
        except Exception as e:
            return {"status": "skipped", "reason": f"browser-err {e}", "prices": [], "parts": []}
        if not out or not out[0].get("verified"):
            return {"status": "skipped", "reason": diag.get("error", "unmatched-in-api"),
                    "prices": [], "parts": []}
        svc = out[0]["locator"].get("services") or []
        prices = [_num(s.get("price")) for s in svc]
        prices = [p for p in prices if p is not None]
        return {"status": "ok", "reason": f"{len(prices)} services", "prices": prices,
                "parts": [s.get("label") for s in svc]}
    if brand == "samsung":
        if browser is None:
            return {"status": "skipped", "reason": "no-browser", "prices": [], "parts": []}
        from crawler.core import open_page
        from crawler.model_links import norm
        page = await browser.new_page(locale="")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(8000)
            text = await page.evaluate("() => document.body.textContent || ''")
        except Exception as e:
            await page.close()
            return {"status": "skipped", "reason": f"browser-err {e}", "prices": [], "parts": []}
        await page.close()
        # 启发式：本机型名出现 + 附近价格数字
        present = norm(name) in norm(text)
        nums = [float(x) for x in re.findall(r"(?<!\d)([0-9][0-9,]{2,7})(?!\d)", text.replace(",", ""))]
        nums = [n for n in nums if 10 <= n <= 5_000_000]
        # 与 DB 价格多重集比对（low-confidence）
        return {"status": "heuristic" if present else "skipped",
                "reason": f"model_present={present}, {len(nums)} page-nums",
                "prices": nums, "parts": []}
    return {"status": "skipped", "reason": "unknown-brand", "prices": [], "parts": []}


# ---------------------------------------------------------------- main
async def main():
    con = db()
    combos = con.execute(
        """SELECT b.name, m.country_code, count(*) FROM models m JOIN brands b ON b.id=m.brand_id
           GROUP BY b.name, m.country_code ORDER BY 1,2""").fetchall()
    con.close()

    browser = None
    need_browser = any(b in ("apple", "samsung") for b, _, _ in combos)
    if need_browser:
        try:
            from crawler.core import launch_browser
            _, browser = await launch_browser()
        except Exception as e:
            print(f"[warn] 浏览器启动失败，apple/samsung 将跳过: {e}", flush=True)
            browser = None

    report = {"generated_at": datetime.now().isoformat(), "sampling_k": SAMPLE_K, "combos": []}
    print(f"T2 抽样验证开始：{len(combos)} 个品牌×国家组合，每组合抽 {SAMPLE_K} 台\n")
    for brand, cc, total in combos:
        samples = sample_models(brand, cc)
        if not samples:
            continue
        combo_res = {"brand": brand, "country": cc, "models_in_db": total,
                     "samples": [], "summary": {}}
        ok = skip = fail = heur = 0
        for row in samples:
            mid, name = row[0], row[1]
            off = await extract_official(brand, cc, row, browser)
            dbp, q, cur = db_prices(mid)
            rec = {"model": name, "quarter": q, "official_status": off["status"],
                   "official_n": len(off["prices"]), "db_n": len(dbp), "reason": off["reason"],
                   "verdict": "SKIP",
                   "official_prices": sorted([p for p in off["prices"] if p is not None]),
                   "db_prices": sorted([p for p in dbp if p is not None]),
                   "currency": cur}
            if off["status"] in ("ok", "heuristic") and dbp:
                matched, missing, extra = compare_prices(off["prices"], dbp)
                n = len(dbp)
                rec["matched"] = matched
                rec["missing_in_official"] = missing
                rec["extra_in_official"] = extra
                rec["price_match_rate"] = round(matched / n, 3) if n else None
                if off["status"] == "heuristic":
                    rec["verdict"] = "HEUR"
                elif matched == n and not missing and not extra:
                    rec["verdict"] = "PASS"
                else:
                    rec["verdict"] = "MISMATCH"
            if rec["verdict"] == "PASS":
                ok += 1
            elif rec["verdict"] == "MISMATCH":
                fail += 1
            elif rec["verdict"] == "HEUR":
                heur += 1
            else:
                skip += 1
            combo_res["samples"].append(rec)
            tag = rec["verdict"]
            print(f"  [{brand}/{cc}] {name[:26]:26} {tag:8} off={off['status']}({len(off['prices'])}) "
                  f"db={len(dbp)} match={rec.get('matched','-')} rate={rec.get('price_match_rate','-')}")
        combo_res["summary"] = {"pass": ok, "mismatch": fail, "skip": skip, "heuristic": heur,
                                "n_samples": len(combo_res["samples"])}
        report["combos"].append(combo_res)
        print(f"  -> {brand}/{cc}: PASS={ok} MISMATCH={fail} SKIP={skip} HEUR={heur}\n")

    if browser:
        try:
            await browser.close()
        except Exception:
            pass

    out = ROOT / "output" / "verify_t2.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    # 汇总
    tot_pass = sum(c["summary"]["pass"] for c in report["combos"])
    tot_mis = sum(c["summary"]["mismatch"] for c in report["combos"])
    tot_skip = sum(c["summary"]["skip"] for c in report["combos"])
    tot_heur = sum(c["summary"]["heuristic"] for c in report["combos"])
    print(f"\n===== T2 汇总 =====")
    print(f"  PASS={tot_pass}  MISMATCH={tot_mis}  SKIP(网络/限流)={tot_skip}  HEURISTIC(三星)={tot_heur}")
    print(f"  报告: {out}")
    return report


if __name__ == "__main__":
    asyncio.run(main())
