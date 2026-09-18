"""tools/probe_mc_m.py - dump #mc-m 中 Pad 5 相关选项的精确 label/value(JSON 转义)。

用于排查 select_option(label=...) 精确匹配失败的原因（不可见字符 / 空格类型 / value 差异）。
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))  # 用 append 即可（原 queue.py 遮蔽 stdlib 的问题已于 2026-09-17 改名 issue_queue.py 根治）

from crawler.core import launch_browser  # noqa: E402

JS = """
() => {
  const s = document.querySelector('#mc-m');
  if (!s) return {err: 'no #mc-m'};
  const out = [];
  for (const o of s.options) {
    if (o.textContent.includes('Pad 5') || o.value.includes('Pad 5')) {
      out.push({value: o.value, text: o.textContent, label: o.label});
    }
  }
  return {count: s.options.length, cur: s.value, hits: out};
}
"""


async def main():
    pw, b = await launch_browser()
    p = await b.new_page(viewport={"width": 1440, "height": 1000})
    try:
        await p.goto("http://127.0.0.1:8000/", wait_until="domcontentloaded", timeout=20000)
        await p.wait_for_timeout(5000)
        await p.get_by_text("比价矩阵", exact=False).first.click(timeout=8000)
        await p.wait_for_timeout(2500)
        await p.select_option("#mc-b", label="oppo", timeout=8000)
        await p.wait_for_timeout(3000)
        r = await p.evaluate(JS)
        print("total options:", r.get("count"), "current:", repr(r.get("cur")))
        for h in r.get("hits", []):
            print("  value=", json.dumps(h["value"], ensure_ascii=False))
            print("  text =", json.dumps(h["text"], ensure_ascii=False))
            print("  label=", json.dumps(h["label"], ensure_ascii=False))
            print("  ---")
    finally:
        sys.stdout.flush()
        for c in (p.close(), b.close(), pw.stop()):
            try:
                await asyncio.wait_for(c, timeout=8)
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(main())
