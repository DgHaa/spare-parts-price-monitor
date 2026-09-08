"""实证核查：失败机型的当前 model_url 标识符 + 官方 API 真实价。
用于决定 vivo(修链接) / xiaomi(重抓价) 的修复方案。
"""
import sqlite3, json, re, urllib.request, urllib.parse, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "spare_parts.db"

MI_CLASS_LIST = "https://api2.service.order.mi.com/repair_price/shop_class_info?keyword=&callback=CALLBACK"
MI_PRICE = "https://api2.service.order.mi.com/repair_price/shop_band_wx_price?class_id={cid}&callback=cb"


def direct_get(url, referer=None, timeout=15):
    headers = {"User-Agent": "Mozilla/5.0", "Referer": referer or ""}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"status": resp.status, "text": resp.read().decode("utf-8", "replace")}
    except Exception as e:
        return {"status": 0, "error": f"{type(e).__name__}: {str(e)[:140]}"}


def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c


def vivo_dropdown_options(cc):
    """抓 vivo 配件页，解析 li.select-model-item 的 data-id 与 label。"""
    url = f"https://www.vivo.com/{cc}/support/accessory"
    r = direct_get(url, referer=url)
    if r["status"] != 200:
        return None, f"HTTP {r['status']} {r.get('error','')}"
    html = r["text"]
    opts = re.findall(r'<li[^>]*class="[^"]*select-model-item[^"]*"[^>]*data-id="([^"]+)"[^>]*>(.*?)</li>',
                      html, re.S)
    if not opts:
        # 退而求其次：任意 data-id + 附近文本
        opts = re.findall(r'data-id="(\d+)"[^>]*>([^<]{2,40})<', html)
    out = []
    for oid, label in opts:
        label = re.sub(r"<[^>]+>", "", label).strip()
        out.append((label, oid))
    return out, f"{len(out)} opts"


def find_xiaomi_cid(name_hint):
    r = direct_get(MI_CLASS_LIST, referer="https://www.mi.com/service/materialprice")
    if r["status"] != 200:
        return None, f"HTTP {r['status']}"
    txt = r["text"]
    m = re.search(r"\((.*)\)", txt, re.S)  # JSONP: CALLBACK({...})
    if not m:
        return None, "no jsonp body"
    try:
        data = json.loads(m.group(1))
    except Exception as e:
        return None, f"json {e}"
    # 递归找 name->class_id
    acc = {}
    def walk(o, depth=0):
        if depth > 8: return
        if isinstance(o, dict):
            nm = None
            for k in ("name", "class_name", "className", "title", "label", "goods_name", "product_name"):
                if isinstance(o.get(k), str) and o[k].strip():
                    nm = o[k].strip(); break
            cid = None
            for k in ("class_id", "classId", "id", "goods_id", "goodsId"):
                v = o.get(k)
                if isinstance(v, (int, str)) and str(v).strip().isdigit():
                    cid = str(v).strip(); break
            if nm and cid:
                acc[nm] = cid
            for v in o.values(): walk(v, depth+1)
        elif isinstance(o, list):
            for v in o: walk(v, depth+1)
    walk(data)
    hits = {k: v for k, v in acc.items() if name_hint.lower().replace(" ", "") in k.lower().replace(" ", "")}
    return hits, f"{len(acc)} entries"


def xiaomi_price(cid):
    url = MI_PRICE.format(cid=cid)
    r = direct_get(url, referer="https://www.mi.com/service/materialprice")
    if r["status"] != 200:
        return None, f"HTTP {r['status']}"
    m = re.search(r"cb\((.*)\)", r["text"], re.S)
    if not m: return None, "no jsonp"
    d = json.loads(m.group(1))
    body = d.get("data", {})
    mats = body.get("materials") if isinstance(body, dict) else None
    if not mats:
        return None, f"no materials, code={d.get('code')}"
    prices = [(x.get("shop_material_class_name"), x.get("sale_price")) for x in mats]
    return prices, f"{len(prices)} materials code={d.get('code')}"


def vivo_price(mid, cc):
    url = f"https://www.vivo.com/{cc}/support/queryPriceByProductId?id={mid}"
    r = direct_get(url, referer=f"https://www.vivo.com/{cc}/support/accessory")
    if r["status"] != 200:
        return None, f"HTTP {r['status']}"
    try:
        j = json.loads(r["text"])
    except Exception as e:
        return None, f"json {e}"
    lst = (((j.get("data") or {}).get("sparePartVO") or {}).get("sparePartsVoList") or [])
    prices = [(p.get("partName"), p.get("partPrice")) for p in lst]
    return prices, f"{len(prices)} parts"


def db_prices(model_id):
    c = db()
    q = c.execute("SELECT MAX(quarter) FROM price_snapshots ps JOIN parts pt ON pt.id=ps.part_id WHERE pt.model_id=?",
                  (model_id,)).fetchone()[0]
    rows = c.execute("""SELECT ps.price, ps.currency, pt.name FROM price_snapshots ps JOIN parts pt ON pt.id=ps.part_id
                        WHERE pt.model_id=? AND ps.quarter=? ORDER BY ps.price""", (model_id, q)).fetchall()
    c.close()
    return q, [(r[0], r[1], r[2]) for r in rows]


def db_model(brand, country, name_like):
    c = db()
    rows = c.execute("""SELECT m.id, m.name, m.model_url, m.source_url, m.model_url_verified
                        FROM models m JOIN brands b ON b.id=m.brand_id
                        WHERE b.name=? AND m.country_code=? AND m.name LIKE ?""",
                     (brand, country, f"%{name_like}%")).fetchall()
    c.close()
    return [dict(r) for r in rows]


print("="*70)
print("【XIAOMI/cn Xiaomi 13 Pro】找正确 class_id 并取官方 sale_price")
hits, info = find_xiaomi_cid("Xiaomi 13 Pro")
print("  class_info:", info)
for nm, cid in list(hits.items())[:10]:
    pr, pin = xiaomi_price(cid)
    print(f"  cid={cid} {nm[:30]:30} -> {pin}")
    if pr:
        print("    官方价:", [float(p) for _, p in pr])
# DB 当前
for mm in db_model("xiaomi", "cn", "Xiaomi 13 Pro"):
    q, rows = db_prices(mm["id"])
    print(f"  DB模型 id={mm['id']} {mm['name']}  q={q} verified={mm['model_url_verified']}")
    print("    DB价:", [float(r[0]) for r in rows])

print("="*70)
print("【VIVO/ae T1 5G】当前 model_url + 下拉正确 data-id")
for mm in db_model("vivo", "ae", "T1 5G"):
    print(f"  DB模型 id={mm['id']} {mm['name']} model_url={mm['model_url']} verified={mm['model_url_verified']}")
    q, rows = db_prices(mm["id"])
    print("    DB价:", sorted(float(r[0]) for r in rows))
    # 当前 model_url 的价
    mid = re.search(r"id=(\d+)", mm["model_url"] or "")
    if mid:
        pr, pin = vivo_price(mid.group(1), "ae")
        print(f"    当前 model_url(id={mid.group(1)}) 价: {pin}", [float(p) for _, p in pr] if pr else "")
opts, oin = vivo_dropdown_options("ae")
print("  vivo/ae 下拉:", oin)
for label, oid in opts:
    if "t1" in label.lower().replace(" ", "") or "5g" in label.lower():
        print(f"    候选: {label} -> id={oid}")
