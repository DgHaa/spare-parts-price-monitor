"""tools/shoot_official.py - 为校验报告抓取「官方源价格凭据」截图

对 verify_t2.json 中 apple / samsung 的抽样机型，取其在库内的官方源 URL：
- Apple：model_url 是定价 API，把返回的 JSON 解析成干净价格表后截图。
- Samsung：source_url 是官网维修费用人读页，直接截图。

截图存到 output/shots/，并写出 manifest.json 供报告生成器嵌入。

用法：
  python tools/shoot_official.py            # 截全部 apple+samsung
  APPLE_ONLY=1 python tools/shoot_official.py
  SAMSUNG_ONLY=1 python tools/shoot_official.py
"""
import json, sqlite3, os, re, urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "spare_parts.db"
T2 = ROOT / "output" / "verify_t2.json"
SHOTS = ROOT / "output" / "shots"
SHOTS.mkdir(parents=True, exist_ok=True)
MANIFEST = SHOTS / "manifest.json"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def get_proxy():
    s = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
         or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
         or os.environ.get("ALL_PROXY") or os.environ.get("all_proxy"))
    return {"server": s} if s else None


def slug(brand, cc, model):
    return re.sub(r"[^A-Za-z0-9]+", "_", f"{brand}_{cc}_{model}")[:90]


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def apple_api_json(url):
    """Fetch Apple pricing API and return parsed dict."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Referer": "https://support.apple.com/",
                 "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def apple_extract_services(data, model_name):
    """从 Apple API 响应中找到匹配机型的 services 列表。"""
    products = data.get("products") or []
    candidates = []
    for prod in products:
        for child in prod.get("childrenProducts") or []:
            titles = [child.get(k, "") for k in ("product_eng_title", "product_loc_title")]
            candidates.append((child, titles))
    # 优先匹配 model_name（去掉大小写/空格/符号）
    target = norm(model_name)
    for child, titles in candidates:
        if any(target in norm(t) or norm(t) in target for t in titles if t):
            return child.get("services") or []
    # 退而求其次：只有一个子产品时直接用它
    if len(candidates) == 1:
        return candidates[0][0].get("services") or []
    return []


def apple_price_card_html(model_name, url, services):
    """把 Apple services 渲染成一张干净的价格凭据 HTML。"""
    rows = []
    for s in services:
        label = s.get("serviceLabel", "")
        price = s.get("price", "")
        rows.append(f"<tr><td>{html_esc(label)}</td><td class='price'>{html_esc(price)}</td></tr>")
    rows_html = "\n".join(rows) if rows else "<tr><td colspan='2'>无价格项</td></tr>"
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
       margin: 0; padding: 24px; background: #f5f5f7; color: #1d1d1f; }}
.card {{ background: #fff; border-radius: 16px; padding: 28px; max-width: 560px; margin: 0 auto;
        box-shadow: 0 8px 24px rgba(0,0,0,.08); }}
h2 {{ margin: 0 0 6px 0; font-size: 22px; }}
.sub {{ color: #6e6e73; font-size: 13px; margin-bottom: 20px; word-break: break-all; }}
table {{ width: 100%; border-collapse: collapse; font-size: 15px; }}
th {{ text-align: left; color: #6e6e73; font-weight: 600; border-bottom: 1px solid #d2d2d7; padding: 10px 8px; }}
td {{ padding: 12px 8px; border-bottom: 1px solid #f0f0f2; }}
td.price {{ text-align: right; font-weight: 600; font-variant-numeric: tabular-nums; }}
.note {{ margin-top: 18px; font-size: 12px; color: #6e6e73; }}
</style></head><body>
<div class="card">
  <h2>{html_esc(model_name)}</h2>
  <div class="sub">Apple 官方维修定价 API（解析渲染）<br>{html_esc(url)}</div>
  <table><thead><tr><th>维修项目</th><th style="text-align:right">官方价格</th></tr></thead>
  <tbody>{rows_html}</tbody></table>
  <div class="note">来源：support.apple.com 定价接口 · 与 verify_t2 抓取的官方价格一致</div>
</div>
</body></html>"""


def html_esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def load_manifest():
    if MANIFEST.exists():
        try:
            return json.load(open(MANIFEST, encoding="utf-8"))
        except Exception:
            pass
    return {}


def main():
    only = os.environ.get("SAMSUNG_ONLY") and "samsung" or (os.environ.get("APPLE_ONLY") and "apple" or None)
    rep = json.load(open(T2, encoding="utf-8"))
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    targets = []
    for c in rep["combos"]:
        if c["brand"] not in ("apple", "samsung"):
            continue
        if only and c["brand"] != only:
            continue
        for s in c["samples"]:
            r = con.execute(
                "SELECT m.model_url, m.source_url, m.model_url_kind FROM models m "
                "JOIN brands b ON b.id=m.brand_id WHERE b.name=? AND m.country_code=? AND m.name=?",
                (c["brand"], c["country"], s["model"])).fetchone()
            if not r:
                continue
            # Apple 用 API 生成价格表；Samsung 用人读页
            shot_url = r["source_url"] if c["brand"] == "samsung" else (r["model_url"] or r["source_url"])
            link_url = r["model_url"] or r["source_url"]
            if not shot_url:
                continue
            targets.append({"brand": c["brand"], "country": c["country"], "model": s["model"],
                            "shot_url": shot_url, "link_url": link_url, "verdict": s["verdict"],
                            "kind": (r["model_url_kind"] if r else "")})
    con.close()
    print(f"[shots] {len(targets)} 个官方源待截图", flush=True)

    manifest = load_manifest()
    proxy = get_proxy()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"], proxy=proxy)
        for t in targets:
            key = f"{t['brand']}|{t['country']}|{t['model']}"
            fname = slug(t["brand"], t["country"], t["model"]) + ".jpg"
            fpath = SHOTS / fname
            page = None
            try:
                if t["brand"] == "apple":
                    # 解析 API → 渲染价格表 → 截图
                    data = apple_api_json(t["shot_url"])
                    services = apple_extract_services(data, t["model"])
                    html = apple_price_card_html(t["model"], t["shot_url"], services)
                    page = browser.new_page(viewport={"width": 660, "height": 900}, user_agent=UA)
                    page.set_content(html, wait_until="domcontentloaded")
                    page.wait_for_timeout(500)
                    # 按内容高度截图
                    page.screenshot(path=str(fpath), type="jpeg", quality=80, full_page=True)
                else:
                    # Samsung 真实官网页
                    page = browser.new_page(viewport={"width": 1400, "height": 1200}, user_agent=UA)
                    page.goto(t["shot_url"], wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(4000)
                    # 尝试展开/滚动到机型名附近
                    try:
                        model = t["model"].replace("Galaxy ", "").split()[0]
                        page.evaluate(
                            f"() => {{ const m='{model}'; const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT,null,false);"
                            "let n; while(n=walker.nextNode()){ if(n.textContent.includes(m)){ const el=n.parentElement; el.scrollIntoView({block:'center'}); return true; } } return false; }")
                        page.wait_for_timeout(800)
                    except Exception:
                        pass
                    page.screenshot(path=str(fpath), type="jpeg", quality=75, full_page=True)
                manifest[key] = {"file": fname, "url": t["link_url"], "shot_url": t["shot_url"],
                                 "verdict": t["verdict"], "kind": t["kind"],
                                 "brand": t["brand"], "country": t["country"], "model": t["model"]}
                print("OK ", fname, t["verdict"], flush=True)
            except Exception as e:
                print("ERR", fname, type(e).__name__, str(e)[:120], flush=True)
            finally:
                try:
                    if page:
                        page.close()
                except Exception:
                    pass
        browser.close()
    json.dump(manifest, open(MANIFEST, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print("[shots] 完成，manifest 记录", len(manifest), "张", flush=True)


if __name__ == "__main__":
    main()
