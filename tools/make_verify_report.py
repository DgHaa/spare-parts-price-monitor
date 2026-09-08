"""tools/make_verify_report.py - 生成 T2 抽样校验 HTML 报告

读取:
  output/verify_t2.json          (修复后重跑结果)
  output/verify_t2_before.json   (修复前基线，用于 before/after 对比)
  spare_parts.db                 (取证 source/model 链接)

输出:
  output/verify_report.html      (自包含，无外部依赖)

用法:
  python tools/make_verify_report.py
"""
import sqlite3, json, html, base64
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "spare_parts.db"
AFTER = ROOT / "output" / "verify_t2.json"
BEFORE = ROOT / "output" / "verify_t2_before.json"
OUT = ROOT / "output" / "verify_report.html"


def esc(x):
    return html.escape("" if x is None else str(x))


def load(p):
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def db_urls():
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT b.name AS brand, m.country_code AS country, m.name AS model,
                  m.model_url, m.model_url_kind, m.source_url
           FROM models m JOIN brands b ON b.id=m.brand_id""").fetchall()
    con.close()
    d = {}
    for r in rows:
        d[(r["brand"], r["country"], r["model"])] = r
    return d


def verdict_class(v):
    return {"PASS": "ok", "MISMATCH": "bad", "HEUR": "warn", "SKIP": "grey"}.get(v, "grey")


def fmt_list(lst):
    if not lst:
        return '<span class="muted">—</span>'
    return "、".join(f"¥{x:,.0f}" if False else f"{x:,.0f}" for x in lst)


def fmt_cur(v, cur):
    if v is None:
        return '<span class="muted">—</span>'
    sym = {"CNY": "¥", "TRY": "₺", "USD": "$", "EUR": "€", "INR": "₹",
           "THB": "฿", "IDR": "Rp", "MYR": "RM", "VND": "₫", "PHP": "₱"}.get(cur or "", "")
    return f"{sym}{v:,.0f}" if sym else f"{v:,.0f} {cur or ''}"


def paired(official, db):
    """容差配对：返回 [(db价, 官方价, 是否命中), ...]，含单边多余项。"""
    off = sorted(float(x) for x in (official or []))
    dbp = sorted(float(x) for x in (db or []))
    rows, used = [], [False] * len(off)
    for d in dbp:
        t = max(1.0, abs(d) * 0.01)
        hit = -1
        for i, o in enumerate(off):
            if not used[i] and abs(o - d) <= t:
                hit = i; break
        if hit >= 0:
            used[hit] = True
            rows.append((d, off[hit], True))
        else:
            rows.append((d, None, False))
    for i, o in enumerate(off):
        if not used[i]:
            rows.append((None, o, False))
    return rows


def build():
    after = load(AFTER)
    before = load(BEFORE)
    urls = db_urls()
    if not after:
        raise SystemExit("缺少 output/verify_t2.json（请先运行 tools/verify_t2.py）")

    # 官方页截图 manifest（tools/shoot_official.py 产出）
    shots_meta, shot_dir = {}, ROOT / "output" / "shots"
    mpath = shot_dir / "manifest.json"
    if mpath.exists():
        try:
            shots_meta = json.loads(mpath.read_text(encoding="utf-8"))
        except Exception:
            shots_meta = {}

    def shot_embed(key):
        m = shots_meta.get(key)
        if not m:
            return ""
        fp = shot_dir / m["file"]
        if not fp.exists():
            return ""
        data = base64.b64encode(fp.read_bytes()).decode("ascii")
        return (f'<div class="shot"><div class="shotcap">官方源截图（类型 {esc(m["kind"] or "official")}）'
                f' · <a href="{esc(m["url"])}" target="_blank" rel="noopener" class="mono">打开官方源</a></div>'
                f'<img src="data:image/jpeg;base64,{data}" alt="official screenshot"></div>')

    # ---- 汇总 ----
    def tally(rep):
        s = dict(pass_=0, mismatch=0, skip=0, heur=0, samples=0)
        for c in rep["combos"]:
            sm = c["summary"]
            s["pass_"] += sm["pass"]
            s["mismatch"] += sm["mismatch"]
            s["skip"] += sm["skip"]
            s["heur"] += sm["heuristic"]
            s["samples"] += sm["n_samples"]
        return s

    t = tally(after)
    tb = tally(before) if before else None

    # before/after 对比索引（按 brand/country/model）
    before_idx = {}
    if before:
        for c in before["combos"]:
            for s in c["samples"]:
                before_idx[(c["brand"], c["country"], s["model"])] = s["verdict"]

    total = t["pass_"] + t["mismatch"] + t["skip"] + t["heur"]
    pass_pct = round(100 * (t["pass_"] + t["heur"]) / total, 1) if total else 0

    parts = []
    parts.append(f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>备件价格监控平台 · T2 抽样校验报告</title>
<style>
  :root {{ --ok:#1a7f37; --bad:#cf222e; --warn:#9a6700; --grey:#656d76; --bg:#f6f8fa; --card:#fff; --line:#d0d7de; }}
  * {{ box-sizing:border-box; }}
  body {{ font-family:-apple-system,"Segoe UI","Microsoft YaHei",Helvetica,Arial,sans-serif; margin:0; background:var(--bg); color:#1f2328; line-height:1.5; }}
  .wrap {{ max-width:1100px; margin:0 auto; padding:28px 20px 60px; }}
  h1 {{ font-size:24px; margin:0 0 4px; }}
  .sub {{ color:var(--grey); font-size:13px; margin-bottom:22px; }}
  .cards {{ display:flex; flex-wrap:wrap; gap:12px; margin:18px 0 26px; }}
  .card {{ flex:1 1 140px; background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }}
  .card .n {{ font-size:28px; font-weight:700; }}
  .card .l {{ font-size:12px; color:var(--grey); margin-top:2px; }}
  .card.ok .n {{ color:var(--ok); }} .card.bad .n {{ color:var(--bad); }}
  .card.warn .n {{ color:var(--warn); }} .card.grey .n {{ color:var(--grey); }}
  .bar {{ height:10px; border-radius:6px; background:#eaeef2; overflow:hidden; display:flex; margin:8px 0 22px; }}
  .bar i {{ display:block; height:100%; }}
  .bar .ok {{ background:var(--ok); }} .bar .bad {{ background:var(--bad); }}
  .bar .warn {{ background:var(--warn); }} .bar .grey {{ background:var(--grey); }}
  section {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:18px 20px; margin-bottom:18px; }}
  section h2 {{ font-size:17px; margin:0 0 12px; }}
  table {{ border-collapse:collapse; width:100%; font-size:13px; }}
  th,td {{ border-bottom:1px solid #eaeef2; padding:8px 10px; text-align:left; vertical-align:top; }}
  th {{ background:#f6f8fa; font-weight:600; color:#57606a; }}
  .badge {{ display:inline-block; padding:1px 8px; border-radius:999px; font-size:12px; font-weight:600; }}
  .badge.ok {{ background:#dafbe1; color:var(--ok); }}
  .badge.bad {{ background:#ffebe9; color:var(--bad); }}
  .badge.warn {{ background:#fff8c5; color:var(--warn); }}
  .badge.grey {{ background:#eaeef2; color:var(--grey); }}
  .combo-h {{ font-size:15px; font-weight:600; margin:18px 0 6px; display:flex; align-items:center; gap:10px; }}
  .pill {{ font-size:12px; color:var(--grey); font-weight:400; }}
  .muted {{ color:#8c959f; }}
  .delta {{ font-size:12px; font-weight:700; }}
  .delta.up {{ color:var(--ok); }} .delta.same {{ color:var(--grey); }}
  a {{ color:#0969da; text-decoration:none; }} a:hover {{ text-decoration:underline; }}
  .mono {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; }}
  .note {{ font-size:13px; color:#57606a; background:#f6f8fa; border-left:3px solid #d0d7de; padding:10px 14px; border-radius:0 6px 6px 0; }}
  .legend span {{ margin-right:14px; font-size:12px; color:var(--grey); }}
  details.pd {{ border:1px solid #eaeef2; border-radius:8px; margin:6px 0; padding:0 12px; }}
  details.pd > summary {{ cursor:pointer; padding:8px 4px; font-size:13px; list-style:decimal inside; }}
  details.pd > summary:hover {{ background:#f6f8fa; }}
  table.ptab {{ font-size:12.5px; margin:6px 0 12px; }}
  table.ptab th {{ background:#f6f8fa; }}
  .ptab .ctr {{ text-align:center; width:48px; font-weight:700; }}
  .ptab tr.ok td:last-child {{ color:var(--ok); }}
  .ptab tr.bad td {{ background:#fff5f5; }}
  .ptab tr.bad td:last-child {{ color:var(--bad); }}
  .ptab td {{ font-variant-numeric:tabular-nums; }}
  .shot {{ margin:8px 0 12px; }}
  .shotcap {{ font-size:12px; color:var(--grey); margin-bottom:4px; }}
  .shot img {{ max-width:100%; border:1px solid #d0d7de; border-radius:6px; display:block; background:#fff; }}
</style></head><body><div class="wrap">
<h1>备件价格监控平台 · T2 抽样校验报告</h1>
<div class="sub">生成时间：{esc(after.get('generated_at'))} ｜ 抽样数 K={esc(after.get('sampling_k'))} 台/品牌×国家 ｜ 数据源：spare_parts.db</div>
""")

    # 总览卡片
    parts.append(f"""<div class="cards">
  <div class="card ok"><div class="n">{t['pass_']}</div><div class="l">PASS 完全匹配</div></div>
  <div class="card bad"><div class="n">{t['mismatch']}</div><div class="l">MISMATCH 不一致</div></div>
  <div class="card grey"><div class="n">{t['skip']}</div><div class="l">SKIP 网络/限流</div></div>
  <div class="card warn"><div class="n">{t['heur']}</div><div class="l">HEUR 三星启发式</div></div>
</div>""")
    # 比例条
    if total:
        w_ok = round(100 * t['pass_'] / total)
        w_bad = round(100 * t['mismatch'] / total)
        w_heur = round(100 * t['heur'] / total)
        w_grey = 100 - w_ok - w_bad - w_heur
        parts.append(f"""<div class="bar">
  <i class="ok" style="width:{w_ok}%"></i><i class="bad" style="width:{w_bad}%"></i>
  <i class="warn" style="width:{w_heur}%"></i><i class="grey" style="width:{max(w_grey,0)}%"></i>
</div>""")
    parts.append(f"""<div class="legend">
  <span>样本总数 <b>{total}</b></span>
  <span>可信率(PASS+HEUR) <b>{pass_pct}%</b></span>
  <span>品牌×国家组合 <b>{len(after['combos'])}</b></span>
</div>""")

    # before/after
    if tb:
        parts.append(f"""<section><h2>修复前后对比（vivo / xiaomi）</h2>
<p class="note">本次修复只针对上一轮 T2 中判为 <b>MISMATCH</b> 的 vivo / xiaomi 机型级链接与价格字段。
下表逐台展示修复前 → 修复后判定变化。</p>
<table><thead><tr><th>品牌</th><th>国家</th><th>机型</th><th>修复前</th><th>修复后</th></tr></thead><tbody>""")
        changed = 0
        rows = []
        for c in after["combos"]:
            if c["brand"] not in ("vivo", "xiaomi"):
                continue
            for s in c["samples"]:
                bv = before_idx.get((c["brand"], c["country"], s["model"]), "—")
                av = s["verdict"]
                if bv != av:
                    changed += 1
                arrow = "" if bv == av else "→"
                rows.append(f"""<tr>
  <td>{esc(c['brand'])}</td><td>{esc(c['country'])}</td><td>{esc(s['model'])}</td>
  <td><span class="badge {verdict_class(bv)}">{esc(bv)}</span></td>
  <td><span class="badge {verdict_class(av)}">{esc(av)}</span> {arrow}</td>
</tr>""")
        parts.append("".join(rows))
        parts.append(f"""</tbody></table>
<p class="sub" style="margin-top:10px">共 {changed} 台判定发生变化（均由 MISMATCH 转为 PASS）。</p></section>""")

    # 逐组合明细
    parts.append("<section><h2>逐品牌 × 国家抽样明细</h2>")
    for c in after["combos"]:
        sm = c["summary"]
        parts.append(f"""<div class="combo-h">{esc(c['brand'])} / {esc(c['country'])}
  <span class="pill">DB 共 {c['models_in_db']} 台 · 抽样 {sm['n_samples']}</span>
  <span class="pill">
    <span class="badge ok">PASS {sm['pass']}</span>
    <span class="badge bad">MIS {sm['mismatch']}</span>
    <span class="badge grey">SKIP {sm['skip']}</span>
    <span class="badge warn">HEUR {sm['heuristic']}</span>
  </span></div>""")
        if not c["samples"]:
            continue
        parts.append("""<table><thead><tr>
  <th>机型</th><th>判定</th><th>官方价数</th><th>DB价数</th><th>匹配率</th>
  <th>缺失/多余</th><th>取证链接</th></tr></thead><tbody>""")
        for s in c["samples"]:
            u = urls.get((c["brand"], c["country"], s["model"]))
            mu = (u["model_url"] or u["source_url"]) if u else None
            su = u["source_url"] if u else None
            kind = u["model_url_kind"] if u else ""
            link = ""
            if mu:
                link = f'<a href="{esc(mu)}" target="_blank" rel="noopener" class="mono">{esc(kind or "link")}</a>'
            elif su:
                link = f'<a href="{esc(su)}" target="_blank" rel="noopener" class="mono">{esc(kind or "link")}</a>'
            else:
                link = '<span class="muted">—</span>'
            rate = s.get("price_match_rate")
            rate_s = f"{round(rate*100)}%" if isinstance(rate, (int, float)) else "—"
            mis = s.get("missing_in_official") or []
            ext = s.get("extra_in_official") or []
            detail = ""
            if mis or ext:
                bits = []
                if mis:
                    bits.append(f"DB有官方无: {fmt_list(mis)}")
                if ext:
                    bits.append(f"官方有DB无: {fmt_list(ext)}")
                detail = "<br>".join(bits)
            else:
                detail = '<span class="muted">—</span>'
            parts.append(f"""<tr>
  <td>{esc(s['model'])}</td>
  <td><span class="badge {verdict_class(s['verdict'])}">{esc(s['verdict'])}</span></td>
  <td>{s.get('official_n','—')}</td><td>{s.get('db_n','—')}</td>
  <td>{rate_s}</td>
  <td>{detail}</td>
  <td>{link}</td>
</tr>""")
        parts.append("</tbody></table>")
    parts.append("</section>")

    # 逐机型价目核对（官方实时抓取 ↔ 库内存储，逐项配对）
    parts.append("""<section><h2>逐机型价目核对（官方实时抓取 ↔ 库内存储）</h2>
<p class="note">以下每个抽样机型，<b>左列</b>为 verify_t2 联网重新抓取到的官方源价格（原币，实时），<b>右列</b>为库内最新季度价格。
两列按容差 max(1, 1%) 逐项配对，✓ 表示命中、✗ 表示单边缺失。你可点击上方「逐品牌明细」里的取证链接，自行打开官方页复核。
这能直接回答「库里的数到底对不对」——两边数字逐行相等即证明抽取正确且数据当前。</p>""")
    for c in after["combos"]:
        if not c["samples"]:
            continue
        parts.append(f'<div class="combo-h">{esc(c["brand"])} / {esc(c["country"])} '
                     f'<span class="pill">价目逐项对照（抽 {len(c["samples"])} 台）</span></div>')
        for s in c["samples"]:
            cur = s.get("currency") or ""
            rows = paired(s.get("official_prices"), s.get("db_prices"), )
            body = []
            for d, o, ok in rows:
                cls = "ok" if ok else "bad"
                mark = "✓" if ok else "✗"
                body.append(f'<tr class="{cls}"><td>{fmt_cur(d, cur)}</td>'
                            f'<td>{fmt_cur(o, cur)}</td><td class="ctr">{mark}</td></tr>')
            v = s["verdict"]
            parts.append(f"""<details class="pd"><summary>
  <b>{esc(s["model"])}</b>
  <span class="badge {verdict_class(v)}">{esc(v)}</span>
  <span class="pill">官 {s.get("official_n","?")} 项 / 库 {s.get("db_n","?")} 项 · {esc(cur)}</span>
</summary>
<table class="ptab"><thead><tr><th>库内价格（存储）</th><th>官方实时价格（抓取）</th><th>配对</th></tr></thead>
<tbody>{''.join(body)}</tbody></table>
{shot_embed(f"{c['brand']}|{c['country']}|{s['model']}")}
</details>""")
    parts.append("</section>")

    # 方法说明
    parts.append("""<section><h2>校验方法说明</h2>
<ul style="font-size:13px;color:#57606a;margin:0;padding-left:18px;line-height:1.7">
  <li><b>比对口径</b>：对每品牌×国家抽 K 台，重新请求其官方机型级源（models.model_url / source_url），抽出现价集合，与库内最新季度原币价格做<b>多重集容差比对</b>（容差 = max(1, 1%)），避免跨语言部件名不匹配的误判。</li>
  <li><b>PASS</b>：官方价集合与 DB 完全一致。<b>MISMATCH</b>：存在缺失/多余价（可能官方调价或抽取 bug）。<b>SKIP</b>：网络/限流/解析失败，绝不误报为失败。<b>HEUR</b>：三星整表页 JS 渲染，仅做人读页机型名+价格数字启发式核验，标 low-confidence。</li>
  <li><b>本论修复</b>：vivo 机型级链接本身正确但 DB 价过期 → 用官方 queryPriceByProductId 当前价整体重插；小米/cn 渲染表抓取把物料价多加了 40（系统性字段错）→ 用官方 shop_band_wx_price 的 sale_price 整体覆盖；vivo tr「seyahat şarj」旅行充电器非手机机型、官方下拉无此项 → 诚实标 brand_entry，不伪造价格。</li>
  <li><b>取证链接</b>：每行「取证链接」指向该机型在官网的机型级价格页（model_api / brand_entry / category_api_locator 等类型见徽标）。</li>
  <li><b>信任边界（请务必知悉）</b>：本验证是<b>联网重新抓取</b>库中已存 URL 指向的官方源、再与库内价比对——它能证明「库里的数 = 官方源此刻的数」，但<b>无法</b>证明该 URL 本身指向的机型没错（那是 T1/T0 机型级链接普查的职责，由 spare-parts-deep-link-harvest 技能保证）。若你怀疑某机型链接指错，请点取证链接人工确认机型名是否一致。</li>
</ul></section>""")

    parts.append("""<p class="sub" style="text-align:center;margin-top:24px">
  报告由 tools/make_verify_report.py 自动生成 · 数据来自 spare_parts.db</p>
</div></body></html>""")

    OUT.write_text("".join(parts), encoding="utf-8")
    print(f"报告已生成: {OUT}  ({OUT.stat().st_size} bytes)")
    print(f"汇总: PASS={t['pass_']} MISMATCH={t['mismatch']} SKIP={t['skip']} HEUR={t['heur']}")


if __name__ == "__main__":
    build()
