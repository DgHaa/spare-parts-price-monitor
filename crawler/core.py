"""crawler/core.py - 代理感知的浏览器启动 + 导航助手（无 LLM）。

复用 spare-parts-price skill 的抓取配方(executor.run_query)，把结果落 SQLite。
代理优先级：环境变量 HTTPS_PROXY/HTTP_PROXY/ALL_PROXY > Windows 系统代理(Internet 选项)。
"""
import os
import sys
from pathlib import Path

# 指向 skill 的脚本与校准目录，便于直接复用 executor / get_proxy
SKILL_ROOT = Path(r"C:/Users/Dong/.workbuddy/skills/spare-parts-price")
SKILL_SCRIPTS = SKILL_ROOT / "scripts"
SKILL_CALIB = SKILL_ROOT / "references" / "calibration"
for p in (SKILL_SCRIPTS, SKILL_CALIB):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


def get_proxy():
    """读取出口代理，优先级：环境变量 > Windows 系统代理。返回 Playwright proxy 字典或 None。"""
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
    if "=" in server:  # http=127.0.0.1:7890;https=...;socks=...
        for part in server.split(";"):
            k, _, v = part.partition("=")
            if k.strip().lower() in ("https", "http"):
                return v.strip()
        return server.split(";")[0].partition("=")[2].strip()
    return server


async def launch_browser():
    """启动无头 Chromium（代理感知）。返回 (playwright, browser)。"""
    from playwright.async_api import async_playwright
    proxy = get_proxy()
    if proxy:
        print(f"[proxy] 使用出口代理: {proxy['server']}", flush=True)
    else:
        print("[proxy] 未检测到代理，直连（Google 等国可能 http=000）", flush=True)
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"], proxy=proxy)
    return pw, browser


async def open_page(browser, rec, goto_url=None, timeout=25000):
    """按 KB 记录导航并返回已加载的 page。goto_url 可覆盖 entry.url（如 OPPO 用 API 源站确保同源）。

    goto 失败（超时/被墙/反爬）不再抛崩整个流程：内部吞掉异常并标记
    page._goto_failed，交由调用方决定是否跳过。

    导航后若 KB 含 entry.steps（如三星需从支持首页点进『维修费用』子页），则按
    executor.run_step 逐步执行以跳转到真正含价表的子页——与 skill executor.fetch_price
    的行为保持一致（此前 crawler 不执行 steps，导致三星等停留在首页而抽取 0 行）。
    步骤执行容错：任一动作失败仅告警，不中断后续流程。
    """
    page = await browser.new_page(locale=rec.get("locale", ""))
    page._goto_failed = False
    entry = rec.get("entry", {})
    url = goto_url or entry.get("url")
    if url:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        except Exception as e:
            page._goto_failed = True
            print(f"  [warn] 打开页面超时/失败 {url}: {type(e).__name__}: {e}", flush=True)
        await page.wait_for_timeout(3000)
    steps = entry.get("steps") or []
    if steps:
        try:
            from executor import run_step
            from pathlib import Path as _P
            ev_dir = _P("output/evidence")
            shots = []
            for step in steps:
                await run_step(page, step, ev_dir, shots)
            await page.wait_for_timeout(800)
        except Exception as e:
            print(f"  [warn] 执行 entry.steps 失败（容错跳过）: {e}", flush=True)
    # 直接跳到已知价表子页（expect_url）比点击多级菜单稳定：本地化站点菜单文案/可见性易变，
    # 而 expect_url 是校准过的固定价表地址（tr/jp/ae 等）。仅对按 KB 首页入口的品牌生效；
    # api_json（OPPO）走 goto_url 且 expect_url 非价表页，跳过避免误跳。
    expect = entry.get("expect_url")
    if goto_url is None and expect and expect != url:
        try:
            await page.goto(expect, wait_until="domcontentloaded", timeout=timeout)
            await page.wait_for_timeout(3000)
        except Exception as e:
            page._goto_failed = True
            print(f"  [warn] 跳转到 expect_url 失败 {expect}: {type(e).__name__}: {e}", flush=True)
    return page
