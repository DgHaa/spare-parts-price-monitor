"""tools/shot_platform.py - 截取本地平台 UI（默认 :8000），用于人读取证。

用法：python tools/shot_platform.py [--url http://127.0.0.1:8000/] [--out output/x.png]
"""
import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 历史坑（已根治）：项目根曾有个 queue.py，会遮蔽 stdlib queue，使 Playwright 传输层报
# "module 'queue' has no attribute 'SimpleQueue'"。2026-09-17 已改名 issue_queue.py。
# 这里仍用 append，保持原状不影响功能。
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

from crawler.core import launch_browser  # noqa: E402


async def main(url, out, wait_ms, clicks, selects, scroll_to):
    pw, b = await launch_browser()
    p = await b.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=2)
    try:
        await p.goto(url, wait_until="domcontentloaded", timeout=20000)
        await p.wait_for_timeout(wait_ms)
        for txt in clicks:  # 依次点击可见文本（如导航"比价矩阵"）
            try:
                await p.get_by_text(txt, exact=False).first.click(timeout=6000)
                print("clicked", txt, flush=True)
                await p.wait_for_timeout(2500)
            except Exception as e:
                print(f"click 失败 {txt}: {type(e).__name__}", flush=True)
        for spec in selects:  # "selector=target" 逐项选择
            sel, _, target = spec.partition("=")
            sel, target = sel.strip(), target.strip()
            # 三级匹配：label 精确 -> value 精确 -> label 包含（最宽松）。
            # 平台部分下拉（如 #mc-m 机型）label 带后缀（"OPPO Pad 5（中端 · 2国）"）
            # 且后缀随数据变化，直接传机型名必然超时，故必须回退到 value。
            done = False
            for how, kwargs in (
                ("label", {"label": target}),
                ("value", {"value": target}),
                ("contains", None),
            ):
                try:
                    if kwargs is not None:
                        await p.select_option(sel, timeout=4000, **kwargs)
                    else:
                        await p.evaluate(
                            """([s, t]) => {
                                const el = document.querySelector(s);
                                if (!el) throw new Error('no ' + s);
                                const opt = [...el.options].find(o => o.textContent.includes(t));
                                if (!opt) throw new Error('no option containing ' + t);
                                el.value = opt.value;
                                el.dispatchEvent(new Event('change', {bubbles: true}));
                            }""",
                            [sel, target],
                        )
                    print(f"selected {sel} = {target} (by {how})", flush=True)
                    done = True
                    break
                except Exception:
                    continue
            if not done:
                print(f"select 失败 {spec}: 三级匹配均未命中", flush=True)
            await p.wait_for_timeout(2500)  # 等联动刷新
        if scroll_to:  # 把含指定文本的单元格滚到视口内，便于截图取到目标行
            try:
                r = await p.evaluate(
                    """(t) => {
                        // 表格单元格常为两行（品类 + 备件名），textContent 是拼接串，
                        // 故用 includes 命中最深层元素，再取其所在 <tr> 作为滚动锚点。
                        const all = [...document.querySelectorAll('td,th,div,span,li,p')];
                        const cands = all.filter(e => e.textContent.trim().includes(t));
                        if (!cands.length) return 'not-found';
                        cands.sort((a, b) => a.querySelectorAll('*').length - b.querySelectorAll('*').length);
                        const inner = cands[0];
                        const el = inner.closest('tr') || inner;
                        let n = el, scrolled = 0;
                        while (n && n !== document.body) {
                            if (n.scrollHeight > n.clientHeight + 4) {
                                const top = el.getBoundingClientRect().top - n.getBoundingClientRect().top + n.scrollTop;
                                n.scrollTop = top - n.clientHeight / 2 + el.clientHeight / 2;
                                scrolled++;
                            }
                            n = n.parentElement;
                        }
                        el.scrollIntoView({block: 'center'});
                        return 'ok(tag=' + el.tagName + ',scrollers=' + scrolled + ')';
                    }""",
                    scroll_to,
                )
                await p.wait_for_timeout(1500)
                print("scrolled to", scroll_to, "->", r, flush=True)
            except Exception as e:
                print(f"scroll 失败 {scroll_to}: {type(e).__name__}", flush=True)
        await p.screenshot(path=str(out))
        print("saved", out, flush=True)
    except Exception as e:
        print("ERR", type(e).__name__, e, flush=True)
    finally:
        # 收尾可能卡住：各步独立短超时，避免拖死进程
        for c in (p.close(), b.close(), pw.stop()):
            try:
                await asyncio.wait_for(c, timeout=8)
            except Exception:
                pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000/")
    ap.add_argument("--out", default=str(ROOT / "output" / "platform_oppo_cn.png"))
    ap.add_argument("--wait", type=int, default=6000)
    ap.add_argument("--click", default="", help="逗号分隔的待点击文本，如 '比价矩阵'")
    ap.add_argument("--select", default="", help="分号分隔的 '选择器=选项文本'，如 '#mc-b=oppo;#mc-m=OPPO Pad 5'")
    ap.add_argument("--scroll-to", default="", help="把含该精确文本的元素滚动到视口中央，如 '屏幕组件'")
    a = ap.parse_args()
    clicks = [s.strip() for s in a.click.split(",") if s.strip()]
    selects = [s.strip() for s in a.select.split(";") if s.strip()]
    asyncio.run(main(a.url, Path(a.out), a.wait, clicks, selects, a.scroll_to.strip()))
