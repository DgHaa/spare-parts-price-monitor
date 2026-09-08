import re, urllib.request
from pathlib import Path

def direct_get(url, referer=None, timeout=15):
    headers = {"User-Agent": "Mozilla/5.0", "Referer": referer or ""}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"status": resp.status, "text": resp.read().decode("utf-8", "replace")}
    except Exception as e:
        return {"status": 0, "error": f"{type(e).__name__}: {str(e)[:140]}"}

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

for cc in ("ae", "my", "tr"):
    url = f"https://www.vivo.com/{cc}/support/accessory"
    r = direct_get(url, referer=url)
    print(f"\n===== vivo/{cc} 下拉抓取 status={r['status']} =====")
    html = r.get("text", "")
    # 解析 li.select-model-item 以及任何 data-id
    opts = re.findall(r'<li[^>]*class="[^"]*select-model-item[^"]*"[^>]*data-id="([^"]+)"[^>]*>(.*?)</li>', html, re.S)
    if not opts:
        opts = re.findall(r'data-id="(\d+)"[^>]*>([^<]{2,40})<', html)
    print(f"  静态HTML命中选项数: {len(opts)}")
    for label, oid in opts:
        label = re.sub(r"<[^>]+>", "", label).strip()
        low = label.lower().replace(" ", "")
        if ("t1" in low) or ("5g" in low) or ("seyahat" in low) or ("şarj" in low) or ("charg" in low):
            pr, pin = vivo_price(oid, cc)
            pp = [p for p in (pr or [])]
            print(f"  候选 {label[:30]:30} id={oid} -> {pin}")
            if pr:
                nums = [float(p) for _, p in pr if p is not None]
                print(f"        API价: {sorted(nums)}")
