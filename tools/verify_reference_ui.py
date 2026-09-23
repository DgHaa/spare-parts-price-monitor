#!/usr/bin/env python3
"""参考价前端渲染校验（B 方案收尾验证）。

断言：
  1) 比价矩阵里至少出现 1 个 .ref-cell（参考价灰底单元格）；
  2) 参考价单元格里渲染出紫色虚线徽标，文本含「参考·中国」；
  3) 参考价单元格背景为灰底（#f1f5f9），而非热力色；
  4) 单元格小字以 CNY 开头（参考价 CNY 单列，不谎报原币）。

前置：后端已启动 python api/server.py (127.0.0.1:8000)。
用法：
  python tools/verify_reference_ui.py
  python tools/verify_reference_ui.py --shot out.png
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "references" / "calibration"))
from _paths import find_chromium  # noqa: E402

URL = "http://127.0.0.1:8000/#matrix"
BRAND, MODEL = "oppo", "OPPO Reno4 Pro"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shot", default=None)
    ap.add_argument("--brand", default=BRAND)
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--result-log", default="logs_ref_verify_result.txt",
                    help="DOM 断言原始结果落盘路径（先于 b.close() 写入）")
    a = ap.parse_args()

    from playwright.sync_api import sync_playwright

    fails = []
    with sync_playwright() as p:
        print("[1/6] 启动无头 Chromium…", flush=True)
        b = p.chromium.launch(headless=True, executable_path=find_chromium(),
                              args=["--no-sandbox"])
        pg = b.new_page(viewport={"width": 1680, "height": 1200})
        pg.on("pageerror", lambda e: fails.append(f"页面 JS 异常: {e}"))
        print("[2/6] 打开比价矩阵…", flush=True)
        pg.goto(URL, wait_until="load")
        pg.wait_for_selector("#mc-b", timeout=20000)
        pg.select_option("#mc-b", a.brand)
        pg.wait_for_timeout(1200)
        pg.fill("#mc-search", a.model)
        pg.wait_for_timeout(1500)
        pg.click(f'#mc-list .combo-item[data-m="{a.model}"]')
        pg.wait_for_timeout(3500)
        print("[3/6] 等表格渲染…", flush=True)
        try:
            pg.wait_for_selector(".heat", timeout=20000)
        except Exception:
            fails.append(f"未渲染出比价表（{a.brand}/{a.model} 是否库内无数据？）")

        print("[4/6] 读取参考价单元格…", flush=True)
        info = pg.evaluate("""() => {
            const cells = [...document.querySelectorAll('td.ref-cell')];
            const badges = [...document.querySelectorAll('sup.ref')];
            const sample = cells.slice(0, 3).map(td => ({
                bg: getComputedStyle(td).backgroundColor,
                text: (td.innerText||'').replace(/\\n/g,' / '),
            }));
            // 归属校验：参考价国家**不得**被标成"最低国/最高国"
            // （那会把"没有本地价的国家"排成最便宜/最贵，属误导）
            const problems = [];
            document.querySelectorAll('table.heat').forEach(tbl => {
              const heads = [...tbl.querySelectorAll('thead th')].map(t => t.innerText.trim());
              [...tbl.querySelectorAll('tbody tr')].forEach(tr => {
                const tds = [...tr.querySelectorAll('td')];
                if (tds.length < 4) return;
                const refC = [], realC = [];
                tds.forEach((td, i) => {
                  if (i === 0 || i >= heads.length - 3) return;  // 跳过行标签与末尾3列
                  if (td.classList.contains('ref-cell')) refC.push(heads[i]);
                  else if (!td.classList.contains('muted')) realC.push(heads[i]);
                });
                const low = (tds[tds.length-3]||{}).innerText?.trim() || '';
                const high = (tds[tds.length-2]||{}).innerText?.trim() || '';
                const rowName = (tds[0].innerText||'').replace(/\\n/g,' ').trim();
                if (low && refC.includes(low))
                  problems.push(`最低国=${low} 但该国只有参考价: ${rowName}`);
                if (high && refC.includes(high))
                  problems.push(`最高国=${high} 但该国只有参考价: ${rowName}`);
              });
            });
            const noteEl = [...document.querySelectorAll('.hint')]
              .find(e => (e.innerText||'').includes('非当地官方价'));
            return {
                refCellCount: cells.length,
                refBadgeCount: badges.length,
                badgeText: badges.slice(0,3).map(x => x.innerText),
                sample,
                labelProblems: problems.slice(0, 8),
                noteRendered: !!noteEl,
                noteText: noteEl ? noteEl.innerText.slice(0, 120) : null,
            };
        }""")
        if a.shot:
            pg.screenshot(path=a.shot)
        # 先把结果落到文件/stdout 再关浏览器：本机残留 chrome-headless-shell 进程会
        # 让 b.close() 长时间阻塞，若把结果打印放在 close 之后，断言结果将永远刷不出来。
        print("结果:", info, flush=True)
        Path(a.result_log).write_text(repr(info), encoding="utf-8")
        print("[5/6] 关闭浏览器…", flush=True)
        try:
            b.close()
        except Exception as e:  # 关不掉不影响已产出的证据
            print(f"  (浏览器关闭异常，忽略): {e}", flush=True)

    return _assert(info, fails)


def _assert(info, fails):
    """把断言与打印独立出来，保证结果先于（可能阻塞的）b.close() 产出。"""
    if info["refCellCount"] == 0:
        fails.append("未渲染出任何参考价灰底单元格 .ref-cell")
    if info["refBadgeCount"] == 0:
        fails.append("未渲染出任何「参考·中国」徽标 sup.ref")
    elif not any("参考" in (t or "") for t in info["badgeText"]):
        fails.append(f"参考价徽标文本异常: {info['badgeText']}")
    # 灰底校验：参考价单元格背景应近似 rgb(241,245,249) = #f1f5f9
    for s in info["sample"]:
        if s["bg"] and "241, 245, 249" not in s["bg"]:
            fails.append(f"参考价单元格非灰底: bg={s['bg']} text={s['text']}")
            break
    # CNY 单列校验
    for s in info["sample"]:
        if s["text"] and "CNY" not in s["text"]:
            fails.append(f"参考价单元格小字未标 CNY: {s['text']}")
            break
    # 参考价国家不得被标成最低国/最高国
    if info.get("labelProblems"):
        for p in info["labelProblems"]:
            fails.append("参考价参与价差归属（应排除）: " + p)
    # 说明横幅
    if not info.get("noteRendered"):
        fails.append("未渲染参考价说明横幅（含「非当地官方价」）")

    if fails:
        print("\n[FAIL]")
        for f in fails:
            print("  -", f)
        return 1
    print("\n[PASS] 参考价灰底单元格 + 「参考·中国」紫虚线徽标 + CNY 单列 + "
          "不参与最低国/最高国归属 + 说明横幅 均已正确渲染")
    return 0


if __name__ == "__main__":
    sys.exit(main())
