"""push.py - 季度异动自动推送（企业微信机器人 + 邮件）。

数据：SQLite 中同一 (brand, country, model, part_type) 本季 vs 上季的 CNY 均价环比。
阈值：默认 ±15%（config.alert_threshold 或 --threshold）。

配置（spare-parts-monitor/config.push.json；缺失或 enabled=false 时仅本地预览，不发送）：
{
  "enabled": false,
  "alert_threshold": 0.15,
  "wechat": {"webhook_url": "", "proxy_mode": "auto"},
  "email":  {"smtp_host":"","smtp_port":465,"smtp_user":"","smtp_pass":"","to":[""]}
}
  wechat.proxy_mode：auto（默认，先直连、失败回退环境代理）/ direct / proxy。
  为何需要它：本机 http_proxy 是给爬虫访海外站用的，而 urllib 会隐式继承，
  会把企微告警绕道爬虫出口（2026-09-23 实测，详见 send_wechat）。
也可用环境变量覆盖：
  SPM_WECHAT_WEBHOOK / SPM_SMTP_HOST / SPM_SMTP_PORT / SPM_SMTP_USER / SPM_SMTP_PASS / SPM_EMAIL_TO

用法：
  python push.py                      # 计算本季异动并推送（受 config.enabled 控制）
  python push.py --quarter 2026Q3 --threshold 0.1
本模块被 run_quarterly.py 在抓取结束后自动调用。
"""
import argparse
import json
import os
import smtplib
import ssl
import sys
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener, urlopen  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import get_conn, this_quarter  # noqa: E402

CONFIG_PATH = Path(__file__).resolve().parent / "config.push.json"


def load_config():
    cfg = {"enabled": False, "alert_threshold": 0.15, "wechat": {}, "email": {}}
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception as e:
            print("[push] 配置读取失败:", e, flush=True)
    # 环境变量覆盖（便于 CI / 计划任务注入凭据，不落盘）
    if os.environ.get("SPM_WECHAT_WEBHOOK"):
        cfg.setdefault("wechat", {})["webhook_url"] = os.environ["SPM_WECHAT_WEBHOOK"]
    if os.environ.get("SPM_SMTP_HOST"):
        em = cfg.setdefault("email", {})
        em["smtp_host"] = os.environ["SPM_SMTP_HOST"]
        em["smtp_port"] = int(os.environ.get("SPM_SMTP_PORT", 465))
        em["smtp_user"] = os.environ.get("SPM_SMTP_USER", "")
        em["smtp_pass"] = os.environ.get("SPM_SMTP_PASS", "")
        to = os.environ.get("SPM_EMAIL_TO", "")
        em["to"] = [t.strip() for t in to.split(",") if t.strip()]
    return cfg


def prev_quarter(quarter):
    """2026Q3 -> 2026Q2（简单季度回退）。"""
    try:
        y = int(quarter[:4])
        q = int(quarter[5])
    except (ValueError, IndexError):
        return ""
    q -= 1
    if q == 0:
        q = 4
        y -= 1
    return f"{y}Q{q}"


def compute_alerts(quarter, threshold=0.15):
    """计算本季相对上季的备件价环比异动（同 brand/country/model/part_type）。"""
    prev = prev_quarter(quarter)
    if not prev:
        return [], prev
    c = get_conn()
    rows = c.execute(
        """SELECT b.name brand, m.country_code country, m.name model, p.part_type cat,
                  ps.cny_price cny, ps.quarter q
           FROM price_snapshots ps
           JOIN parts p ON p.id=ps.part_id
           JOIN models m ON m.id=p.model_id
           JOIN brands b ON b.id=m.brand_id
           WHERE ps.quarter IN (?, ?)""",
        (quarter, prev)).fetchall()
    c.close()
    cur, last = {}, {}
    for r in rows:
        key = (r["brand"], r["country"], r["model"], r["cat"])
        (cur if r["q"] == quarter else last)[key] = r["cny"]
    alerts = []
    for key, cny in cur.items():
        if cny is None:
            continue
        pc = last.get(key)
        if pc is None or pc == 0:
            continue  # 上季无数据：不算异动
        pct = (cny - pc) / pc
        if abs(pct) >= threshold:
            alerts.append({
                "brand": key[0], "country": key[1], "model": key[2], "cat": key[3],
                "prev": round(pc, 2), "cur": round(cny, 2), "pct": round(pct * 100, 1),
            })
    alerts.sort(key=lambda x: abs(x["pct"]), reverse=True)
    return alerts, prev


def format_wechat(quarter, prev, alerts, threshold):
    if not alerts:
        return f"## 备件价中台 · {quarter} 季度异动\n本季无显著异动（阈值 ±{int(threshold*100)}%）。"
    lines = [f"## 备件价中台 · {quarter} 季度异动（对比 {prev}）",
             f"> 共 **{len(alerts)}** 条异动（阈值 ±{int(threshold*100)}%）\n"]
    for a in alerts[:30]:
        arrow = "🔺" if a["pct"] > 0 else "🔻"
        lines.append(f"- {arrow} **{a['brand']} {a['model']}** [{a['country']}] {a['cat']}："
                     f"{a['prev']}→{a['cur']} CNY（{a['pct']:+}%）")
    if len(alerts) > 30:
        lines.append(f"- … 其余 {len(alerts)-30} 条见中台")
    return "\n".join(lines)


def format_email(quarter, prev, alerts, threshold=0.15):
    """邮件文案。阈值必须**参数化**——原实现把 ±15% 硬编码进标题与正文，
    改 --threshold 后邮件会继续说 15%（微信侧是对的，两边口径不一致会误导读者）。"""
    tp = int(threshold * 100)
    if not alerts:
        return (f"备件价中台 {quarter} 季度异动报告",
                f"{quarter} 季度无显著备件价异动（阈值 ±{tp}%）。")
    body = [f"备件价中台 {quarter} 季度异动报告（对比 {prev}）",
            f"共 {len(alerts)} 条异动（阈值 ±{tp}%）：\n"]
    for a in alerts:
        body.append(f"{'↑' if a['pct'] > 0 else '↓'} {a['brand']} {a['model']} [{a['country']}] {a['cat']}: "
                    f"{a['prev']}→{a['cur']} CNY ({a['pct']:+}%)")
    return (f"备件价中台 {quarter} 异动（{len(alerts)}条）", "\n".join(body))


def send_wechat(webhook, content, proxy_mode="auto"):
    """POST 到企微 webhook。

    ⚠️ 代理陷阱（2026-09-23 实测发现）：本机为爬虫访问海外品牌站设置了
    http_proxy / https_proxy（见 crawler.core.get_proxy），而 urllib 的 urlopen
    会**隐式**读取这些环境变量——于是企微告警被悄悄绕道爬虫出口节点。
    证据：把 webhook 指向一个未监听端口，回来的是代理的
    `HTTP Error 502 Bad Gateway` 而不是"连接被拒"，说明请求确实经过了代理。

    告警链路不该依赖爬虫代理的可用性（代理重启/限流会静默吞掉告警），故默认
    **直连**；直连失败时自动回退走代理并打印说明，兼容"必须走代理才能出网"的环境。

    proxy_mode: "auto"（默认：先直连，失败回退代理）| "direct" | "proxy"
    """
    payload = json.dumps({"msgtype": "markdown",
                          "markdown": {"content": content}}).encode("utf-8")
    headers = {"Content-Type": "application/json"}

    def _post(use_proxy):
        opener = build_opener() if use_proxy else build_opener(ProxyHandler({}))
        req = Request(webhook, data=payload, headers=headers)
        with opener.open(req, timeout=10) as r:
            return r.read().decode("utf-8")

    if proxy_mode == "direct":
        return _post(False)
    if proxy_mode == "proxy":
        return _post(True)
    try:
        return _post(False)
    except Exception as e:  # noqa: BLE001
        print(f"[push] 直连 webhook 失败（{e}），回退走环境代理重试", flush=True)
        return _post(True)


def send_email(cfg, subject, body):
    em = cfg["email"]
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = em.get("smtp_user", "")
    msg["To"] = ", ".join(em.get("to", []))
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(em["smtp_host"], int(em.get("smtp_port", 465)), context=ctx) as s:
        s.login(em["smtp_user"], em["smtp_pass"])
        s.send_message(msg)


def push_quarterly_alerts(quarter=None, threshold=None):
    cfg = load_config()
    threshold = threshold if threshold is not None else cfg.get("alert_threshold", 0.15)
    quarter = quarter or this_quarter()
    alerts, prev = compute_alerts(quarter, threshold)
    print(f"[push] {quarter} 计算到 {len(alerts)} 条异动（对比 {prev}，阈值 ±{int(threshold*100)}%）", flush=True)
    if not cfg.get("enabled", False):
        print("[push] enabled=false，仅本地预览，不发送。预览内容：", flush=True)
        print(format_wechat(quarter, prev, alerts, threshold)[:800], flush=True)
        return alerts
    if alerts:
        wc = cfg.get("wechat", {}).get("webhook_url")
        if wc:
            try:
                send_wechat(wc, format_wechat(quarter, prev, alerts, threshold),
                            proxy_mode=cfg.get("wechat", {}).get("proxy_mode", "auto"))
                print("[push] 企微推送成功", flush=True)
            except Exception as ex:
                print(f"[push] 企微推送失败: {ex}", flush=True)
        em = cfg.get("email", {})
        if em.get("smtp_host") and em.get("to"):
            try:
                subj, body = format_email(quarter, prev, alerts, threshold)
                send_email(cfg, subj, body)
                print("[push] 邮件推送成功", flush=True)
            except Exception as ex:
                print(f"[push] 邮件推送失败: {ex}", flush=True)
    else:
        print("[push] 无显著异动，跳过发送", flush=True)
    return alerts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quarter")
    ap.add_argument("--threshold", type=float)
    args = ap.parse_args()
    push_quarterly_alerts(args.quarter, args.threshold)


if __name__ == "__main__":
    main()
