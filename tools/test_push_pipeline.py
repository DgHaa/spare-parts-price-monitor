"""test_push_pipeline.py - 异动推送链路的**真实发送**验证。

== 为什么需要本测试 ==

仓库里没有 `config.push.json`，于是 `load_config()` 恒为 `enabled=False`，
`push_quarterly_alerts()` 永远只走「仅本地预览，不发送」那条 return——
**真正调 send_wechat / send_email 的代码从未被执行过**。只要那段代码有笔误
（改错的配置键、拼错的参数），线上永远不会报错，因为根本跑不到。

本测试把发送路径真跑一遍：临时库（不改 spare_parts.db）+ 临时配置（不落仓库）
+ 本地接收端，验证四件事：

  1. 请求**真的发出**了（企微：loopback HTTP 真发真收）
  2. payload 结构与内容正确（msgtype=markdown / 收件人 / 主题 / 正文）
  3. 阈值过滤正确（只有 |环比| ≥ 阈值的才进推送）
  4. 发送失败时**不抛异常**、打印明确错误（否则会把整批跑批带崩）

企微走真实 HTTP；邮件走**真实 TLS + 真实 SMTP 对话**（自签证书），
仅跳过证书校验（那是标准库职责，不是被测代码）。
若本机缺 openssl，邮件退化为「记录型替身」并在结果里明确标注。

用法：python tools/test_push_pipeline.py      # 全绿 exit 0，否则 exit 1
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from email import message_from_string
from email.policy import default as EMAIL_POLICY
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

import db  # noqa: E402

FAILS: list[str] = []
NOTES: list[str] = []


def check(name, got, want):
    ok = got == want
    print(f"  {'✓' if ok else '✗'} {name}: got={got!r} want={want!r}")
    if not ok:
        FAILS.append(name)


def check_true(name, got):
    ok = bool(got)
    print(f"  {'✓' if ok else '✗'} {name}: {got!r}")
    if not ok:
        FAILS.append(name)


# ---------------------------------------------------------------- 企微接收端
class HttpSink:
    def __init__(self):
        self.hits = []
        sink = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n).decode("utf-8", "replace")
                sink.hits.append({"path": self.path, "ct": self.headers.get("Content-Type"),
                                  "body": raw})
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"errcode":0,"errmsg":"ok"}')

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/cgi-bin/webhook/send?key=test"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()


# ---------------------------------------------------------------- SMTP 接收端
class SmtpSink:
    """最小 SMTP-over-TLS 服务端，够 smtplib.SMTP_SSL + login + send_message 用。"""

    def __init__(self, certfile, keyfile):
        self.sessions = []
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(certfile, keyfile)
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self._stop = False
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while not self._stop:
            try:
                raw, _ = self.sock.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(raw,), daemon=True).start()

    def _handle(self, raw):
        try:
            conn = self.ctx.wrap_socket(raw, server_side=True)
        except Exception:
            return
        f = conn.makefile("rwb", buffering=0)

        def send(s):
            f.write((s + "\r\n").encode())

        sess = {"mail_from": None, "rcpt": [], "data": None}
        send("220 smtpsink ESMTP")
        in_data, buf = False, []
        try:
            while True:
                line = f.readline()
                if not line:
                    break
                line = line.decode("utf-8", "replace").rstrip("\r\n")
                if in_data:
                    if line == ".":
                        in_data = False
                        sess["data"] = "\n".join(buf)
                        buf = []
                        send("250 OK queued")
                    else:
                        buf.append(line)
                    continue
                cmd = line.split(" ", 1)[0].upper()
                if cmd == "EHLO":
                    for s in ("250-smtpsink", "250-AUTH PLAIN LOGIN", "250 SIZE 10485760"):
                        send(s)
                elif cmd == "HELO":
                    send("250 smtpsink")
                elif cmd == "AUTH":
                    rest = line.split(" ", 1)[1] if " " in line else ""
                    if rest.upper().startswith("PLAIN") and len(rest.split()) > 1:
                        send("235 ok")
                    elif rest.upper().startswith("PLAIN"):
                        send("334 ")
                        f.readline()
                        send("235 ok")
                    else:  # LOGIN
                        send("334 VXNlcm5hbWU6")
                        f.readline()
                        send("334 UGFzc3dvcmQ6")
                        f.readline()
                        send("235 ok")
                elif cmd == "MAIL":
                    sess["mail_from"] = line
                    send("250 ok")
                elif cmd == "RCPT":
                    sess["rcpt"].append(line)
                    send("250 ok")
                elif cmd == "DATA":
                    in_data = True
                    send("354 end with <CRLF>.<CRLF>")
                elif cmd == "QUIT":
                    send("221 bye")
                    break
                else:
                    send("250 ok")
        except Exception:
            pass
        finally:
            self.sessions.append(sess)
            try:
                conn.close()
            except Exception:
                pass

    def stop(self):
        self._stop = True
        try:
            self.sock.close()
        except Exception:
            pass


def gen_cert(d: Path):
    """自签证书；openssl 不可用则返回 None。"""
    if not shutil.which("openssl"):
        return None
    cert, key = d / "c.pem", d / "k.pem"
    r = subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", str(key),
         "-out", str(cert), "-days", "1", "-nodes", "-subj", "/CN=127.0.0.1"],
        capture_output=True)
    return (cert, key) if r.returncode == 0 and cert.exists() else None


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="push_test_"))
    db_path = work / "t.db"

    # 1) 临时库：复制真实库结构（含 UNIQUE(part_id,quarter)），不碰真实数据
    src = sqlite3.connect(str(ROOT / "spare_parts.db"))
    dst = sqlite3.connect(str(db_path))
    with dst:
        src.backup(dst)
    src.close()
    db.DB_PATH = db_path

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    # 取「(brand,country,model,part_type) 唯一」的 Q3 行做样本，避免同键覆盖导致预期错位
    rows = con.execute(
        """SELECT ps.id, ps.part_id, ps.cny_price, b.name brand, m.country_code country,
                  m.name model, p.part_type cat
           FROM price_snapshots ps
           JOIN parts p ON p.id=ps.part_id
           JOIN models m ON m.id=p.model_id
           JOIN brands b ON b.id=m.brand_id
           WHERE ps.quarter='2026Q3' AND ps.cny_price IS NOT NULL AND ps.cny_price > 0
             AND p.part_type IS NOT NULL
           GROUP BY b.name, m.country_code, m.name, p.part_type
           HAVING COUNT(*)=1
           LIMIT 8""").fetchall()
    if len(rows) < 8:
        print(f"[FATAL] 样本不足（{len(rows)}），无法构造异动")
        return 2

    # 环比：+25% -30% +5% -3% +40% -20% +12% -18%（阈值 15% → 应命中 5 条）
    pcts = [0.25, -0.30, 0.05, -0.03, 0.40, -0.20, 0.12, -0.18]
    expect = []
    with con:
        for r, pct in zip(rows, pcts):
            prev = float(r["cny_price"]) / (1 + pct)      # 使本季/上季 = 1+pct
            con.execute(
                "INSERT INTO price_snapshots(part_id,quarter,price,currency,cny_price) "
                "VALUES(?,?,?,?,?)",
                (r["part_id"], "2026Q2", prev, "CNY", prev))
            if abs(pct) >= 0.15:
                expect.append({"brand": r["brand"], "model": r["model"], "cat": r["cat"],
                               "pct": round(pct * 100, 1)})
    con.close()
    print(f"临时库：{db_path}")
    print(f"构造 8 条环比，阈值 15% → 预期命中 {len(expect)} 条\n")

    # 2) 接收端
    http_sink = HttpSink()
    certs = gen_cert(work)
    smtp_sink = SmtpSink(*certs) if certs else None
    if smtp_sink:
        print(f"企微接收端 http://127.0.0.1:{http_sink.port}/...  SMTP 接收端 127.0.0.1:{smtp_sink.port} (真实 TLS)")
    else:
        NOTES.append("本机无 openssl → 邮件路径降级为记录型替身，未走真实 TLS")

    # 3) 临时配置（不落仓库！）
    cfg = {
        "enabled": True,
        "alert_threshold": 0.15,
        "wechat": {"webhook_url": http_sink.url},
        "email": {"smtp_host": "127.0.0.1", "smtp_port": smtp_sink.port if smtp_sink else 1,
                  "smtp_user": "u@test.local", "smtp_pass": "p",
                  "to": ["a@test.local", "b@test.local"]},
    }
    cfg_path = work / "config.push.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    import push
    push.CONFIG_PATH = cfg_path

    # 邮件走自签证书：只关掉证书校验（标准库职责），TLS 与 SMTP 对话仍是真实的
    _real_ctx = ssl.create_default_context
    if smtp_sink:
        def _lax_ctx(*a, **k):
            c = _real_ctx(*a, **k)
            c.check_hostname = False
            c.verify_mode = ssl.CERT_NONE
            return c
        ssl.create_default_context = _lax_ctx

    print("\n[1] 真实发送（enabled=true）")
    # 关键：把环境代理指向**死地址**再发。本机 http_proxy 指向爬虫出口代理，
    # urllib 会隐式继承——若 send_wechat 不主动绕开，请求会先去 127.0.0.1:1 而失败。
    # 因此"死代理下仍能送达"正是"确实直连"的判据。
    _saved_proxy = {k: os.environ.get(k) for k in
                    ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")}
    for k in _saved_proxy:
        os.environ[k] = "http://127.0.0.1:1"
    try:
        alerts = push.push_quarterly_alerts()
    finally:
        for k, v in _saved_proxy.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    time.sleep(0.4)  # 等接收端落盘

    # ---- 断言：阈值过滤 ----
    check("异动条数", len(alerts), len(expect))
    check("按 |环比| 降序", [a["pct"] for a in alerts],
          sorted([a["pct"] for a in alerts], key=abs, reverse=True))
    got_keys = sorted((a["brand"], a["model"], a["cat"]) for a in alerts)
    want_keys = sorted((e["brand"], e["model"], e["cat"]) for e in expect)
    check("命中的 品牌/机型/备件类 集合", got_keys, want_keys)

    # ---- 断言：企微真发真收 ----
    print("\n[2] 企微：loopback HTTP 真发真收（且在环境代理为死地址时仍送达 = 确实直连）")
    check("接收端收到请求数", len(http_sink.hits), 1)
    if http_sink.hits:
        hit = http_sink.hits[0]
        check_true("Content-Type 为 application/json", "application/json" in (hit["ct"] or ""))
        payload = json.loads(hit["body"])
        check("msgtype", payload.get("msgtype"), "markdown")
        content = (payload.get("markdown") or {}).get("content", "")
        check_true("正文含标题", "2026Q3" in content and "异动" in content)
        check_true(f"正文含异动条数 {len(expect)}", f"共 **{len(expect)}** 条异动" in content)
        miss = [e for e in expect if e["model"] not in content]
        check("异动机型全部出现在正文", miss, [])

    # ---- 断言：邮件真 TLS 握手 + SMTP 对话 ----
    print("\n[3] 邮件：真实 TLS + SMTP 对话")
    if smtp_sink and smtp_sink.sessions:
        s = smtp_sink.sessions[0]
        check("收件人数", len(s["rcpt"]), 2)
        check_true("MAIL FROM 存在", bool(s["mail_from"]))
        # MIMEText(utf-8) 会把标题与正文编码成 base64，必须按 MIME 解析后再断言
        msg = message_from_string(s["data"] or "", policy=EMAIL_POLICY)
        subj = str(msg["Subject"] or "")
        try:
            body = msg.get_payload(decode=True).decode("utf-8", "replace")
        except Exception:
            body = str(msg.get_payload())
        check_true(f"Subject 含季度与条数（{subj!r}）",
                   "2026Q3" in subj and f"{len(expect)}条" in subj)
        check_true("正文含异动条数", f"{len(expect)} 条异动" in body)
        check("异动机型全部出现在邮件正文",
              [e["model"] for e in expect if e["model"] not in body], [])
        # 阈值参数化回归：原先 format_email 把 ±15% 硬编码，改阈值后邮件会说谎
        check_true("正文阈值与配置一致（±15%）", "±15%" in body)
    else:
        NOTES.append("SMTP sink 未收到会话（或本机无 openssl），邮件路径未见真实证据")
        FAILS.append("邮件真实会话")

    # ---- 断言：失败不抛异常（把 webhook 指向未监听端口）----
    print("\n[4] 发送失败必须优雅降级（不抛异常）")
    http_sink.stop()
    bad_cfg = dict(cfg)
    bad_cfg["wechat"] = {"webhook_url": "http://127.0.0.1:1/webhook"}
    bad_cfg["email"] = {}          # 关掉邮件，只测企微失败
    cfg_path.write_text(json.dumps(bad_cfg, ensure_ascii=False), encoding="utf-8")
    ok = True
    try:
        push.push_quarterly_alerts()
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"    抛异常了：{type(e).__name__}: {e}")
    check("失败时未抛异常", ok, True)

    # ---- 断言：阈值参数化（回归：邮件文案曾把 ±15% 硬编码）----
    print("\n[5] 阈值参数化（邮件文案曾硬编码 ±15%，改 --threshold 后会说谎）")
    demo = [{"brand": "b", "model": "m", "country": "cn", "cat": "屏幕",
             "prev": 100.0, "cur": 130.0, "pct": 30.0}]
    _s, b25 = push.format_email("2026Q3", "2026Q2", demo, 0.25)
    check_true("有异动时正文用配置阈值（±25%）", "±25%" in b25)
    _s0, b30 = push.format_email("2026Q3", "2026Q2", [], 0.30)
    check_true("无异动时正文也用配置阈值（±30%）", "±30%" in b30)
    check_true("不再出现硬编码的 ±15%", "±15%" not in b25 and "±15%" not in b30)

    if smtp_sink:
        smtp_sink.stop()
    ssl.create_default_context = _real_ctx
    shutil.rmtree(work, ignore_errors=True)

    print("\n" + "=" * 72)
    if NOTES:
        print("说明：")
        for n in NOTES:
            print("  · " + n)
    if FAILS:
        print(f"结果：{len(FAILS)} 项未通过 ✗ → {FAILS}")
        return 1
    print("结果：全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
