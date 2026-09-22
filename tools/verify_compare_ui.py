#!/usr/bin/env python3
"""比价矩阵前端回归校验：行标签必须显示规格、单元格币种标签必须是 CNY。

== 为什么需要这个工具 ==
  这两个缺陷都是「数据对、界面错」，后端测试完全测不出来，只能靠渲染后断言：

  1) `web/app.js` 的行标签只输出 cat + part，不含 spec。而比价分组键是
     (品类, 件名, 规格)，主板会按存储规格拆成 5 行（8G+128G / 8G+256G / 12G+256G /
     12G+512G + 1 行无规格）——规格不上屏，五行主板全渲染成「主板 / 主板」，
     看起来像重复行。修复后应出现 .spectag 徽标。

  2) 单元格主数字取的是 `cny`（折算值），小字却输出原币币种码，导致
     「数字是 CNY、标签是外币」：日本格显示 2,947 JPY，实为 2,947 CNY（原币 68,200 JPY）。
     修复后小字必须以 CNY 开头，非人民币再用 ≈ 跟随原币。

== 前置 ==
  后端需已启动：python api/server.py   （默认 127.0.0.1:8000）

== 环境提醒 ==
  本脚本每次会拉起一个无头 Chromium。若前几次运行被中途 Ctrl-C / 超时杀掉，
  会残留 chrome-headless-shell 进程（可在任务管理器里看到一堆），
  后续启动会明显变慢甚至像"卡住"。跑之前先确认没有残留；本机若无法结束进程，
  重启终端/机器后再跑。脚本已逐步打印进度，便于区分「慢」与「真挂」。

用法：
  python tools/verify_compare_ui.py                       # 校验（失败退出码 1）
  python tools/verify_compare_ui.py --shot out.png        # 同时留一张截图
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "references" / "calibration"))
from _paths import find_chromium  # noqa: E402

URL = "http://127.0.0.1:8000/#matrix"
BRAND, MODEL = "oppo", "OPPO Pad 2"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shot", default=None, help="截图输出路径（可选）")
    ap.add_argument("--brand", default=BRAND)
    ap.add_argument("--model", default=MODEL)
    a = ap.parse_args()

    from playwright.sync_api import sync_playwright

    fails = []
    with sync_playwright() as p:
        print("[1/6] 启动无头 Chromium（若此处长时间无输出，多半是残留进程争抢资源）…",
              flush=True)
        b = p.chromium.launch(headless=True, executable_path=find_chromium(),
                              args=["--no-sandbox"])
        pg = b.new_page(viewport={"width": 1680, "height": 1200})
        pg.on("pageerror", lambda e: fails.append(f"页面 JS 异常: {e}"))
        print("[2/6] 打开比价矩阵…", flush=True)
        pg.goto(URL, wait_until="load")
        pg.wait_for_selector("#mc-b", timeout=20000)
        print("[3/6] 选品牌 / 机型…", flush=True)
        pg.select_option("#mc-b", a.brand)
        pg.wait_for_timeout(1200)
        # 机型是「搜索 + 下拉组合框」，不是 select。必须真实鼠标点击：
        # evaluate(el.click()) 不触发该组件绑定的监听器，下拉不收、机型不选中。
        pg.fill("#mc-search", a.model)
        pg.wait_for_timeout(1500)
        pg.click(f'#mc-list .combo-item[data-m="{a.model}"]')
        pg.wait_for_timeout(3500)
        print("[4/6] 等表格渲染…", flush=True)
        try:
            pg.wait_for_selector(".heat", timeout=20000)
        except Exception:
            fails.append(f"未渲染出比价表（{a.brand}/{a.model} 是否库内无数据？）")

        print("[5/6] 读取行标签与单元格…", flush=True)
        rows = pg.evaluate("""() => [...document.querySelectorAll('.heat tbody tr')].map(tr => {
            const lab = tr.querySelector('.rowlabel');
            if (!lab) return null;
            return {label: (lab.innerText||'').replace(/\\n/g,' | '),
                    spec: (lab.querySelector('.spectag')||{}).innerText || null,
                    firstCell: (tr.querySelector('td:nth-child(2)')||{}).innerText || ''};
        }).filter(Boolean)""")
        if a.shot:
            pg.screenshot(path=a.shot)
        print("[6/6] 关闭浏览器…", flush=True)
        b.close()

    if not rows:
        fails.append("未取到任何比价行")

    spec_rows = [r for r in rows if r["spec"]]
    dup_labels = [r["label"] for r in rows if r["label"] == "主板"]
    if len(spec_rows) == 0:
        fails.append("回归：没有任何行渲染出规格徽标 .spectag（规格未上屏）")
    if len(dup_labels) > 1:
        fails.append(f"回归：出现 {len(dup_labels)} 行同名无规格标签 {dup_labels}")
    for r in rows:
        c = r["firstCell"]
        if c and c != "—" and "CNY" not in c:
            fails.append(f"回归：单元格未标注 CNY -> {r['label']} / {c!r}")
            break

    print(f"渲染行数 = {len(rows)}；带规格徽标 = {len(spec_rows)}")
    for r in rows[:12]:
        print(f"  {r['label']:<24} spec={r['spec']}  首格={r['firstCell'].replace(chr(10),' / ')}")
    if fails:
        print("\n[FAIL]")
        for f in fails:
            print("  -", f)
        return 1
    print("\n[PASS] 行标签含规格、单元格币种标注为 CNY")
    return 0


if __name__ == "__main__":
    sys.exit(main())
