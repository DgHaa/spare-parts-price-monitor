#!/usr/bin/env python3
"""google_calibrate.py - Google Pixel 维修估价「一键校准器」（在用户真实出口 IP 的本机运行）。

⚠️ 本脚本无法在 WorkBuddy 沙箱运行：整个 Google 域(store.google.com / www.google.com)
   从沙箱 000 不可达（数据中心 IP 被 Google 限流/地理封锁，curl/WebFetch 均超时）。
   同样，在中国大陆 Google 被墙，本机直连也会 000 —— 必须挂代理/VPN 才能跑通。

真实路径（全部相对，仓库可整体搬迁）：

    cd <仓库根>/references/calibration
    pip install playwright && playwright install chromium
    python google_calibrate.py                # 默认校准全部 5 国并自动写回 KB
    # 或仅指定区域： python google_calibrate.py --regions de jp

  Chromium 位置无需配置：优先读环境变量 PLAYWRIGHT_CHROMIUM_PATH，
  其次扫描 %LOCALAPPDATA%/ms-playwright/chromium-*/chrome-win64/chrome.exe，
  都没有则直接用 Playwright 自带的 Chromium（见 _paths.find_chromium）。

代理支持（自动读取，无需改代码）：
  优先级 1：环境变量 HTTPS_PROXY/HTTP_PROXY/ALL_PROXY（见下）。
  优先级 2：Windows 系统代理(Internet 选项) —— 若 Clash/v2rayN 已开『系统代理』
           （注册表 ProxyEnable=1），脚本自动读取 ProxyServer，无需手动 set。
  环境变量取值示例（http/https 代理，如 http://127.0.0.1:7890）：
    HTTPS_PROXY / https_proxy
    HTTP_PROXY  / http_proxy
    ALL_PROXY    / all_proxy    （SOCKS5 代理，如 socks5://127.0.0.1:1080）
  例（Windows cmd）： set HTTPS_PROXY=http://127.0.0.1:7890
  例（PowerShell）：  $env:HTTPS_PROXY="http://127.0.0.1:7890"
  例（Git Bash）：    export HTTPS_PROXY=http://127.0.0.1:7890
  两者均无则直连（中国大陆会 000，属正常——说明本机未挂代理）。

它会自动完成：
  1) 对每个区域加载 store.google.com/<国>/repair-cost-estimator?hl=<语>；
  2) 探测设备选择器 -> 尝试选 Pixel；
  3) 抽取维修价行（屏幕/电池/后盖/充电口等一口价，含零件+人工）；
  4) 截图存 .../spare-parts-price/output/evidence/google_<国>_cal.png；
  5) 写 references/kb/google_locators.json（供 executor 的 google_estimator 使用精确定位器）；
  6) 智能把 google.json 中该区域状态更新：
       - 抽到 ≥2 条且含维修关键词的价行  -> verified（带证据截图 + 时间戳）
       - 页面需 IMEI/序列号才出价 或 加载后无价行 -> unavailable（诚实标注，不写假价）
       - 导航/加载失败               -> blocked（保留并记真实错误，可重跑）

重要：Google 官方估价流程可能需在 store.google.com/repair 输入 IMEI/序列号才出价，
未必是干净的可选机型价表。脚本会检测 IMEI 输入框；若页面只要求设备标识而无公开价表，
则诚实标 unavailable，绝不伪造价表写进 KB。
"""
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import EVIDENCE_DIR, KB_DIR, HERE, ensure_evidence  # noqa: E402

REGIONS = {
    "de": ("https://store.google.com/de/repair-cost-estimator?hl=de", "de-DE", "EUR"),
    "jp": ("https://store.google.com/jp/repair-cost-estimator?hl=ja", "ja-JP", "JPY"),
    "ae": ("https://store.google.com/ae/repair-cost-estimator?hl=en", "en-AE", "AED"),
    "my": ("https://store.google.com/my/repair-cost-estimator?hl=en", "en-MY", "MYR"),
    "tr": ("https://store.google.com/tr/repair-cost-estimator?hl=tr", "tr-TR", "TRY"),
}

# 维修关键词：价行 label 命中其一才视为可信维修价（避免把无关金额写进 KB）
REPAIR_KW = re.compile(
    r"screen|battery|display|back|glass|charge|port|button|speaker|camera|repair|cost|price|"
    r"修理|維修|维修|電池|电池|螢幕|屏幕|価格|費|kamera|pil|ekran|anakart|onar",
    re.I,
)
PRICE_RE = re.compile(
    r"[€$¥£AEDRM]\s?[\d.,]+|[\d.,]+\s?(円|€|\$|¥|£|AED|RM)|[\d,]+\s?円|"
    r"[\d][\d.,]*\s?(TRY|EUR|USD|JPY|MYR)",
    re.I,
)


def price_of(text):
    if not text:
        return None
    m = re.search(r"(?:[€¥£$]|[A-Z]{2,3})\s*([\d][\d.,]*)", text)
    if m:
        return float(m.group(1).replace(",", ""))
    m = re.search(r"([\d][\d.,]*)\s*[A-Z]{2,3}", text)
    if m:
        return float(m.group(1).replace(",", ""))
    m = re.search(r"([\d][\d.,]*)", text)
    if m:
        return float(m.group(1).replace(",", ""))
    return None


async def calibrate_region(pg, area, url, locale, cur):
    rec = {"area": area, "currency": cur, "url": url}
    try:
        await pg.goto(url, wait_until="commit", timeout=30000)
        await pg.wait_for_timeout(6000)
        rec["title"] = await pg.title()
        rec["final_url"] = pg.url

        # 检测 IMEI/序列号输入框（Google 真实估价常需设备标识）
        imei = await pg.evaluate("""() => {
            const inp = [...document.querySelectorAll('input')];
            return inp.some(i => (i.placeholder || i.getAttribute('aria-label') || i.name || '').match(/imei|serial|序號|序列号|serial number/i));
        }""")
        rec["needs_imei"] = bool(imei)

        # 尝试设备选择器 -> 选 Pixel
        dev = pg.locator(".repair-device-select, [data-test='device'], select, [role='combobox']").first
        if await dev.count():
            try:
                if await dev.evaluate("e => e.tagName") == "SELECT":
                    opts = await dev.locator("option").all_inner_texts()
                    pick = next((o for o in opts if "pixel" in (o or "").lower()), (opts[0] if opts else None))
                    if pick:
                        await dev.select_option(label=pick)
                else:
                    await dev.click(timeout=6000)
                    px = pg.locator("li:has-text('Pixel'), [role='option']:has-text('Pixel')").first
                    if await px.count():
                        await px.click(timeout=6000)
            except Exception as e:
                rec.setdefault("warns", []).append(f"device select: {e}")
            await pg.wait_for_timeout(3000)

        # 抽取价行：先试候选容器，再回退整页
        info = await pg.evaluate("""() => {
            const PRICE = /[€$¥£AEDRM]\\s?[\\d.,]+|[\\d.,]+\\s?(円|€|$|¥|£|AED|RM)|[\\d,]+\\s?円|[\\d][\\d.,]*\\s?(TRY|EUR|USD|JPY|MYR)/i;
            const cand = [document.querySelector('table'), document.querySelector('[role=table]'),
                          document.querySelector('[data-test*=price]'), document.querySelector('.repair-prices')].filter(Boolean);
            let best = [];
            for (const c of cand) {
                const ls = (c.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
                if (ls.length > best.length) best = ls;
            }
            const L = (document.body.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
            return {candidate_lines: best, all_lines: L};
        }""")
        src = info["candidate_lines"] or info["all_lines"]
        rows = []
        for i in range(len(src)):
            l = src[i]
            if PRICE_RE.search(l) and len(l) < 60:
                lab = src[i - 1] if (i > 0 and not PRICE_RE.search(src[i - 1])) else ""
                rows.append({"label": lab, "price": l})
        rec["price_rows"] = rows
        rec["confident"] = bool(len(rows) >= 2 and any(REPAIR_KW.search(r["label"] or "") for r in rows))

        # 证据截图
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        shot = EVIDENCE_DIR / f"google_{area}_cal.png"
        try:
            await pg.screenshot(path=str(shot), full_page=False)
            rec["screenshot"] = str(shot)
        except Exception as e:
            rec.setdefault("warns", []).append(f"screenshot: {e}")

        # best-effort 定位器发现
        rec["locators"] = await pg.evaluate("""() => {
            const c = [...document.querySelectorAll('table, [role=table], [data-test*=price], .repair-prices')].filter(Boolean);
            if (!c.length) return {};
            const el = c.sort((a, b) => (b.innerText || '').length - (a.innerText || '').length)[0];
            const sel = el.id ? ('#' + el.id)
                        : (el.getAttribute('data-test') ? ("[data-test=\\"" + el.getAttribute('data-test') + "\\"]")
                        : el.tagName.toLowerCase());
            return {price_container: sel, row_selector: el.tagName.toLowerCase() == 'table' ? 'tr' : '[role=row], div'};
        }""")

        if rec["needs_imei"] and not rows:
            rec["outcome"] = "unavailable_imei"
        elif rec["confident"]:
            rec["outcome"] = "verified"
        elif rec["needs_imei"]:
            rec["outcome"] = "unavailable_imei"
        else:
            rec["outcome"] = "no_prices"
    except Exception as e:
        rec["outcome"] = "error"
        rec["error"] = str(e)[:300]
    return rec


def apply_to_kb(results):
    gpath = KB_DIR / "google.json"
    if not gpath.exists():
        print(f"[warn] 未找到 {gpath}，跳过 KB 写回")
        return
    data = json.loads(gpath.read_text(encoding="utf-8"))
    now = datetime.now().strftime("%Y-%m-%d")
    ts = datetime.now().strftime("%Y-%m-%dT%H:%M")
    locs_doc = {
        "updated_at": ts,
        "needs_imei": any(r.get("needs_imei") for r in results.values()),
        "regions": {},
        "default": {},
    }
    for area, r in results.items():
        recs = data["countries"].get(area, [])
        rec = recs[0] if recs else None
        if rec is None:
            continue
        shot = r.get("screenshot", "")
        oc = r.get("outcome")
        if oc == "verified":
            rec["status"] = "verified"
            rec["reviewed"] = True
            rec["evidence"] = {"screenshot": shot, "captured_url": r.get("final_url") or r["url"], "captured_at": ts}
            rec["notes"] = (
                f"【{ts} 真实 IP 校准】store.google.com/{area}/repair-cost-estimator 选 Pixel 后取到维修一口价"
                f"（样本：{r['price_rows'][:3]}）。币种 {rec['currency']}。")
            if r.get("locators"):
                locs_doc["regions"][area] = r["locators"]
        elif oc in ("unavailable_imei", "no_prices"):
            rec["status"] = "unavailable"
            rec["reviewed"] = True
            rec["evidence"] = {"screenshot": shot, "captured_url": r.get("final_url") or r["url"], "captured_at": ts}
            why = "需输入 IMEI/序列号才出价，无公开可抓价表" if r.get("needs_imei") else "加载后未取到维修价行"
            rec["notes"] = f"【{ts} 真实 IP 校准】该区域 Google 估价页{why}。币种 {rec['currency']}。"
        else:  # error / 导航失败
            rec["status"] = "blocked"
            rec["notes"] = f"【{ts} 校准失败】{r.get('error', '')}。请检查本机网络/Google 可达性后重跑本脚本。"
        rec["updated_at"] = now
    if locs_doc["regions"]:
        locs_doc["default"] = next(iter(locs_doc["regions"].values()))
    (KB_DIR / "google_locators.json").write_text(
        json.dumps(locs_doc, ensure_ascii=False, indent=2), encoding="utf-8")
    gpath.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"KB {gpath.name} 已更新；{KB_DIR / 'google_locators.json'} 已写入。")


def get_proxy():
    """读取出口代理，优先级：环境变量 > Windows 系统代理(Internet 选项)。返回 Playwright proxy 字典或 None。"""
    import os
    raw = (
        os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
        or os.environ.get("ALL_PROXY") or os.environ.get("all_proxy")
    )
    if not raw:
        raw = _windows_system_proxy()
    if not raw:
        return None
    server = raw.strip()
    if server.startswith("socks5h://"):
        server = "socks5://" + server[len("socks5h://"):]
    return {"server": server, "bypass": "localhost,127.0.0.1,<-loopback>"}


def _windows_system_proxy():
    """读取 HKCU Internet Settings 的系统代理（Clash/v2rayN 开启『系统代理』后 ProxyEnable=1 即生效）。"""
    try:
        import winreg
    except Exception:
        return None
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
        if not enabled:
            return None
        server, _ = winreg.QueryValueEx(key, "ProxyServer")
    except Exception:
        return None
    if not server:
        return None
    server = server.strip()
    # 形如 "http=127.0.0.1:7890;https=127.0.0.1:7890;socks=127.0.0.1:7891"
    if "=" in server:
        for part in server.split(";"):
            k, _, v = part.partition("=")
            if k.strip().lower() in ("https", "http"):
                return v.strip()
        return server.split(";")[0].partition("=")[2].strip()
    return server


async def main(regions, out):
    from playwright.async_api import async_playwright
    proxy = get_proxy()
    if proxy:
        print(f"[proxy] 使用出口代理: {proxy['server']}", flush=True)
    else:
        print("[proxy] 未检测到代理环境变量，将直连（中国大陆可能 http=000）", flush=True)
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    results = {}
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True, args=["--no-sandbox"], proxy=proxy)
        pg = await b.new_page()
        for area in regions:
            url, locale, cur = REGIONS[area]
            results[area] = await calibrate_region(pg, area, url, locale, cur)
            r = results[area]
            print(f"[{area}] {r.get('outcome')}  rows={len(r.get('price_rows', []))} imei={r.get('needs_imei')}", flush=True)
        await b.close()
    apply_to_kb(results)
    doc = {
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "results": {a: {k: v for k, v in r.items() if k != "locators"} for a, r in results.items()},
    }
    if out:
        Path(out).write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"saved -> {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--regions", nargs="*", default=list(REGIONS.keys()),
                    help="要校准的区域代码，默认全部 5 国(de jp ae my tr)")
    ap.add_argument("--out", default=str(HERE / "google_cal.json"),
                    help="校准结果 JSON 输出路径（默认 google_cal.json）")
    args = ap.parse_args()
    import asyncio
    asyncio.run(main(args.regions, args.out))
