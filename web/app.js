/* app.js - 备件价格监控中台前端（零构建 SPA，原生 JS）。 */
(function () {
  // API 地址：优先 ?api= 覆盖；
  // 页面托管在本机后端(localhost / 127.0.0.1，任意端口)时用相对地址；
  // 其他情况(内置预览面板、file:// 等)回退到本机后端绝对地址(端口取 ?port= 或默认 8000)。
  // 后端已开 CORS *，可跨源。也可用 ?api= 强制指定后端地址。
  const _params = new URLSearchParams(location.search);
  const _cfgPort = _params.get("port") || "8000";
  // API 地址：优先 ?api= 覆盖；页面经 http/https 托管（本机或 cpolar 等穿透域名）时一律用
  // 相对地址（同源：前端与后端同端口，任何域名/公网穿透都正确）；仅在 file:// 直接打开 HTML
  // 预览时回退到本机后端绝对地址（端口取 ?port= 或默认 8000）。
  const _isFile = location.protocol === "file:";
  const API = (_params.get("api") && _params.get("api").trim())
    || (_isFile ? "http://localhost:" + _cfgPort : "");
  const $ = (s, el = document) => el.querySelector(s);
  // 产品品类（models.category）。2026-09-18 起库里收全品类，但跨品类比价无意义，
  // 故比价矩阵默认只看手机、并允许切换品类。
  const MODEL_CATS = [["phone", "手机"], ["tablet", "平板"], ["watch", "手表"],
                      ["earbuds", "耳机"], ["wearable", "手环/戒指"], ["other", "其他"]];
  const view = $("#view");
  const state = { overview: null, brands: [], quarters: [], health: [], anomalies: [], models: [], fx: null, mcData: null };

  async function api(path) {
    let r;
    try { r = await fetch(API + path); }
    catch (e) { setConn(false, "网络不可达：" + e.message); throw e; }
    if (!r.ok) { setConn(false, path + " -> HTTP " + r.status); throw new Error(path + " -> " + r.status); }
    setConn(true);
    return r.json();
  }
  function toast(msg) {
    const t = $("#toast"); t.textContent = msg; t.classList.add("show");
    setTimeout(() => t.classList.remove("show"), 2200);
  }
  function setConn(ok, msg) {
    const b = $("#conn-banner");
    if (!b) return;
    if (ok) { b.hidden = true; return; }
    b.hidden = false;
    const dot = $("#conn-dot"); if (dot) dot.className = "conn-dot bad";
    const m = b.querySelector(".conn-msg"); if (m) m.textContent = "后端未连接：" + (msg || "请在本机浏览器打开 http://localhost:" + _cfgPort);
  }
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
  // 用于 onclick 属性内（单引号包裹）的字符串转义
  function escAttr(s) { return String(s == null ? "" : s).replace(/'/g, "&#39;").replace(/"/g, "&quot;"); }
  function fmt(n, d = 0) { return n == null ? "—" : Number(n).toLocaleString("zh-CN", { maximumFractionDigits: d }); }

  // 国家码 → 完整中文名（前台统一展示中文国名，不用缩写）。优先由 /api/countries 运行时补全，
  // 这里内置一份兜底，保证即使接口未返回也能正确显示。
  const COUNTRY_NAMES = {
    ae: "阿联酋", cn: "中国", de: "德国", jp: "日本",
    mx: "墨西哥", my: "马来西亚", tr: "土耳其",
  };
  // 将国家码显示为完整中文名；找不到时回退原码（如未来新增国家尚未录入映射）。
  function cn(cc) { return COUNTRY_NAMES[cc] || cc; }
  function money(n) { return n == null ? "—" : "¥" + fmt(n, 0); }

  /* ============ 机型级取证链接（一机一链） ============
     只渲染库内已落库的 model_url，前端不再拼接/猜测任何 URL。
     kind 与 verified 决定徽标与提示文案，如实告知这条链接能定位到什么粒度：
       model_api            仅返回本机型价格的官方接口
       category_api_locator 官网只有品类级接口（Apple），本机型价格在响应内，locator 给出 JSON 路径
       model_text_fragment  官网整表页（三星），链接用文本片段自动滚动并高亮到本机型行
       brand_entry / 空     仅品牌入口页 —— 明确标注"非本机型精确链接" */
  const LINK_KIND = {
    model_api: { icon: "🔗机型", cls: "lk-ok", label: "本机型专属官方接口（已实测校验命中本机型）" },
    category_api_locator: { icon: "🔗机型", cls: "lk-ok", label: "官方品类级接口 + 本机型精确定位（Apple 仅提供品类级接口，本机型价在响应内）" },
    model_text_fragment: { icon: "🔗行", cls: "lk-ok", label: "官网整表页 + 自动定位到本机型行" },
    model_page: { icon: "🔗页", cls: "lk-ok", label: "本机型官网页面" },
    brand_entry: { icon: "🔗牌", cls: "lk-weak", label: "仅品牌入口页（非本机型精确链接）" },
  };
  function modelLinkHTML(pc, opt) {
    opt = opt || {};
    const stop = opt.stopProp ? ' onclick="event.stopPropagation()"' : "";
    const url = pc.model_url || pc.source_url;
    if (!url) return "";
    const kind = pc.model_url_kind || pc.source_url_kind || "brand_entry";
    const meta = LINK_KIND[kind] || LINK_KIND.brand_entry;
    const ok = pc.model_url_verified === 1;
    let loc = null;
    try { loc = pc.model_url_locator ? JSON.parse(pc.model_url_locator) : null; } catch (e) { loc = null; }
    let tip = meta.label;
    if (pc.model_name) tip += `｜机型：${pc.model_name}`;
    if (loc) {
      if (loc.json_path) tip += `｜定位：${loc.json_path}`;
      else if (loc.param) tip += `｜定位：${loc.param}=${loc.value}`;
      else if (loc.text_fragment) tip += `｜定位文本：${loc.text_fragment}`;
      if (loc.picker_hint) tip += `｜${loc.picker_hint}`;
      if (loc.note) tip += `｜${loc.note}`;
    }
    tip += ok ? "｜已实测校验：请求该链接确认命中本机型"
              : "｜⚠ 未通过实测校验，不作为本机型精确取证";
    let html = `<a class="srclink ${ok ? meta.cls : "lk-bad"}" href="${esc(url)}" target="_blank"`
      + ` rel="noopener" title="${esc(tip)}"${stop}>${meta.icon}${ok ? "" : "⚠"}</a>`;
    // 人可读的官网页面（与数据链接不同时额外给一个）
    if (opt.showModel && pc.model_page_url && pc.model_page_url !== url)
      html += ` <a class="srclink" href="${esc(pc.model_page_url)}" target="_blank" rel="noopener"`
        + ` title="${esc("官网人可读页面（在页面上按上述定位选择本机型即可核对）")}"${stop}>📄</a>`;
    return html;
  }
  function brandCaveat(b) { const x = (state.brands || []).find(r => r.name === b); return (x && x.price_caveat) || ""; }
  function statusBadge(st) {
    if (st === "success") return '<span class="badge green">正常</span>';
    if (st === "failed") return '<span class="badge red">抓取失败</span>';
    if (st === "partial") return '<span class="badge amber">部分失败</span>';
    if (st === "skipped") return '<span class="badge gray">跳过</span>';
    return '<span class="badge gray">' + esc(st || "?") + "</span>";
  }
  // 迷你折线（内联 SVG，无依赖）
  function sparklineSVG(values, w = 96, h = 26) {
    const vv = (values || []).filter(v => v != null);
    if (vv.length < 2) return '<span class="muted">—</span>';
    const max = Math.max(...vv), min = Math.min(...vv), span = (max - min) || 1;
    const n = vv.length;
    const pts = vv.map((v, i) => {
      const x = Math.round((i / (n - 1)) * (w - 4)) + 2;
      const y = Math.round(2 + (h - 4) * (1 - (v - min) / span));
      return x + "," + y;
    }).join(" ");
    const up = vv[vv.length - 1] >= vv[0];
    const col = up ? "#dc2626" : "#16a34a"; // 中国习惯：涨红跌绿
    return '<svg class="spark" width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h + '">' +
      '<polyline points="' + pts + '" fill="none" stroke="' + col + '" stroke-width="1.6" ' +
      'stroke-linejoin="round" stroke-linecap="round"/></svg>';
  }

  // 备件名 -> 规范品类（用于跨品牌比价 / 走势分组）。
  // 与 db.normalize_category 的人工映射表保持一致，确保前端分组口径与库内 part_type 对齐
  // （"其他"占比由 ~30% 降到 <2%）。覆盖中/英/日/德/土多语与本地化损伤类标签。
  function canon(part) {
    const p = (part || "").toLowerCase();
    const MAP = [
      [/屏幕|画面|display|ekran|screen|ディスプレイ|画面の損傷|画面および背面/, "屏幕"],
      [/后盖|背板|后壳|back\s*cover|back\s*glass|arka\s*kapak|背面ガラス|rückglas|rückglasschaden/, "后盖"],
      [/电池|蓄电|battery|pil|バッテリー|バッテリー修理|batterieservice/, "电池"],
      [/主板|逻辑板|mainboard|anakart|メイン基板|ロジック|board/, "主板"],
      [/摄像|相机|镜头|camera|lens|kamera|カメラ|背面カメラ|rückkamera|schaden an der rückkamera|后摄/, "摄像头"],
      [/充电口|接口|尾插|usb|type-c|充電コネクタ/, "充电口"],
      [/扬声器|speaker|hoparlör|スピーカー/, "扬声器"],
      [/受话器|听筒|earpiece|受話器/, "听筒"],
      [/马达|振动|vibration|バイブ|titreşim|昇降|升降/, "振动马达"],
      [/中框|框架|frame|フレーム|çerçeve|滑动/, "中框"],
      [/指纹|fingerprint|parmak/, "指纹"],
      [/侧键|电源键|音量键|音量|button|ボタン|tuş/, "侧键"],
      [/麦克风|mic|microphone|マイク|mikrofon/, "麦克风"],
      [/数据线|cable|ケーブル|kablo/, "数据线"],
      [/耳机|earphone|イヤホン|kulaklık|左耳|右耳/, "耳机"],
      [/适配器|adapter|充電器|アダプター|şarj cihaz|充电头|闪充/, "电源适配器"],
      [/电源线|power cord|電源コード/, "电源线"],
      [/遥控器|remote|リモコン/, "遥控器"],
      [/壁挂|wall mount/, "壁挂"],
      [/底座|スタンド|stand/, "底座"],
      [/表盘|文字盤|watch face/, "表盘"],
      [/表带|腕带|バンド|band|kayış/, "表带"],
      [/手写笔|笔尖|stylus|スタイラス/, "手写笔"],
      [/护眼膜|贴膜|film|フィルム/, "贴膜"],
      [/键盘|keyboard|キーボード/, "键盘"],
      [/转接线|av输入/, "转接线"],
      [/眼镜|メガネ|gözlük/, "眼镜"],
    ];
    for (const [rx, lab] of MAP) if (rx.test(p)) return lab;
    if (/屏幕|display|screen|屏/.test(p)) return "屏幕";
    if (/电池|battery/.test(p)) return "电池";
    if (/主板|board/.test(p)) return "主板";
    if (/后盖|back/.test(p)) return "后盖";
    if (/摄像|camera|镜头/.test(p)) return "摄像头";
    if (/充电|usb|尾插/.test(p)) return "充电口";
    return part;
  }

  /* ---------- 我的关注（localStorage 持久化） ---------- */
  const WKEY = "spm_watch";
  function getWatch() { try { return JSON.parse(localStorage.getItem(WKEY) || "[]"); } catch (e) { return []; } }
  function toggleWatch(key) {
    const a = getWatch();
    const i = a.indexOf(key);
    if (i >= 0) a.splice(i, 1); else a.push(key);
    localStorage.setItem(WKEY, JSON.stringify(a));
    return a.includes(key);
  }
  function inWatch(key) { return getWatch().includes(key); }

  /* ---------- 数据加载 ---------- */
  async function loadCore() {
    const [ov, brands, quarters, health, anomalies, fx, cl] = await Promise.all([
      api("/api/overview"), api("/api/brands"), api("/api/quarters"),
      api("/api/health"), api("/api/anomalies"), api("/api/fx"),
      api("/api/countries"),
    ]);
    state.overview = ov; state.brands = brands; state.quarters = quarters;
    state.health = health; state.anomalies = anomalies; state.fx = fx;
    // 用后台 countries 表补全国名映射，保证与库一致（新增国家也自动生效）。
    (cl || []).forEach(x => { if (x && x.code) COUNTRY_NAMES[x.code] = x.name || COUNTRY_NAMES[x.code]; });
    $("#db-quarter").textContent = "最新季度 " + (ov.kpis.latest_quarter || "—");
    fillCrawlSelects();
  }

  /* 用已加载的品牌/国家填充「重新抓取」控件 */
  function fillCrawlSelects() {
    const b = $("#crawl-brand"), c = $("#crawl-country");
    if (!b || !c) return;
    b.innerHTML = '<option value="">全部品牌</option>' +
      (state.brands || []).map(x => `<option value="${esc(x.name)}">${esc(x.name)}</option>`).join("");
    c.innerHTML = '<option value="">全部国家</option>' +
      (state.countries || []).map(x => `<option value="${esc(x.code)}">${esc(x.name)}（${esc(x.code)}）</option>`).join("");
  }

  /* 触发后端重新抓取（POST /api/crawl，token 走请求头） */
  async function triggerCrawl(brand, country, token, force) {
    const q = new URLSearchParams();
    if (brand) q.set("brand", brand);
    if (country) q.set("country", country);
    if (force) q.set("force", "1");
    let r;
    try {
      r = await fetch(API + "/api/crawl?" + q.toString(), {
        method: "POST",
        headers: { "X-Crawl-Token": token || "" },
      });
    } catch (e) { setConn(false, "网络不可达：" + e.message); throw e; }
    let data;
    try { data = await r.json(); } catch { data = {}; }
    if (!r.ok) { setConn(false, "/api/crawl -> HTTP " + r.status); throw new Error(data.error || ("HTTP " + r.status)); }
    setConn(true);
    return data;
  }

  /* 轮询抓取任务状态，结束后提示 */
  async function pollCrawl(jobId, token) {
    for (let i = 0; i < 120; i++) {
      await new Promise(res => setTimeout(res, 3000));
      let st;
      try { st = await api("/api/crawl/status"); } catch { continue; }
      const job = (st.jobs || []).find(j => j.id === jobId);
      if (!job) break;
      if (job.status === "done") {
        let m = "抓取完成 ✅ " + job.id;
        if (job.kb_sync && job.kb_sync.conflicts && job.kb_sync.conflicts.length)
          m += "（KB 冲突未覆盖 " + job.kb_sync.conflicts.length + " 个）";
        toast(m); break;
      }
      if (job.status === "failed") { toast("抓取失败 ❌ " + job.id + (job.error ? "：" + job.error : "")); break; }
      if (job.status === "timeout") { toast("抓取超时已回收（已强制结束，可重试）" + (job.error ? "：" + job.error : "")); break; }
    }
  }

  /* ---------- 视图：总览 ---------- */
  async function renderOverview() {
    const ov = state.overview; if (!ov) return;
    const k = ov.kpis;
    const kcov = k.coverage || {};
    const kpis = [
      { l: "监控品牌", v: k.brands, s: "国内外友商" },
      { l: "覆盖国家", v: k.countries, s: "区域市场" },
      { l: "机型数", v: k.models, s: "已发现" },
      { l: "备件价条数", v: fmt(k.price_rows), s: "原币+CNY" },
      { l: "季度数", v: k.quarters, s: k.latest_quarter },
      // 覆盖口径（品牌×国家，看"有没有数据"）替代原先的"最新运行状态"口径：
      // 断点续跑会让最新运行恒为 skipped，据此统计会把正常覆盖误报成"抓取异常"。
      { l: "覆盖就绪", v: kcov.ok || 0, s: "品牌×国家·本季有数据", dot: "var(--green)" },
      { l: "覆盖待补", v: (kcov.stale || 0) + (kcov.failed || 0) + (kcov.empty || 0), s: "无数据/非本季", dot: "var(--amber)" },
      { l: "待修工单", v: k.open_issues, s: "自愈队列", dot: "var(--amber)" },
    ];
    let html = '<div class="grid kpis">';
    kpis.forEach(x => {
      html += `<div class="card kpi"><div class="kpi-label">${x.l}</div>
        <div class="kpi-value">${x.dot ? '<span class="dot" style="background:' + x.dot + '"></span>' : ""}${x.v}</div>
        <div class="kpi-sub">${x.s}</div></div>`;
    });
    html += "</div>";

    // 覆盖不足提醒（数据覆盖度 / 公信力）
    // 注意：判据是"本季是否有数据"(cov_status==='ok')，不是"本轮运行是否 success"。
    // 后者在断点续跑时恒为 skipped，会把已有数据的品牌误报成"0国"。
    const covByBrand = {};
    (ov.coverage || []).forEach(r => {
      (covByBrand[r.brand] = covByBrand[r.brand] || new Set());
      if (r.cov_status === "ok") covByBrand[r.brand].add(r.country);
    });
    const weak = Object.entries(covByBrand).filter(([b, s]) => s.size <= 1).map(([b, s]) => esc(b) + "(" + s.size + "国)");
    if (weak.length) {
      html += '<div class="warn-banner">⚠️ <b>覆盖不足：</b>' + weak.join("、") +
        ' — 仅覆盖单一国家，跨国比价公信力受限，建议补充更多国家抓取。</div>';
    }

    // 覆盖矩阵（品牌 × 国家）
    const cov = ov.coverage || [];
    const countries = [...new Set(cov.map(r => r.country))].sort();
    const brands = [...new Set(cov.map(r => r.brand))].sort();
    // 格子底色 = 实际覆盖情况（cov_status），不是"本轮运行状态"。
    // 断点续跑会让绝大多数格子 status='skipped'，若据此涂灰，会把已有上万条
    // 价行的格子显示成"无数据"——这正是"只有一块绿色"的原因。
    const covCls = { ok: "cov-ok", stale: "cov-stale", failed: "cov-fail", empty: "cov-empty" };
    const covLabel = { ok: "已覆盖本季", stale: "仅历史季度有数据，本季待补抓", failed: "抓取失败/受阻，无数据", empty: "从未抓到数据" };
    const runLabel = { success: "成功", skipped: "跳过（断点续跑/无新增）", failed: "失败", partial: "部分成功" };
    html += '<div class="panel"><div class="section-title">🌐 抓取覆盖矩阵</div>';
    html += '<div class="legend cov-legend">' +
      '<span><i class="cov-ok"></i>已覆盖本季</span>' +
      '<span><i class="cov-stale"></i>仅历史季度</span>' +
      '<span><i class="cov-fail"></i>失败/无数据</span>' +
      '<span><i class="cov-empty"></i>从未抓取</span>' +
      '<span class="muted">数字为该品牌/国家累计价行数；底色看「实际覆盖」，悬停可见本轮运行状态</span></div>';
    html += '<div class="scroll"><table><thead><tr><th>品牌</th>';
    countries.forEach(c => html += `<th>${cn(c)}</th>`);
    html += "</tr></thead><tbody>";
    brands.forEach(b => {
      html += `<tr><td><b>${esc(b)}</b></td>`;
      countries.forEach(c => {
        const r = cov.find(x => x.brand === b && x.country === c);
        if (!r) {
          html += `<td class="cov-cell cov-empty" title="${esc(b + "/" + cn(c) + "：无运行记录，尚未抓取")}">·</td>`;
          return;
        }
        const cs = r.cov_status || "empty";
        const mark = r.open_issues ? " ⚠" : "";
        const tip = [
          `${b}/${cn(c)} · ${covLabel[cs] || cs}`,
          `累计价行 ${r.price_rows || 0} 条（本季 ${r.price_rows_latest || 0} 条）`,
          `本轮运行：${runLabel[r.status] || r.status || "—"} · 写入 ${r.rows_written || 0} 条 · ${(r.finished_at || "—").replace("T", " ")}`,
          `最近一次成功：${r.last_success_at ? r.last_success_at.replace("T", " ") : "从未"}`,
          r.open_issues ? `未解决工单：${r.open_issues}` : "",
          r.anomaly_reason ? `备注：${r.anomaly_reason}` : "",
        ].filter(Boolean).join("\n");
        html += `<td class="cov-cell ${covCls[cs] || "cov-empty"}" title="${esc(tip)}">${fmt(r.price_rows || 0)}${mark}</td>`;
      });
      html += "</tr>";
    });
    html += "</tbody></table></div></div>";

    // 待修工单
    html += '<div class="panel"><div class="section-title">🛠 待修队列（自愈 Agent 工作来源）</div>';
    if (!state.anomalies.length) html += '<div class="empty">暂无待修项 ✅ 所有已知抓取均正常</div>';
    else {
      html += '<table><thead><tr><th>品牌/国家</th><th>问题</th><th>发现时间</th></tr></thead><tbody>';
      state.anomalies.forEach(a => {
        html += `<tr><td><span class="badge red">${esc(a.brand)}/${cn(a.country)}</span></td>
          <td>${esc(a.issue_summary)}</td><td class="muted">${esc(a.detected_at)}</td></tr>`;
      });
      html += "</tbody></table>";
    }
    html += "</div>";
    view.innerHTML = html;
  }

  /* ---------- 视图：比价矩阵（双模式：同型号跨国 / 同档位跨品牌） ---------- */
  let _mMode = "model";  // model | tier
  let _mSpec = "";       // 当前所选规格（模式①）
  let _mColor = "";      // 当前所选颜色（模式①）
  async function renderMatrix() {
    const q = state.quarters.length ? state.quarters[state.quarters.length - 1] : "";
    view.innerHTML = '<div class="filters"><div class="seg">' +
      '<button class="seg-btn ' + (_mMode === "model" ? "on" : "") + '" data-m="model">① 同型号·跨国比价</button>' +
      '<button class="seg-btn ' + (_mMode === "tier" ? "on" : "") + '" data-m="tier">② 同档位·跨品牌对标</button>' +
      '</div><span class="tag">公平比价原则：同一机型按规格/颜色分组，逐组跨国对比（不混算不同规格/颜色）</span></div>' +
      '<div id="mc-body"></div>';
    view.querySelectorAll(".seg-btn").forEach(b => b.onclick = () => { _mMode = b.dataset.m; renderMatrix(); });
    if (_mMode === "model") await renderModelCompare(q);
    else await renderTierMatrix(q);
  }

  async function renderModelCompare(q) {
    const bm = (await api("/api/brand_models")) || [];
    const brandsList = [...new Set(bm.map(r => r.brand))].sort();
    $("#mc-body").innerHTML = '<div class="filters">' +
      '<div class="field"><label>品牌</label><select id="mc-b">' + brandsList.map(b => `<option>${esc(b)}</option>`).join("") + '</select></div>' +
      '<div class="field mc-model-field"><label>基础机型</label>' +
      '<div id="mc-m-wrap" class="combo">' +
      '<input id="mc-search" class="mc-search" type="text" placeholder="检索或筛选机型（如 iPhone / Galaxy）" autocomplete="off">' +
      '<div id="mc-list" class="combo-list" role="listbox"></div>' +
      '</div>' +
      '<span id="mc-mcount" class="mcount"></span></div>' +
      '<div class="field"><label>规格/SKU</label><span id="mc-specs" class="chips"></span></div>' +
      '<div class="field"><label>颜色</label><span id="mc-colors" class="chips"></span></div>' +
      '<div class="field"><label>季度</label><select id="mc-q">' + (state.quarters || []).map(x => `<option ${x === q ? "selected" : ""}>${x}</option>`).join("") + '</select></div>' +
      '<div class="field"><label>&nbsp;</label><button id="mc-star" class="btn">☆ 关注此机型</button></div>' +
      '</div><div id="mc-result"></div>';
    const bSel = $("#mc-b"), qSel = $("#mc-q"), specBox = $("#mc-specs"), colorBox = $("#mc-colors"), starBtn = $("#mc-star"), searchBox = $("#mc-search"), mcount = $("#mc-mcount"), comboWrap = $("#mc-m-wrap"), listBox = $("#mc-list");
    let selectedModel = "";
    const modelsOf = b => (bm || []).filter(r => r.brand === b);
    const MAX_OPTS = 300;
    const syncStar = () => {
      const key = bSel.value + "|" + selectedModel;
      starBtn.textContent = (inWatch(key) ? "★ " : "☆ ") + "关注此机型";
      starBtn.classList.toggle("on", inWatch(key));
    };
    const fillModels = () => {
      const kw = (searchBox.value || "").trim().toLowerCase();
      let ms = modelsOf(bSel.value);
      if (kw) ms = ms.filter(r => (r.model || "").toLowerCase().includes(kw) || (r.base_model || "").toLowerCase().includes(kw));
      const total = ms.length;
      const shown = ms.slice(0, MAX_OPTS);
      if (total === 0) {
        listBox.innerHTML = '<div class="combo-empty">无匹配机型，换个关键字试试</div>';
        mcount.textContent = kw ? "无匹配机型" : "该品牌暂无机型数据";
        mcount.style.color = "var(--red)";
        return;
      }
      listBox.innerHTML = shown.map(r => {
        const sel = r.model === selectedModel ? " active" : "";
        const nSpec = (r.specs || []).length, nColor = (r.colors || []).length, nEd = (r.editions || []).length;
        const meta = `${r.tier || "—"} · ${(r.countries || []).length}国` +
          (nSpec ? " · " + nSpec + "规格" : "") +
          (nColor ? " · " + nColor + "色" : "") +
          (nEd ? " · " + nEd + "版本" : "");
        return `<div class="combo-item${sel}" role="option" data-m="${esc(r.model)}">` +
          `<span class="combo-name">${esc(r.model)}</span>` +
          `<span class="combo-meta">${esc(meta)}</span></div>`;
      }).join("");
      // 用 mousedown + preventDefault，避免输入框先 blur 导致面板提前关闭而点不到
      listBox.querySelectorAll(".combo-item").forEach(el => {
        el.onmousedown = (e) => { e.preventDefault(); pickModel(el.dataset.m); };
      });
      if (total > MAX_OPTS) { mcount.textContent = `共 ${total} 条 · 显示前 ${MAX_OPTS}，输入可缩小`; mcount.style.color = "var(--muted)"; }
      else { mcount.textContent = `共 ${total} 条`; mcount.style.color = "var(--muted)"; }
    };
    const showList = () => { comboWrap.classList.add("open"); fillModels(); };
    const hideList = () => { comboWrap.classList.remove("open"); };
    const showCur = () => { mcount.textContent = selectedModel ? "已选：" + selectedModel : "请选择机型"; mcount.style.color = "var(--muted)"; };
    const pickModel = (m) => {
      selectedModel = m;
      searchBox.value = "";            // 清空检索框，下次聚焦可重新浏览全部
      hideList();
      _mSpec = ""; _mColor = "";
      syncStar();
      showCur();
      draw();
    };
    const onComboKey = (e) => {
      const items = [...listBox.querySelectorAll(".combo-item")];
      if (e.key === "ArrowDown") {
        e.preventDefault();
        if (!comboWrap.classList.contains("open")) { showList(); return; }
        let i = items.findIndex(it => it.classList.contains("active"));
        i = i < 0 ? 0 : Math.min(items.length - 1, i + 1);
        items.forEach(it => it.classList.remove("active"));
        if (items[i]) { items[i].classList.add("active"); items[i].scrollIntoView({block: "nearest"}); }
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        let i = items.findIndex(it => it.classList.contains("active"));
        i = i < 0 ? items.length - 1 : Math.max(0, i - 1);
        items.forEach(it => it.classList.remove("active"));
        if (items[i]) { items[i].classList.add("active"); items[i].scrollIntoView({block: "nearest"}); }
      } else if (e.key === "Enter") {
        e.preventDefault();
        const act = listBox.querySelector(".combo-item.active") || items[0];
        if (act) pickModel(act.dataset.m);
      } else if (e.key === "Escape") { hideList(); }
    };
    const initBrand = () => {
      const ms = modelsOf(bSel.value);
      selectedModel = ms.length ? ms[0].model : "";
      searchBox.value = "";
      fillModels();
      syncStar();
      showCur();                       // 面板默认收起，mcount 显示当前选中
    };
    initBrand();
    let _msT = null;
    searchBox.oninput = () => { clearTimeout(_msT); _msT = setTimeout(() => { showList(); fillModels(); }, 60); };
    searchBox.onfocus = () => { showList(); fillModels(); };
    searchBox.onblur = () => { setTimeout(hideList, 160); };
    searchBox.onkeydown = onComboKey;
    bSel.onchange = () => { _mSpec = ""; _mColor = ""; initBrand(); draw(); };
    qSel.onchange = draw;
    starBtn.onclick = () => {
      const key = bSel.value + "|" + selectedModel;
      const on = toggleWatch(key);
      syncStar();
      toast(on ? "已加入「我的关注」" : "已取消关注");
    };
    async function draw() {
      const d = (await api("/api/model_compare?brand=" + encodeURIComponent(bSel.value) +
        "&base_model=" + encodeURIComponent(selectedModel) + "&quarter=" + encodeURIComponent(qSel.value) +
        (_mSpec ? "&spec=" + encodeURIComponent(_mSpec) : "") +
        (_mColor ? "&color=" + encodeURIComponent(_mColor) : ""))) || {};
      state.mcData = d;  // 缓存供单元格下钻明细使用
      if ((d.specs || []).length) {
        specBox.innerHTML = '<button class="chip ' + (_mSpec === "" ? "on" : "") + '" data-s="">全部规格</button>' +
          (d.specs || []).map(s => `<button class="chip ${_mSpec === s ? "on" : ""}" data-s="${esc(s)}">${esc(s)}</button>`).join("");
        specBox.querySelectorAll(".chip").forEach(ch => ch.onclick = () => { _mSpec = ch.dataset.s; draw(); });
      } else specBox.innerHTML = '<span class="muted">该机型无多规格</span>';
      // 颜色 chips
      if ((d.colors || []).length) {
        colorBox.innerHTML = '<button class="chip ' + (_mColor === "" ? "on" : "") + '" data-c="">全部颜色</button>' +
          (d.colors || []).map(s => `<button class="chip ${_mColor === s ? "on" : ""}" data-c="${esc(s)}">${esc(s)}</button>`).join("");
        colorBox.querySelectorAll(".chip").forEach(ch => ch.onclick = () => { _mColor = ch.dataset.c; draw(); });
      } else colorBox.innerHTML = '<span class="muted">该机型无多颜色</span>';
      // 定价结构（档位定价说明）：独立拉取，失败不影响主矩阵
      let pb = null;
      try {
        pb = await api("/api/price_bands?brand=" + encodeURIComponent(bSel.value) +
          "&model=" + encodeURIComponent(selectedModel) + "&quarter=" + encodeURIComponent(qSel.value));
      } catch (e) { pb = null; }
      // 组装：分组多表 + 定价结构 + 到手估算 + 第三方对比 + 降价预警
      let html = modelCompareHTML(d, pb);
      if (pb) html += priceBandsHTML(pb);
      html += estimatorHTML(d);
      html += '<details class="aux" id="mc-tp"><summary>🔧 第三方兼容件参考价对比（官方价是否合理？）</summary>' +
        '<div id="mc-tp-body" class="aux-body">展开后加载…</div></details>';
      html += '<details class="aux" id="mc-drop"><summary>📉 降价预警（环比上季度 · 跨规格合并）</summary>' +
        '<div id="mc-drop-body" class="aux-body">展开后加载…</div></details>';
      $("#mc-result").innerHTML = html;
      wireEstimator(d);
      wireAux(d);
    }
    await draw();
  }

  /** 定价结构：说明「某国的这个价格实际涵盖哪些备件」。
   * 背景：德国等市场把多个不同备件定在同一价位（档位定价 / groupCode=PHONE_OTHER），
   * 若把该价格挂在单一备件名下，会被误读为"这个零件的成本价"，
   * 进而得出"德国卡托比中国贵 50 倍"这种数学正确、归因错误的结论。 */
  function priceBandsHTML(pb) {
    pb = pb || {};
    const cs = (pb.countries || []).filter(c => (c.bands || []).length);
    if (!cs.length) return "";
    let h = '<details class="aux" open><summary>💡 定价结构提示：这些价格实际「涵盖」哪些备件</summary><div class="aux-body">';
    h += '<div class="hint">某些市场（尤其德国）把多个<b>不同备件</b>定在同一价位，'
      + '属 <b>档位定价</b>而非零件成本定价。此时把该价格挂在单一备件名下，会被误读为'
      + '「这个零件的成本」，跨市场比价结论随之失真。<br>'
      + '判据：同一机型内 ≥' + (pb.min_parts || 5) + ' 个不同备件的价格落在 '
      + Math.round((pb.tol || 0.15) * 100) + '% 容差内。</div>';
    cs.forEach(c => {
      const rng = c.range_ratio == null ? "" : ('；该机型价格动态范围仅 <b>' + c.range_ratio + '×</b>');
      h += '<div style="margin:12px 0 4px"><b>' + esc(cn(c.country)) + '</b>'
        + '<span class="muted">（共 ' + (c.n_parts || 0) + ' 个备件' + rng + '）</span></div>';
      (c.bands || []).forEach(b => {
        h += '<div style="margin:2px 0 2px 10px">▸ <b>' + fmt(b.lo, 2) + '–' + fmt(b.hi, 2) + ' '
          + esc(c.currency || '') + '</b> 档，涵盖 <b>' + b.size + '</b> 个不同备件'
          + '（占该机型 ' + Math.round((b.share || 0) * 100) + '%）：<br>'
          + '<span style="margin-left:14px">'
          + (b.members || []).map(m => esc(m.name)
              + (m.spec ? ' <span class="muted">' + esc(m.spec) + '</span>' : '')
              + '<span class="muted"> ' + fmt(m.price, 2) + '</span>').join('、')
          + '</span></div>';
      });
    });
    h += '<div class="hint">→ 解读建议：以上备件的价格是<b>档位价</b>，不能直接与他国零件价横向比较'
      + '（例如"德国卡托比中国贵 50 倍"属数学正确、归因错误的结论）。</div>';
    h += '</div></details>';
    return h;
  }

  function modelCompareHTML(d, pb) {
    d = d || {};
    const groups = d.groups || [];
    // 档位定价索引：{国家: {"件名|规格": {label, tip}}}，用于在单元格上直标"这是档位价"，
    // 不必让用户滚到下方面板才发现。详见 priceBandsHTML 的说明。
    const bandIdx = {};
    ((pb || {}).countries || []).forEach(c => {
      const m = bandIdx[c.country] = {};
      (c.bands || []).forEach(b => {
        const label = fmt(b.lo, 2) + "–" + fmt(b.hi, 2) + " " + (c.currency || "") + "档";
        (b.members || []).forEach(mem => {
          m[mem.name + "|" + (mem.spec || "")] = {
            tip: "该价格属「" + label + "」档位定价，同档涵盖 " + b.size + " 个不同备件（占该机型 "
              + Math.round((b.share || 0) * 100) + "%）：" + (b.members || []).map(x => x.name).join("、")
              + "。这是档位价，不是该零件单独的成本价，不宜与他国零件价直接横比。",
          };
        });
      });
    });
    if (!groups.length) {
      const meta = d.model_meta || null;
      let loc = null;
      if (meta && meta.model_url_locator) { try { loc = JSON.parse(meta.model_url_locator); } catch (e) { loc = null; } }
      const reason = (loc && loc.reason) || "该机型在所选季度暂无价格数据";
      const humanUrl = meta && meta.model_page_url;
      const url = humanUrl || (meta && meta.model_url) || null;
      const label = humanUrl ? "官方价目页" : (meta ? (meta.model_url_kind || "官方页") : "官方页");
      let html = '<div class="panel"><div class="nodata-box">';
      html += '<div class="nodata-title">⚠️ 该机型官方未发布维修价</div>';
      html += '<div class="nodata-reason">' + esc(reason) + '</div>';
      if (url) {
        html += '<div class="nodata-actions"><a class="btn" href="' + escAttr(url) + '" target="_blank" rel="noopener">🔗 前往官网核实（' + esc(label) + '）</a></div>';
      }
      html += '<div class="nodata-note">平台已如实显示「无数据」：原官网渲染表价已于 2026-09-07 按用户选择清空，未用无据可查的价格冒充官方核实价。</div>';
      html += '</div></div>';
      return html;
    }
    let html = '<div class="panel"><div class="section-title">🧮 同基础机型·跨国比价 — ' +
      esc(d.brand || "") + ' ' + esc(d.base_model || "") + '（' + (d.quarter || "") + ' · 单元格=CNY；底色=该备件内各国高低）</div>';
    const hasNoSplit = (d.groups || []).some(g => (g.parts || []).some(p =>
      Object.values(p.prices || {}).some(pc => pc.has_labor_split !== 1 && pc.material_fee == null && pc.labor_fee == null)));
    const hasSplit = (d.groups || []).some(g => (g.parts || []).some(p =>
      Object.values(p.prices || {}).some(pc => pc.has_labor_split === 1)));
    html += '<div class="hint">同一机型按 <b>(规格, 颜色, 版本)</b> 分组，每组<b>独立</b>跨国比价，<b>不混算</b>不同规格/颜色/版本。上方 chips 可只留单一规格/颜色；下方每组一张表。点击 🔗 可查看官方来源页。</div>';
    let leg = '<div class="hint warn">';
    if (hasSplit) leg += '🔧 <b>人工费</b>=官网明确单列的人工费金额（如小米《保外人工指导价》），悬停看官网原文说明、🔗证 跳取证页；';
    if (hasNoSplit) leg += '※ = 官网<b>仅给总价、未单列人工费</b>（平台显式标注，绝不编造）；';
    leg += '本平台<b>无种子/占位数据</b>：所有价格均来自官方页真实抓取；单元格 tooltip 含汇率来源与时点。</div>';
    html += leg;
    const cav = brandCaveat(d.brand);
    if (cav)
      html += '<div class="hint warn">⚠️ ' + esc(cav) + '</div>';
    // 参考价（B 方案）说明——只在真的出现参考价时展示，避免无谓噪音
    const hasRef = (d.groups || []).some(g => (g.parts || []).some(p =>
      Object.values(p.prices || {}).some(pc => pc.is_reference === 1)));
    if (hasRef)
      html += '<div class="hint"><b>参考·中国</b>（灰底 + 紫虚线）= 该机型在本地官方<b>查无备件价</b>'
        + '（OPPO 各区官网只公布在售机型的价目；已下架机型当地无价，这<b>不是</b>抓取失败）。'
        + '此处借用<b>同机型 OPPO 中国官网价（CNY）</b>作参考，<b>非当地官方价</b>：'
        + '灰色单元格不参与"最低国 / 最高国 / 价差"的本地价差归因。</div>';
    groups.forEach(g => {
      const specLabel = g.spec || "未注明";
      const colorLabel = g.color || "未注明";
      const editionLabel = g.edition || "未注明";
      const countries = g.countries || [];
      html += '<div class="grp"><div class="grp-title">📦 规格 <b>' + esc(specLabel) + '</b> · 颜色 <b>' + esc(colorLabel) +
        '</b> · 版本 <b>' + esc(editionLabel) + '</b>（覆盖 ' + countries.length + ' 国）</div>';
      if (!(g.parts || []).length) { html += '<div class="empty">该配置暂无价格</div></div>'; return; }
      html += '<div class="scroll"><table class="heat"><thead><tr><th class="rowlabel">规范品类 / 备件（规格）</th>';
      countries.forEach(c => html += `<th>${cn(c)}</th>`);
      html += '<th>最低国</th><th>最高国</th><th>价差</th></tr></thead><tbody>';
      (g.parts || []).forEach(p => {
        const vals = countries.map(c => (p.prices[c] && p.prices[c].cny != null) ? p.prices[c].cny : null);
        const valid = vals.filter(v => v != null);
        if (!valid.length) return;
        // 参考价（is_reference=1）不是当地官方价，**不得**参与"最低国/最高国/价差"的归属与排序：
        // 否则会把"德国"标成最低价国 —— 实际德国根本没有本地报价，那个数字只是 CN 价的回显。
        // 故低/高基准优先取"真实本地价"国家；若某行全是参考价（该配置在 CN 无对应规格/颜色），
        // 则退化为全部值，且不归属任何国家（loC/hiC 保持空）。
        const realVals = countries
          .map((c, i) => (vals[i] != null && p.prices[c].is_reference !== 1) ? vals[i] : null)
          .filter(v => v != null);
        const basis = realVals.length ? realVals : valid;
        const lo = Math.min(...basis), hi = Math.max(...basis), span = (hi - lo) || 1;
        // 行标签必须带上「规格」。同一备件会按存储规格拆成多行（8G+128G / 8G+256G /
        // 12G+256G / 12G+512G），这是分组键 (品类,件名,规格) 的正确行为——数据没错；
        // 但若行标签只画 cat+part，五行主板会全部渲染成「主板 / 主板」，看起来像重复行。
        // 另：cat 与 part 相同时（主板/主板、电池/电池）不再重复输出第二行，避免噪音。
        const specTag = p.spec ? `<span class="spectag">${esc(p.spec)}</span>` : "";
        const subPart = (p.part && p.part !== p.cat)
          ? `<br><small class="muted">${esc(p.part)}</small>` : "";
        html += `<tr><td class="rowlabel">${esc(p.cat)}${specTag}${subPart}</td>`;
        let loC = "", hiC = "";
        countries.forEach((c, i) => {
          const v = vals[i];
          if (v == null) { html += '<td class="muted">—</td>'; return; }
          // 参考价不认领"最低国/最高国"（它不是当地官方价，见上方 lo/hi 基准说明）
          if (p.prices[c].is_reference !== 1) {
            if (v === lo) loC = cn(c);
            if (v === hi) hiC = cn(c);
          }
          const t = (v - lo) / span;
          const pc = p.prices[c];
          const cur = pc.currency, raw = pc.price;
          // 人工费举证（P1-1）：官网单列→显式金额+原文+取证链接；未单列→显式标注不编造
          let laborBadge = "", laborTip = "";
          if (pc.has_labor_split === 1) {
            const ln = (pc.labor_note || "官网单列人工费");
            laborBadge = ` <span class="lb labor-ok" title="${esc(ln)}">🔧人工费¥${fmt(pc.labor_fee)}</span>`;
            laborTip = " · " + ln;
            if (pc.labor_source_url)
              laborBadge += ` <a class="srclink" href="${esc(pc.labor_source_url)}" target="_blank" rel="noopener" title="人工费取证来源页" onclick="event.stopPropagation()">🔗证</a>`;
          } else {
            laborTip = " · " + (pc.labor_note || "官网未单列人工费，仅提供含人工的总维修价");
          }
          let tip = `${esc(cn(c))} 原币 ${raw} ${cur}（规格 ${esc(pc.spec || "—")} / 颜色 ${esc(pc.color || "—")}${pc.edition ? " / 版本 " + esc(pc.edition) : ""}）`;
          if (pc.material_fee != null || pc.labor_fee != null)
            tip += ` · 物料¥${fmt(pc.material_fee)} 人工¥${fmt(pc.labor_fee)}`;
          else
            tip += ` · 官方未提供物料/人工拆分（仅总价）`;
          tip += laborTip;
          tip += ` · ${pc.tax_included ? "含税" : "未含税"}`;
          if (pc.captured_at) tip += ` · 抓取 ${esc(pc.captured_at)}`;
          if (pc.rate_source) tip += ` · 汇率[${esc(pc.rate_source)}${pc.rate_as_of ? " @ " + esc(pc.rate_as_of) : ""}]`;
          // 取证链接：一律使用库内"机型级链接"（model_url，已实测校验）；
          // 绝不在前端拼接猜测 URL（历史上拼 support.apple.com/<slug> 实测全为软 404）
          const src = " " + modelLinkHTML(pc, { stopProp: true });
          const nosplit = (pc.has_labor_split !== 1 && pc.material_fee == null && pc.labor_fee == null)
            ? ' <sup class="nosplit" title="' + esc(pc.labor_note || "官方仅提供总价，未拆分物料/人工费") + '">※</sup>' : "";
          const seedBadge = (pc.is_seed === 1)
            ? ' <sup class="seed" title="演示/种子数据，非真实官网取证">seed</sup>' : "";
          // 档位定价标记：该国此价属"多个不同备件同价"的档位，非该零件单独成本价
          const _bi = bandIdx[c] && bandIdx[c][p.part + "|" + (p.spec || "")];
          const bandMark = _bi ? ` <sup class="band" title="${esc(_bi.tip)}">⚖档</sup>` : "";
          // 单元格可点击下钻：展示本平台实际抓取到的该机型+国家+规格明细（准确，不依赖外部误导页）
          const oc = `onclick="openCompareDetail('${escAttr(c)}','${escAttr(specLabel)}','${escAttr(colorLabel)}','${escAttr(editionLabel)}')"`;
          // 单元格口径：主数字是 **CNY 折算值**（表头「单元格=CNY」、底色排序都基于它），
          // 故小字必须写 CNY。历史 bug：小字直接输出原币币种码，导致「数字是 CNY、
          // 标签却是外币」——日本格显示 2,947 JPY，实为 2,947 CNY（原币 68,200 JPY），
          // 把本来正确的跨区域数据也显得像脏数据。原币价改用 ≈ 跟随其后。
          const isCny = (cur === "CNY");
          const oriTxt = isCny ? "" : ` ≈ ${fmt(raw, 2)} ${esc(cur)}`;
          // 参考价（B 方案）：本地官方无价，借用同机型 OPPO 中国官网价（CNY）。
          // 必须明确标注、且底色用灰色（不走热力色），避免被误读为"真实本地价排名"。
          const isRef = (pc.is_reference === 1);
          const refBadge = isRef
            ? ' <sup class="ref" title="参考价：本地官方未提供该机型备件价，借用同机型 OPPO 中国官网价（CNY），非本地官方价，不参与本地价差归因">参考·中国</sup>'
            : "";
          const bg = isRef ? "#f1f5f9" : heatColor(t);
          html += `<td class="cell-click${isRef ? " ref-cell" : ""}" style="background:${bg}" title="${esc(tip)}" ${oc}>${fmt(v)}<br><small class="muted">CNY${oriTxt}${refBadge}${src}${nosplit}${seedBadge}</small>${laborBadge}${bandMark}</td>`;
        });
        const diff = lo ? Math.round((hi - lo) / lo * 100) : 0;
        html += `<td>${esc(loC)}</td><td>${esc(hiC)}</td><td>${diff > 0 ? "+" + diff + "%" : "—"}</td></tr>`;
      });
      html += '</tbody></table></div></div>';
    });
    html += '<div class="legend"><span><i style="background:#dcfce7"></i>该备件内较低</span>' +
      '<span><i style="background:#fef3c7"></i>居中</span>' +
      '<span><i style="background:#fee2e2"></i>该备件内较高</span>' +
      '<span class="muted">同一机型跨国价差主要反映汇率/税费/区域定价；切换上方 chips 可只看某规格/颜色</span></div></div>';
    return html;
  }

  /* 单元格下钻：展示本平台实际抓取到的该机型+国家+规格明细（准确真实数据，
     避免点外部品牌级链接被 Apple 官网默认重定向到最新机型） */
  function openCompareDetail(country, spec, color, edition) {
    const d = state.mcData; if (!d) { toast("明细数据未就绪"); return; }
    const brand = d.brand, base = d.base_model, quarter = d.quarter;
    const group = (d.groups || []).find(g =>
      (g.spec || "未注明") === (spec || "未注明") &&
      (g.color || "未注明") === (color || "未注明") &&
      (g.edition || "未注明") === (edition || "未注明"));
    if (!group) { toast("该配置无明细"); return; }
    const rows = (group.parts || []).map(p => {
      const pc = (p.prices || {})[country];
      if (!pc) return `<tr><td>${esc(p.cat)}</td><td>${esc(p.part)}</td><td colspan="6" class="muted">该国无价</td></tr>`;
      let labor;
      if (pc.has_labor_split === 1)
        labor = `🔧人工费¥${fmt(pc.labor_fee)}（物料¥${fmt(pc.material_fee)}）`;
      else
        labor = "官方未单列（仅总价）";
      const srcLink = modelLinkHTML(pc, { showModel: true });
      const laborLink = pc.labor_source_url
        ? `<a class="srclink" href="${esc(pc.labor_source_url)}" target="_blank" rel="noopener" title="人工费取证页">🔗证</a>` : "";
      const ln = pc.labor_note ? `<div class="muted" style="font-size:11px">${esc(pc.labor_note)}</div>` : "";
      return `<tr><td>${esc(p.cat)}</td><td>${esc(p.part)}${ln}</td>` +
        `<td class="num">${fmt(pc.cny)}</td><td>${esc(pc.price)} ${esc(pc.currency)}</td>` +
        `<td>${labor}</td><td>${pc.tax_included ? "含税" : "未税"}</td>` +
        `<td class="muted">${esc(pc.captured_at || "")}</td>` +
        `<td>${srcLink}${laborLink}</td></tr>`;
    }).join("");
    const title = `${esc(brand)} ${esc(base)} · ${cn(country)} · 规格 ${esc(spec)} / 颜色 ${esc(color)} / 版本 ${esc(edition)}（${esc(quarter)}）`;
    const body =
      `<div class="modal"><div class="modal-h">🔍 本平台实际抓取明细 — ${title}` +
      `<button class="modal-x" onclick="closeCompareDetail()">✕</button></div>` +
      `<div class="hint warn">以下为本平台从官方页<b>真实抓取</b>到的该机型备件价（CNY 按实时汇率折算）。` +
      `取证列的 <b>🔗机型</b>／<b>🔗行</b> 是<b>本机型级链接</b>（已实测请求并确认命中本机型，鼠标悬停可看精确定位方式）；` +
      `<b>🔗牌⚠</b> 表示官网未提供机型级定位、仅为品牌入口页，不可当作本机型精确报价页；<b>📄</b> 为官网人可读页面。</div>` +
      `<div class="scroll"><table class="tbl"><thead><tr><th>品类</th><th>备件</th><th class="num">CNY</th><th>原币</th><th>物料/人工</th><th>含税</th><th>抓取时间</th><th>取证</th></tr></thead><tbody>${rows}</tbody></table></div></div>`;
    let mask = document.getElementById("cmp-mask");
    if (!mask) {
      mask = document.createElement("div");
      mask.id = "cmp-mask"; mask.className = "modal-mask";
      mask.onclick = (e) => { if (e.target === mask) closeCompareDetail(); };
      document.body.appendChild(mask);
    }
    mask.innerHTML = body;
    mask.style.display = "flex";
  }
  function closeCompareDetail() { const m = document.getElementById("cmp-mask"); if (m) m.style.display = "none"; }

  /* 本文件整体包在 IIFE 内，而单元格/关闭按钮用的内联 onclick= 是在全局作用域求值的，
     不显式挂到 window 就会报 "openCompareDetail is not defined"，导致比价矩阵点开下钻明细
     始终无反应（下钻明细正是核对"该机型取证链接"的地方，必须可用）。 */
  window.openCompareDetail = openCompareDetail;
  window.closeCompareDetail = closeCompareDetail;

  /* 跨境到手估算器：物料 + 国际运费 + 进口关税 = 到手 CNY，并与国内价对比 */
  function estimatorHTML(d) {
    const cats = [...new Set((d.groups || []).flatMap(g => (g.parts || []).map(p => p.cat)))].sort();
    if (!cats.length) return "";
    const copts = (d.countries || []).map(c => `<option value="${esc(c)}" ${c === "cn" ? "selected" : ""}>${cn(c)}</option>`).join("");
    const catOpts = cats.map(c => `<option>${esc(c)}</option>`).join("");
    return '<div class="panel est"><div class="section-title">🧾 跨境到手估算（物料 + 运费 + 关税）</div>' +
      '<div class="filters">' +
      '<div class="field"><label>备件品类</label><select id="est-cat">' + catOpts + '</select></div>' +
      '<div class="field"><label>来源国</label><select id="est-c">' + copts + '</select></div>' +
      '<div class="field"><label>国际运费 ¥</label><input id="est-ship" type="number" min="0" value="0" /></div>' +
      '<div class="field"><label>进口关税 %</label><input id="est-tax" type="number" min="0" value="0" /></div>' +
      '<div class="field"><label>&nbsp;</label><button id="est-go" class="btn primary">估算到手价</button></div>' +
      '</div><div id="est-out" class="est-out"></div></div>';
  }
  function wireEstimator(d) {
    const cSel = $("#est-cat"), coSel = $("#est-c");
    if (!cSel || !coSel) return;
    const cnyOf = (cat, country) => {
      for (const g of (d.groups || [])) for (const p of (g.parts || []))
        if (p.cat === cat && p.prices[country] && p.prices[country].cny != null) return p.prices[country].cny;
      return null;
    };
    $("#est-go").onclick = () => {
      const cat = cSel.value, country = coSel.value;
      const base = cnyOf(cat, country);
      const out = $("#est-out");
      if (base == null) { out.innerHTML = '<span class="muted">该组合暂无价格</span>'; return; }
      const ship = Number($("#est-ship").value) || 0;
      const tax = Number($("#est-tax").value) || 0;
      const tariff = base * tax / 100;
      const total = base + ship + tariff;
      let cmp = "";
      const dom = cnyOf(cat, "cn");
      if (dom != null && country !== "cn") {
        const diff = dom - total;
        cmp = diff >= 0
          ? `比在国内买（¥${fmt(dom)}）<b class="green">省 ¥${fmt(diff)}</b>`
          : `比在国内买（¥${fmt(dom)}）<b class="red">贵 ¥${fmt(-diff)}</b>`;
      } else if (country === "cn") {
        cmp = '<span class="muted">（来源国即国内，无跨境对照）</span>';
      } else {
        cmp = '<span class="muted">（国内无对照价）</span>';
      }
      out.innerHTML = `<div class="est-line">来源国 <b>${cn(country)}</b> 物料 ¥${fmt(base)} + 运费 ¥${fmt(ship)} + 关税 ¥${fmt(tariff)} = <b>到手 ¥${fmt(total)}</b></div>` +
        `<div class="est-line">${cmp}</div>`;
    };
  }

  /* 第三方对比 + 降价预警：懒加载（展开时才请求） */
  function wireAux(d) {
    const tp = $("#mc-tp"), drop = $("#mc-drop");
    const cats = [...new Set((d.groups || []).flatMap(g => (g.parts || []).map(p => p.cat)))].sort();
    let tpLoaded = false, dropLoaded = false;
    if (tp) tp.addEventListener("toggle", async () => {
      if (!tp.open || tpLoaded) return; tpLoaded = true;
      const body = $("#mc-tp-body"); body.innerHTML = "加载中…";
      try {
        const res = await Promise.all(cats.map(c =>
          api("/api/third_party?brand=" + encodeURIComponent(d.brand) +
            "&cat=" + encodeURIComponent(c) + "&quarter=" + encodeURIComponent(d.quarter))));
        body.innerHTML = tpHTML(res);
      } catch (e) { body.innerHTML = "加载失败：" + esc(e.message); }
    });
    if (drop) drop.addEventListener("toggle", async () => {
      if (!drop.open || dropLoaded) return; dropLoaded = true;
      const body = $("#mc-drop-body"); body.innerHTML = "加载中…";
      try {
        const res = await Promise.all(cats.map(c =>
          api("/api/price_history?brand=" + encodeURIComponent(d.brand) +
            "&base_model=" + encodeURIComponent(d.base_model) + "&cat=" + encodeURIComponent(c) +
            "&quarter=" + encodeURIComponent(d.quarter))));
        body.innerHTML = dropHTML(res);
      } catch (e) { body.innerHTML = "加载失败：" + esc(e.message); }
    });
  }
  function tpHTML(list) {
    const rows = (list || []).filter(x => x && x.official_avg_cny != null);
    if (!rows.length) return '<div class="empty">暂无官方价可对比</div>';
    let h = '<div class="scroll"><table><thead><tr><th>规范品类</th><th class="num">官方各国均价</th>' +
      '<th class="num">兼容件参考</th><th class="num">溢价倍数</th><th>说明</th></tr></thead><tbody>';
    rows.forEach(r => {
      const factor = r.factor;
      const prem = factor == null ? "—" : (factor >= 1 ? "官方贵 " + ((factor - 1) * 100).toFixed(0) + "%" : "兼容件便宜 " + ((1 - factor) * 100).toFixed(0) + "%");
      const cls = factor == null ? "gray" : (factor >= 1 ? "red" : "green");
      h += `<tr><td>${esc(r.cat)}</td><td class="num">${money(r.official_avg_cny)}</td>` +
        `<td class="num">${r.ref_cny != null ? money(r.ref_cny) : "—"}</td>` +
        `<td class="num"><span class="badge ${cls}">${factor != null ? factor.toFixed(2) + "×" : "—"}</span></td>` +
        `<td class="muted">${esc(r.note || "")}${r.is_demo_estimate ? "（演示估算）" : ""}</td></tr>`;
    });
    h += "</tbody></table></div>";
    h += '<div class="legend"><span class="muted">溢价倍数 = 兼容件参考价 ÷ 官方均价；&lt;1 表示兼容件更便宜，可优先考虑第三方维修。兼容件价为演示估算，真实值应来自兼容件聚合。</span></div>';
    return h;
  }
  function dropHTML(list) {
    // 跨规格合并：每个 (cat, country) 取各季度均值，再算环比
    const items = [];
    let up = 0;
    (list || []).forEach(r => {
      if (!r || !r.data) return;
      const byCQ = {};
      for (const c in r.data) for (const pt of r.data[c]) {
        (byCQ[c] = byCQ[c] || {})[pt.quarter] = (byCQ[c][pt.quarter] || []).concat(pt.cny);
      }
      for (const c in byCQ) {
        const qs = Object.keys(byCQ[c]).sort();
        if (qs.length < 2) continue;
        const avg = q => { const a = byCQ[c][q].filter(v => v != null); return a.length ? a.reduce((s, v) => s + v, 0) / a.length : null; };
        const prev = avg(qs[qs.length - 2]), latest = avg(qs[qs.length - 1]);
        if (prev == null || latest == null || prev <= 0) continue;
        const delta = (latest - prev) / prev;
        if (delta < 0) items.push({ cat: r.cat, country: c, prev, latest, delta });
        else up++;
      }
    });
    if (!items.length) return '<div class="empty">本季度该机型各备件均无降价（' + up + ' 项上涨/持平）✅</div>';
    items.sort((a, b) => a.delta - b.delta);
    let h = '<div class="scroll"><table><thead><tr><th>规范品类</th><th>国家</th><th class="num">上季</th><th class="num">本季</th><th class="num">降幅</th></tr></thead><tbody>';
    items.slice(0, 20).forEach(it => {
      h += `<tr><td>${esc(it.cat)}</td><td>${cn(it.country)}</td>` +
        `<td class="num">${money(it.prev)}</td><td class="num">${money(it.latest)}</td>` +
        `<td class="num"><span class="badge green">${(it.delta * 100).toFixed(1)}%</span></td></tr>`;
    });
    h += "</tbody></table></div>";
    h += '<div class="legend"><span class="muted">共 ' + items.length + ' 项降价、' + up + ' 项上涨/持平（跨规格合并取均值）。降幅为环比上季度。</span></div>';
    return h;
  }

  async function renderTierMatrix(q) {
    const [tiers, countries] = await Promise.all([api("/api/tiers"), api("/api/countries")]);
    const tl = tiers || [];
    const cl = countries || [];
    const tier = tl[0] || "";
    $("#mc-body").innerHTML = '<div class="filters">' +
      '<div class="field"><label>档位</label><select id="tm-tier">' + tl.map(t => `<option ${t === tier ? "selected" : ""}>${esc(t)}</option>`).join("") + '</select></div>' +
      '<div class="field"><label>国家（可选）</label><select id="tm-c"><option value="">全部国家合并</option>' +
      cl.map(c => `<option value="${esc(c.code)}">${esc(c.name)}</option>`).join("") + '</select></div>' +
      '<div class="field"><label>季度</label><select id="tm-q">' + (state.quarters || []).map(x => `<option ${x === q ? "selected" : ""}>${x}</option>`).join("") + '</select></div>' +
      '<div class="field"><label>品类</label><select id="tm-cat">' +
      MODEL_CATS.map(([v, label]) => `<option value="${v}"${v === "phone" ? " selected" : ""}>${label}</option>`).join("") +
      '</select></div>' +
      '</div><div class="hint">仅同档位机型参与均值（旗舰比旗舰、入门比入门），同一机型多个规格/颜色按"参考配置"取代表价，避免混算导致比价失真。' +
      '<b>品类必须分开看</b>：平板的「屏幕」和手机的「屏幕」不是同一个东西，混算出的均价两头不靠（如三星电池：手机 ¥522 vs 平板 ¥691，差 32%）。</div>' +
      '<div id="tm-result"></div>';
    const tierSel = $("#tm-tier"), cSel = $("#tm-c"), qSel = $("#tm-q"), catSel = $("#tm-cat");
    const draw = async () => {
      const d = await api("/api/tier_matrix?tier=" + encodeURIComponent(tierSel.value) +
        "&country=" + encodeURIComponent(cSel.value) + "&quarter=" + encodeURIComponent(qSel.value) +
        "&category=" + encodeURIComponent(catSel.value));
      $("#tm-result").innerHTML = tierMatrixHTML(d);
    };
    tierSel.onchange = cSel.onchange = qSel.onchange = catSel.onchange = draw;
    await draw();
  }

  function tierMatrixHTML(d) {
    d = d || {};
    const rows = d.rows || [];
    const brands = d.brands || [];
    if (!rows.length) return '<div class="panel"><div class="empty">该档位在所选季度暂无可比备件</div></div>';
    const note = d.country ? "（" + d.country + "）" : "（全部国家合并）";
    let html = '<div class="panel"><div class="section-title">🧮 同档位·跨品牌对标 — ' +
      esc(d.tier || "") + note + '（' + (d.quarter || "") + ' · 单元格=该品牌同档位 CNY 均价）</div>';
    html += '<div class="hint warn">仅同档位机型参与均值；<b>库内已无 seed 占位数据</b>，仅用真实抓取均价。';
    html += '<b>按「规范件名(+规格)」分组</b>——镜头、镜片、镜头盖属不同备件，不并入同一均值；';
    html += (d.country
      ? '已限定单国，展示该国有数据的全部备件。'
      : '仅展示 <b>≥2 个品牌都有数据</b>的备件（这是"跨品牌可比"的前提）。');
    html += '<br><b>口径提示</b>：Apple 官网只公布「服务价」（含人工的整体维修报价），'
      + '与安卓品牌的「零件价」口径不同，故其备件（屏幕损坏/电池维修服务等）'
      + '多数无法按件名与其他品牌匹配，<b>不会强行并入同一行</b>——跨品牌解读时需注意此差异。';
    if (state.fx && state.fx.rates && state.fx.rates.length)
      html += '<span class="muted"> 折算汇率：' +
        (state.fx.rates.find(r => r.currency === "USD") ? "USD→CNY " + fmt(state.fx.rates.find(r => r.currency === "USD").rate_to_cny, 4) : "") +
        ' · 来源 ' + esc(state.fx.rates[0].rate_source || "—") + (state.fx.rates[0].rate_as_of ? " @ " + esc(state.fx.rates[0].rate_as_of) : "") + '</span>';
    html += '</div>';
    html += '<div class="scroll"><table class="heat"><thead><tr><th class="rowlabel">规范件名</th>';
    brands.forEach(b => {
      const n = ((d.models_per_brand || {})[b]) || 0;
      html += `<th>${esc(b)}<br><small class="muted">${n} 机型</small></th>`;
    });
    html += '</tr></thead><tbody>';
    (d.cats || []).forEach(cat => {
      const group = rows.filter(r => r.cat === cat);
      if (!group.length) return;
      html += `<tr class="catrow"><td colspan="${brands.length + 1}">${esc(cat)}</td></tr>`;
      group.forEach(r => {
        const cells = r.cells || {};
        // 按「行内」着色：同一备件横向比，最贵=红、最便宜=绿
        const vals = brands.map(b => cells[b]).filter(v => v != null);
        const rmin = Math.min.apply(null, vals), rmax = Math.max.apply(null, vals);
        html += `<tr><td class="rowlabel">${esc(r.label)}</td>`;
        brands.forEach(b => {
          const v = cells[b];
          if (v == null) { html += '<td class="muted">—</td>'; return; }
          const t = (v - rmin) / ((rmax - rmin) || 1);
          html += `<td style="background:${heatColor(t)}">${fmt(v)}</td>`;
        });
        html += "</tr>";
      });
    });
    html += '</tbody></table></div>';
    html += '<div class="legend"><span><i style="background:#dcfce7"></i>该备件最低</span>' +
      '<span><i style="background:#fef3c7"></i>中等</span>' +
      '<span><i style="background:#fee2e2"></i>该备件最高</span>' +
      '<span class="muted">底色按「同一备件」横向比（每个备件行独立着色）；已排除不同价位机型/规格/颜色干扰</span></div></div>';
    return html;
  }
  function heatColor(t) {
    if (t < 0.34) return "#dcfce7";
    if (t < 0.67) return "#fef3c7";
    return "#fee2e2";
  }

  /* ---------- 视图：价格走势 ---------- */
  async function renderTrend() {
    const brands = state.brands.map(b => b.name);
    const models = await api("/api/models");
    let html = '<div class="filters">' +
      '<div class="field"><label>品牌</label><select id="t-b">' + brands.map(b => `<option>${esc(b)}</option>`).join("") + '</select></div>' +
      '<div class="field"><label>机型</label><select id="t-m"></select></div>' +
      '<div class="field"><label>备件</label><select id="t-p"></select></div>' +
      '<div class="field"><label>币种</label><span class="tag">CNY 跨国家对比</span></div></div>';
    html += '<div class="panel"><div class="section-title">📈 价格走势（按季度 · 同备件跨国家）</div>';
    html += '<div class="chart-wrap"><canvas id="trend-canvas"></canvas></div>';
    html += '<div id="trend-table"></div></div>';
    view.innerHTML = html;

    const modelSel = $("#t-m"), partSel = $("#t-p"), brandSel = $("#t-b");
    function fillModels() {
      const b = brandSel.value;
      const ms = [...new Set(models.filter(m => m.brand === b).map(m => m.model))].sort();
      modelSel.innerHTML = ms.map(m => `<option>${esc(m)}</option>`).join("");
      fillParts();
    }
    async function fillParts() {
      const b = brandSel.value, m = modelSel.value;
      const ps = await api("/api/parts?brand=" + encodeURIComponent(b) + "&model=" + encodeURIComponent(m));
      // 优先用后端归一化名（备件级口径，与比价矩阵一致）；
      // 旧后端没有该字段时回退到本地 canon()，保证兼容。
      const cats = [...new Set(ps.map(p => p.canonical || canon(p.part)))].sort();
      partSel.innerHTML = cats.map(c => `<option>${esc(c)}</option>`).join("");
      draw();
    }
    async function draw() {
      const b = brandSel.value, m = modelSel.value, p = partSel.value;
      // 取该机型全部快照（跨国家/季度/部件），按归一化备件名分组
      const all = await api("/api/part_series?brand=" + encodeURIComponent(b) + "&model=" + encodeURIComponent(m));
      const series = all.filter(r => (r.canonical || canon(r.part)) === p);
      // 按国家分组时间序列
      const byC = {};
      series.forEach(r => { (byC[r.country] = byC[r.country] || {})[r.quarter] = r.cny_price; });
      const quarters = [...new Set(series.map(r => r.quarter))].sort();
      const sdata = Object.keys(byC).sort().map((c, i) => ({
        name: c, color: SPCharts.COLORS[i % SPCharts.COLORS.length],
        data: quarters.map(q => byC[c][q] != null ? byC[c][q] : null),
      }));
      if (quarters.length) SPCharts.lineChart($("#trend-canvas"), { labels: quarters, series: sdata, yfmt: v => "¥" + Math.round(v) });
      else { const cv = $("#trend-canvas"); const ctx = cv.getContext("2d"); ctx.clearRect(0, 0, cv.width, cv.height); }
      let t = '<table><thead><tr><th>国家</th><th>季度</th><th class="num">CNY</th><th>原币</th></tr></thead><tbody>';
      series.sort((a, b) => (a.country + a.quarter).localeCompare(b.country + b.quarter));
      series.forEach(r => { t += `<tr><td>${cn(r.country)}</td><td>${esc(r.quarter)}</td><td class="num">${money(r.cny_price)}</td><td>${esc(r.price)} ${esc(r.currency)}</td></tr>`; });
      t += "</tbody></table>";
      $("#trend-table").innerHTML = t || '<div class="empty">暂无数据</div>';
    }
    brandSel.onchange = fillModels; modelSel.onchange = fillParts; partSel.onchange = draw;
    fillModels();
  }

  /* ---------- 视图：异动告警 ---------- */
  async function renderAlerts() {
    const quarters = state.quarters;
    const q = quarters.length ? quarters[quarters.length - 1] : "";
    const data = await api("/api/alerts?quarter=" + encodeURIComponent(q));
    const rows = (data.rows || []).slice().sort((a, b) => Math.abs(b.change_pct || 0) - Math.abs(a.change_pct || 0));
    let html = '<div class="filters"><div class="field"><label>季度</label><select id="a-q">' +
      quarters.map(x => `<option ${x === q ? "selected" : ""}>${x}</option>`).join("") + "</select></div>" +
      '<div class="field"><label>对比</label><span class="tag">环比 ' + esc(data.prev_quarter || "—") + "</span></div></div>";
    html += '<div class="panel"><div class="section-title">🔔 异动告警（环比变动）</div>';
    if (!rows.length) html += '<div class="empty">本季度无环比数据（需至少两个季度快照）</div>';
    else {
      html += '<div class="scroll"><table><thead><tr><th>品牌</th><th>国家</th><th>机型</th><th>备件</th><th class="num">上期</th><th class="num">本期</th><th class="num">变动</th></tr></thead><tbody>';
      rows.forEach(r => {
        const cp = r.change_pct;
        const cls = cp == null ? "gray" : (Math.abs(cp) >= 0.1 ? "red" : (cp > 0 ? "amber" : "green"));
        const txt = cp == null ? "—" : (cp > 0 ? "+" : "") + (cp * 100).toFixed(1) + "%";
        html += `<tr><td>${esc(r.brand)}</td><td>${esc(r.country)}</td><td>${esc(r.model)}</td><td>${esc(r.part)}</td>
          <td class="num">${fmt(r.prev_price, 0)}</td><td class="num">${fmt(r.price, 0)}</td>
          <td class="num"><span class="badge ${cls}">${txt}</span></td></tr>`;
      });
      html += "</tbody></table></div>";
    }
    html += "</div>";
    view.innerHTML = html;
    $("#a-q").onchange = () => renderAlerts();
  }

  /* ---------- 视图：清单浏览 ---------- */
  async function renderList() {
    const brands = state.brands.map(b => b.name);
    const countries = [...new Set(state.health.map(h => h.country))].sort();
    let html = '<div class="filters">' +
      '<div class="field"><label>品牌</label><select id="l-b"><option value="">全部</option>' + brands.map(b => `<option>${esc(b)}</option>`).join("") + '</select></div>' +
      '<div class="field"><label>国家</label><select id="l-c"><option value="">全部</option>' + countries.map(c => `<option value="${esc(c)}">${cn(c)}</option>`).join("") + '</select></div>' +
      '<div class="field"><label>搜索机型/备件</label><input id="l-s" placeholder="如 屏幕 / Find X7" /></div>' +
      '<div class="field"><label>&nbsp;</label><span class="tag" id="l-cnt"></span></div></div>';
    html += '<div class="panel"><div class="section-title">📋 备件价格清单</div><div id="l-body"></div></div>';
    view.innerHTML = html;
    const body = $("#l-body"), cnt = $("#l-cnt");
    async function load() {
      const b = $("#l-b").value, c = $("#l-c").value, s = $("#l-s").value.trim().toLowerCase();
      const rows = await api("/api/list?brand=" + encodeURIComponent(b) + "&country=" + encodeURIComponent(c));
      const f = rows.filter(r => !s || (r.model + " " + r.part).toLowerCase().includes(s));
      cnt.textContent = f.length + " 条";
      if (!f.length) { body.innerHTML = '<div class="empty">无匹配数据</div>'; return; }
      let cavHtml = "";
      const cav = brandCaveat(b);
      if (cav) cavHtml = '<div class="hint warn">⚠️ ' + esc(cav) + '</div>';
      let t = '<div class="scroll"><table><thead><tr><th>品牌</th><th>国家</th><th>机型</th><th>备件</th>' +
        '<th class="num">原币价</th><th>币种</th><th class="num">CNY</th><th>人工费（举证）</th><th>季度</th></tr></thead><tbody>';
      f.forEach(r => {
        let labor;
        if (r.has_labor_split === 1)
          labor = `<span class="lb labor-ok" title="${esc(r.labor_note || "")}">🔧 ¥${fmt(r.labor_fee)}</span>` +
            (r.labor_source_url ? ` <a class="srclink" href="${esc(r.labor_source_url)}" target="_blank" rel="noopener" title="人工费取证页">🔗证</a>` : "");
        else
          labor = '<span class="muted" title="' + esc(r.labor_note || "官网未单列人工费") + '">未单列</span>';
        const seed = (r.is_seed === 1) ? ' <sup class="seed" title="演示/种子数据">seed</sup>' : "";
        t += `<tr><td>${esc(r.brand)}</td><td>${cn(r.country)}</td><td>${esc(r.model)}</td><td>${esc(r.part)}${seed}</td>
          <td class="num">${fmt(r.price, 0)}</td><td>${esc(r.currency)}</td><td class="num">${money(r.cny_price)}</td>
          <td>${labor}</td><td>${esc(r.quarter)}</td></tr>`;
      });
      t += "</tbody></table></div>";
      body.innerHTML = cavHtml + t;
    }
    $("#l-b").onchange = $("#l-c").onchange = load;
    let to; $("#l-s").oninput = () => { clearTimeout(to); to = setTimeout(load, 250); };
    load();
  }

  /* ---------- 视图：我的关注（收藏 + 降价预警） ---------- */
  async function renderWatch() {
    const keys = getWatch();
    let html = '<div class="panel"><div class="section-title">⭐ 我的关注（降价预警）</div>';
    html += '<div class="hint">在「比价矩阵」中点击 ☆ 关注机型即可加入此处。数据保存在本机浏览器（localStorage），不会上传。下方展示各关注机型的备件均价走势与环比降幅。</div>';
    if (!keys.length) {
      html += '<div class="empty">尚未关注任何机型。前往「比价矩阵」选择机型后点击 ☆ 关注。</div></div>';
      view.innerHTML = html; return;
    }
    html += '<div class="grid watch-grid">';
    for (const key of keys) {
      const [brand, base] = key.split("|");
      let card = '<div class="card watch-card"><div class="watch-head"><b>' + esc(brand) + " " + esc(base) + '</b>' +
        '<button class="btn sm" data-unwatch="' + esc(key) + '">取消关注</button></div>';
      try {
        const h = await api("/api/price_history?brand=" + encodeURIComponent(brand) +
          "&base_model=" + encodeURIComponent(base));
        const byCQ = {};
        for (const c in (h.data || {})) for (const pt of h.data[c]) {
          (byCQ[c] = byCQ[c] || {})[pt.quarter] = (byCQ[c][pt.quarter] || []).concat(pt.cny);
        }
        const qs = [...new Set(Object.values(byCQ).flatMap(o => Object.keys(o)))].sort();
        if (qs.length >= 2) {
          const modelAvg = qs.map(q => {
            const av = [];
            for (const c in byCQ) if (byCQ[c][q]) av.push(...byCQ[c][q].filter(v => v != null));
            return av.length ? av.reduce((s, v) => s + v, 0) / av.length : null;
          });
          const prev = modelAvg[modelAvg.length - 2], latest = modelAvg[modelAvg.length - 1];
          const delta = (prev && prev > 0) ? (latest - prev) / prev : null;
          card += '<div class="watch-spark">' + sparklineSVG(modelAvg) + '</div>';
          card += '<div class="watch-meta">备件均价 ' + money(latest) +
            (delta != null ? ' · <span class="badge ' + (delta < 0 ? "green" : "red") + '">环比 ' + (delta > 0 ? "+" : "") + (delta * 100).toFixed(1) + '%</span>' : "") +
            '（' + qs[qs.length - 1] + ' vs ' + qs[qs.length - 2] + '）</div>';
        } else {
          card += '<div class="muted">数据不足（需 ≥2 季度）</div>';
        }
      } catch (e) {
        card += '<div class="muted">加载失败：' + esc(e.message) + '</div>';
      }
      card += '</div>';
      html += card;
    }
    html += "</div></div>";
    view.innerHTML = html;
    view.querySelectorAll("[data-unwatch]").forEach(b => b.onclick = () => {
      toggleWatch(b.dataset.unwatch); toast("已取消关注"); renderWatch();
    });
  }

  /* ---------- 视图：隐私与数据说明 ---------- */
  function renderPrivacy() {
    const html = '<div class="panel"><div class="section-title">🔒 隐私与数据说明</div>' +
      '<div class="legal">' +
      '<h3>我们收集什么</h3>' +
      '<p>本平台<b>不采集任何个人身份信息</b>（无账号、无登录、无设备指纹、无行为追踪）。你对「我的关注」的收藏仅保存在<b>本机浏览器 localStorage</b>，不会上传到服务器。</p>' +
      '<h3>数据从哪来</h3>' +
      '<p>价格数据来自各品牌<b>官方公开</b>的维修/备件页面<b>真实抓取</b>聚合，<b>库内不含任何种子/占位数据</b>。所有快照均记录 <code>source_url</code> 与抓取时间；比价矩阵取证列优先使用<b>本机型级官方链接</b>（🔗机型／🔗行，已实测请求确认命中本机型）：vivo/小米/OPPO 为仅返回本机型价的官方接口（<code>model_api</code>），Apple 为官方品类级定价接口 + 本机型 JSON 定位器（<code>category_api_locator</code>），三星为整表页 + 文本片段定位到本机型行（<code>model_text_fragment</code>）。<b>官网确实不提供机型级入口的</b>（如部分老旧 SKU），如实标注为 🔗牌⚠（仅品牌入口页，非本机型精确链接），<b>绝不拼造 slug</b>。</p>' +
      '<h3>关于真实取证与失败透明</h3>' +
      '<p>本平台坚持<b>不编造</b>原则：' +
      '<b>真实抓取</b>来自各品牌官方公开维修/备件页，含原币价、CNY 折算（汇率来源与实时时点见单元格 tooltip）、来源链接；' +
      '<b>抓不到就报错</b>——凡官网不可达、需交互式 consent、或选择器失效导致无法抓取的品牌/国家，<b>不会用任何占位数据填充</b>，而是写入「待修队列（运行监控页）」并标注真实失败原因（如 网络不可达 / 需代理 / 页面改版），供在可访问环境补跑。</p>' +
      '<p><b>人工费举证原则：</b>仅当官网<b>明确单列</b>人工费时（如小米《保外人工指导价（元）》列），平台才记录真实金额并在单元格以 🔧 标注、附官网原文说明与取证链接；' +
      '若官网<b>仅给总价</b>，平台显式标注「未单列人工费」（※），<b>绝不编造</b>人工费金额。</p>' +
      '<h3>口径与免责</h3>' +
      '<p>跨国比价已尽量统一为 CNY 并区分「含税/未含税」；但各国税费、汇率、区域定价不同，<b>到手价请以「跨境到手估算」结合实际运费与关税计算</b>。本平台仅供研究参考，不构成购买或维修建议。</p>' +
      '</div></div>';
    view.innerHTML = html;
  }

  /* ---------- 视图：运行监控 ---------- */
  async function renderMonitor() {
    const [health, runs, anomalies] = await Promise.all([api("/api/health"), api("/api/runs"), api("/api/anomalies")]);
    let html = '<div class="panel"><div class="section-title">🩺 抓取健康状态</div><div class="scroll"><table><thead><tr><th>品牌</th><th>国家</th><th>季度</th><th>状态</th><th class="num">价行数</th><th>异常原因</th><th>待修</th></tr></thead><tbody>';
    (health || []).forEach(r => {
      html += `<tr><td>${esc(r.brand)}</td><td>${cn(r.country)}</td><td>${esc(r.quarter)}</td>
        <td>${statusBadge(r.status)}</td><td class="num">${r.rows_written || 0}</td>
        <td class="muted">${esc(r.anomaly_reason || "")}</td>
        <td>${r.open_issues ? '<span class="badge red">' + r.open_issues + "</span>" : "—"}</td></tr>`;
    });
    html += "</tbody></table></div></div>";

    html += '<div class="panel"><div class="section-title">📜 运行日志（最近 200）</div><div class="scroll"><table><thead><tr><th>时间</th><th>品牌/国家</th><th>状态</th><th class="num">行数</th><th>错误</th></tr></thead><tbody>';
    (runs || []).forEach(r => {
      html += `<tr><td class="muted">${esc(r.finished_at)}</td><td>${esc(r.brand)}/${cn(r.country)}</td>
        <td>${statusBadge(r.status)}</td><td class="num">${r.rows_written || 0}</td>
        <td class="muted">${esc((r.error_text || "").slice(0, 80))}</td></tr>`;
    });
    html += "</tbody></table></div></div>";

    html += '<div class="panel"><div class="section-title">🛠 待修队列（自愈 Agent 自动化每 6h 巡检）</div>';
    if (!anomalies.length) html += '<div class="empty">队列为空 ✅</div>';
    else {
      html += '<div class="scroll"><table><thead><tr><th>品牌/国家</th><th>问题</th><th>发现</th><th>状态</th></tr></thead><tbody>';
      anomalies.forEach(a => {
        html += `<tr><td><span class="badge red">${esc(a.brand)}/${cn(a.country)}</span></td>
          <td>${esc(a.issue_summary)}</td><td class="muted">${esc(a.detected_at)}</td>
          <td><span class="badge amber">open</span></td></tr>`;
      });
      html += "</tbody></table></div>";
    }
    html += "</div>";
    view.innerHTML = html;
  }

  /* ---------- 路由 ---------- */
  const VIEWS = {
    overview: { t: "总览", s: "平台运行概览与抓取覆盖", fn: renderOverview },
    matrix: { t: "比价矩阵", s: "友商备件 CNY 比价热力矩阵", fn: renderMatrix },
    trend: { t: "价格走势", s: "单备件跨季度 / 跨国家走势", fn: renderTrend },
    alerts: { t: "异动告警", s: "环比上季度的价格异动", fn: renderAlerts },
    list: { t: "清单浏览", s: "全部备件价格明细检索", fn: renderList },
    watch: { t: "我的关注", s: "收藏机型与降价预警", fn: renderWatch },
    privacy: { t: "隐私与数据", s: "数据来源与隐私说明", fn: renderPrivacy },
    monitor: { t: "运行监控", s: "抓取健康、日志与待修队列", fn: renderMonitor },
  };
  async function route() {
    const key = (location.hash || "#overview").slice(1);
    const v = VIEWS[key] || VIEWS.overview;
    $("#view-title").textContent = v.t;
    $("#view-sub").textContent = v.s;
    document.querySelectorAll(".nav-item").forEach(a => a.classList.toggle("active", a.getAttribute("href") === "#" + key));
    view.innerHTML = '<div class="empty">加载中…</div>';
    try { await v.fn(); }
    catch (e) { view.innerHTML = '<div class="empty">加载失败：' + esc(e.message) + '</div>'; }
  }

  async function boot() {
    try { await loadCore(); }
    catch (e) { setConn(false, e.message); toast("后端未连接：" + e.message); }
    window.addEventListener("hashchange", route);
    $("#btn-refresh").onclick = async () => { await loadCore().then(() => route()).catch(e => setConn(false, e.message)); toast("已刷新"); };
    const retryBtn = $("#btn-retry-conn");
    if (retryBtn) retryBtn.onclick = async () => { await loadCore().then(() => route()).catch(e => setConn(false, e.message)); };
    const crawlBtn = $("#btn-crawl");
    if (crawlBtn) crawlBtn.onclick = async () => {
      const brand = $("#crawl-brand").value || "";
      const country = $("#crawl-country").value || "";
      const token = $("#crawl-token").value.trim();
      const force = $("#crawl-force").checked;
      if (!token) { toast("请先输入抓取 token"); $("#crawl-token").focus(); return; }
      crawlBtn.disabled = true;
      try {
        const res = await triggerCrawl(brand, country, token, force);
        let msg = "已派发抓取任务：" + res.job_id + "（" + res.brand + "/" + res.country + "）";
        if (force) msg += "；强制重抓（忽略本季断点）";
        if (res.kb_sync) {
          if (res.kb_sync.conflicts && res.kb_sync.conflicts.length)
            msg += "；⚠️ KB 冲突未覆盖 " + res.kb_sync.conflicts.length + " 个（skill 较新）";
          else if (res.kb_sync.deployed)
            msg += "；已同步 KB " + res.kb_sync.deployed + " 个文件到运行时";
        }
        toast(msg);
        pollCrawl(res.job_id, token);
      } catch (e) { toast("抓取派发失败：" + e.message); }
      finally { crawlBtn.disabled = false; }
    };
    setInterval(() => { $("#db-clock").textContent = new Date().toLocaleString("zh-CN"); }, 1000);
    route();
  }
  boot();
})();
