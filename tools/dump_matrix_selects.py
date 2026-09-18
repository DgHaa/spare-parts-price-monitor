"""tools/dump_matrix_selects.py - 打印比价矩阵页各 <select> 的选项，便于驱动 SPA 截图。"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

from crawler.core import launch_browser  # noqa: E402


async def main():
    pw, b = await launch_browser()
    p = await b.new_page(viewport={"width": 1440, "height": 1000})
    try:
        await p.goto("http://127.0.0.1:8000/", wait_until="domcontentloaded", timeout=20000)
        await p.wait_for_timeout(5000)
        await p.get_by_text("比价矩阵", exact=False).first.click(timeout=6000)
        await p.wait_for_timeout(3000)
        await p.select_option("#mc-b", label="oppo", timeout=6000)
        await p.wait_for_timeout(3000)
        opts = await p.locator("#mc-m option").all_inner_texts()
        print(f"#mc-m (oppo) 选项 {len(opts)} 个：", flush=True)
        for o in opts:
            print("   ", o, flush=True)
        sels = await p.locator("select").all()
        print("select 数量:", len(sels), flush=True)
        for i, s in enumerate(sels):
            try:
                opts = await s.locator("option").all_inner_texts()
            except Exception as e:
                opts = [f"<err {e}>"]
            print(f"[{i}] id={await s.get_attribute('id')} name={await s.get_attribute('name')} "
                  f"opts({len(opts)})={opts[:8]}", flush=True)
    finally:
        for c in (p.close(), b.close(), pw.stop()):
            try:
                await asyncio.wait_for(c, timeout=8)
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(main())
