# vivo/de 备件价源核查报告（2026-09-23）

> **结论**：vivo 德国站**有**备件价查询工具，但**官方未给该区配置任何价格数据**。
> `vivo/de` 由 `unverified` 结案为 `unavailable`（附完整证据链）。
> 这是全库**最后一个**未决项——处理后 KB 中不再有 `unverified`。

## 1. 为什么查这个

2026-09-23 全品牌覆盖体检（见 `model_hygiene_audit_20260923.md`）发现 10 个
「SCOPE 配了、库里却没数据」的 brand×country 组合。逐个核对 KB recipe status 后，
9 个是人工研判过的 `unavailable`（如小米备件价接口仅中国可用、Apple 无土耳其语维修价页），
**只有 `vivo/de` 是 `unverified`** —— 配了但没验过，是唯一值得投入的探索项。

## 2. 排查过程与证据

### 2.1 页面存在，且机型解析完整（不是 jp 那种 404）

| 项 | 结果 |
|---|---|
| `https://www.vivo.com/de/support/accessory` | HTTP 200，title `vivo Deutschland` |
| SSR 机型数 | **30 台**（V70 FE / V70 / V50 Lite 5G / V50 Lite / V50e / V50 / X300 Ultra …） |
| 我们的解析正则 | 覆盖页面全部 30 个 `data-id`，**无遗漏** |
| 页面 `globalVar.regionId` | `'de'`（locale `de-DE`）——区域码正确，非拼错 |

对比：`vivo/jp` 是页面 404（无此工具），de 与它**性质不同**。

### 2.2 全 30 台逐台取价：30/30 均为「成功但无价表」

对页面列出的全部 30 台并发实测官方接口 `POST /de/support/queryPriceByProductId`：

```
状态分布: {'noprice': 30}
```

响应体（以 V70 FE 为例）：

```json
{"success":true,"code":null,"msg":"查询成功!",
 "data":{"sparePartVO":{"decimalPoint":null,"thousands":null,
   "spareCompany":null,"spareCurrencyPosition":null,
   "decimalPlaces":0,"sparePartsVoList":null,"queryByCrm":false}}}
```

关键特征：**币种相关字段（`spareCompany`/`spareCurrencyPosition`/`decimalPoint`/`thousands`）
全部为 `null`** —— 这是「该区域未配置价格数据」的典型形态，而非请求出错。

### 2.3 地面真相：真浏览器点选，站点自己发出的请求也返回空

用无头 Chromium 打开 de 备件页、关掉 cookie 浮层、展开下拉并点选 **V70 FE (data-id=456)**，
同时拦截页面自身的网络请求：

```
[200] POST https://www.vivo.com/de/support/queryPriceByProductId
      POST: id=456
      RESP: {"success":true,...,"sparePartsVoList":null,...}
选中显示 = 'V70 FE'
含价格数字的节点 = []
DOM 长度 = 54326
```

→ **站点自己**发出请求也拿不到价表，页面渲染 **0 个价格节点**，只有空状态占位图。
截图：`evidence/vivo_de_no_price.png`

### 2.4 对照组：同端点同参数，my 站正常出价

对 `vivo/my` 跑同一条探针路径（相同端点形式、相同参数名）：

```
[200] POST https://www.vivo.com/my/support/queryPriceByProductId
      POST: id=1823
      RESP: {"success":true,...,"spareCompany":"RM","sparePartsVoList":[{"name":"Adapter","price":"130.00",...}]}
含价格数字的节点 = ['RM130.00','RM95.00','RM205.00','RM50.00',...]
```

→ 证明**我们的取价链路与官网同源同参、无实现缺陷**；de 的空是数据侧的空，不是抓取侧的空。
截图：`evidence/vivo_my_has_price_control.png`

### 2.5 de 站不存在其它价格入口

| 候选路径 | 结果 |
|---|---|
| `/de/support/repairPrice`、`/repair-price`、`/servicePrice`、`/price` | 全部 **HTTP 404** |
| `/de/support` 首页 | 无 `Preis`（价格）字样；`Ersatzteil` 仅 1 次、`Reparatur` 5 次 |

## 3. 结论

vivo 德国站提供备件价查询 UI（说明文案承诺「显示部分备件参考价」），
但**该区未配置价格数据**：选出机型后官方接口如实返回空。
这与小米非 cn 区域（接口仅中国可用）属**同类**——官方如实无数据，非抓取失败。

运行时按 `skipped` 记录，不再反复尝试。

## 4. 顺带修掉的一个真实缺陷：说明文字被前端「吞掉」

结案过程中发现：KB 里说明文字的**键名不统一**——

- recipe 级一律用**复数** `notes`（各品牌主流写法，每家 7 条）
- 只有 query / query.api 级用**单数** `note`（apple / oppo / samsung / xiaomi / vivo 部分条目）

而 `api/server.py::_kb_region_status()` 原先**只认单数** `note`，导致：

```
旧逻辑下说明为空的区域（界面只剩无理由的空白横幅）: 2
   - vivo/de
   - vivo/jp
新逻辑下说明为空的区域: 0
```

即：vivo/de 与 vivo/jp 的说明在页面上**一直是空的**，
"官方无此数据"退化成一句没有理由的空白提示——正是本次体检要消除的误导。

**修复**：读取端兼容 `note` / `notes` × 三个层级；前端把原因作为悬停提示呈现（`.no-src`）。

同时修正了 KB 中**与实际行为不符**的过时描述：
xiaomi 6 个非 cn 区域写「运行时以 failed 记录标记」、samsung/mx 写「→RuntimeError→failed 记录」，
但两者 status 均为 `unavailable`，运行时在 mode 分派**之前**就短路跳过（记 `skipped`，不建工单）。
已改为与代码一致的表述（samsung 那条保留为历史变更记录，因其明确标注了"原 unverified 导致…（误报）"）。

## 5. 改动清单

| 文件 | 改动 |
|---|---|
| `references/kb/vivo.json` | de：`unverified` → `unavailable`，写入 6 条判据的完整证据链；evidence 补 `probe_note` |
| `api/server.py` | `_kb_region_status()` 兼容 `note`/`notes` 三个层级 |
| `web/app.js` | 「官方不提供」区域名改为可悬停，显示 KB 记录的原因 |
| `web/styles.css` | 新增 `.no-src`（虚线下划线 + `cursor:help`） |
| `references/kb/xiaomi.json` | 6 条 recipe notes + 6 条 api note 改为与运行时一致 |
| `references/kb/samsung.json` | api note 改为与运行时一致 |
| `tools/verify_oppo_matrix.py` | 新增 `api_note_fails()`（端到端断言说明非空）与 `.no-src` 逐条断言 |

## 6. 验证

```
$ python tools/verify_oppo_matrix.py --brand vivo --model "X300 Pro"
[kb] API 暴露「官方不提供备件价」区域 10 个，说明文字均非空
结果: {'tableRendered': True, 'cellCount': 115, 'refCellCount': 0, 'refBadgeCount': 0,
       'hasRefText': False, 'unavailableNoteRendered': True,
       'noSrcTitles': [{'cc': '德国（官方未提供）', 'tip': '【2026-09-23 实测转 unavailable·证据链完整】…'},
                       {'cc': '日本（官方未提供）', 'tip': '【2026-09-22 实测转 unavailable】…'}]}
[PASS]
```

德国与日本**两条说明均非空**——日本这条此前一直是空的，本次修复后可见。

## 7. 复现方式

只读探针（不入仓，`_probe*.py` 已被 .gitignore 覆盖）：

```bash
python -u tools/_probe_vivo_de2.py de    # 单区域：拦截站点自身请求 + DOM 价格节点 + 截图
python -u tools/_probe_vivo_de2.py de my # 连带对照组
```

## 8. 附：证据截图

| 文件 | 内容 |
|---|---|
| `evidence/vivo_de_no_price.png` | de 站选中 V70 FE 后：空状态占位，**无价格表** |
| `evidence/vivo_my_has_price_control.png` | 对照组 my 站 T1x：完整价格表（RM130.00 / RM205.00 …） |
| `evidence/vivo_de_dropdown_v1.png` | de 站机型下拉（V-Serie / X-Serie / Y-Serie 分组，共 30 台） |
