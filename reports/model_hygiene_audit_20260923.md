# 全品牌数据体检报告（机型卫生 × 覆盖缺口）

日期：2026-09-23　｜　触发：OPPO 机型行污染修正后的横向排查

---

## 一、为什么要做这次体检

OPPO 的教训是：**清退废弃端点的"价格行"时，漏清了它创建的"机型行"**，
残留 1,123 个污染机型条目，直到今天才被发现（详见
`reports/oppo_coverage_finding_20260923.md`）。

既然这是一个"上次清退的通病"，就应该横向排查其他品牌有没有同样的问题。
同时顺带核对了"SCOPE 配了但库内没数据"的组合到底是故障还是已知限制。

---

## 二、结论 1：机型行污染**仅 OPPO 存在**，其他品牌干净

### 判据

对每个品牌的**非CN区域**机型名，检查是否含**中文特有词**
（限定版 / 活力版 / 至尊版 / 智能电视 / 手环 / 兰博基尼 / 火星探索 / 柯南 / 超级闪充 …）。

### 结果

| 品牌 | 非CN机型数 | 含中文特有词 |
|---|---|---|
| apple | 846 | **0** |
| oppo | 466 | **0**（已修复） |
| samsung | 656 | **0** |
| vivo | 331 | **0** |
| xiaomi | 0（仅 cn） | **0** |

### ⚠️ 一个必须记录的判据陷阱

apple/jp 有 **27 台**机型名含汉字（`Apple Watch SE (第 2 世代) GPS 40mm`、
`iPad (第 10 世代) Wi-Fi`），**初看像中文污染，实际是日文**：

- 来源全部是 `support.apple.com/ja-jp/`（日文语区）；
- 拆解汉字成分后，27 台剩余的汉字**只有「世代」**（27/27）——
  这是 Apple 日本官网的标准写法（日语「第10世代」），不是中文。

**教训**：用正则 `[\u4e00-\u9fff]` 直接判"中文名"会**误伤日文汉字**。
必须用"中文特有词"表，或结合 `source_url` 的语区前缀一起判断。

---

## 三、结论 2：10 个覆盖缺口**全部是已知无源**，没有一个是抓取故障

`run.py` 的 SCOPE 配置了 35 个 brand×country 组合，库内缺 10 个。
逐个核对 KB 的 recipe `status` 后：

| 品牌 | 缺失区域 | KB status | 原因（KB note） |
|---|---|---|---|
| xiaomi | de / tr / my / jp / ae / mx | `unavailable` | 小米备件价接口 `api2.service.order.mi.com` **仅中国可用**，非 cn 无对应接口 |
| vivo | de | **`unverified`** | 尚未验证有无官方价源（唯一待探索项） |
| vivo | jp | `unavailable` | — |
| samsung | mx | `unavailable` | `samsung_api` 仅实现 de/my/ae/tr/jp/cn |
| apple | tr | `unavailable` | Apple 未提供土耳其语区维修价页 |

**关键结论：没有任何一个是"标了 verified 却抓不到"的真 bug。**

`run.py` 的处理也是正确的 —— 读到 `status=unavailable` 就如实跳过、不发请求、不写数据：

```
[region] ▶ xiaomi/de
[skip] xiaomi/de 状态=unavailable（需真机/代理或官网无工具）
[region] ■ xiaomi/de 耗时 0.3s
```

---

## 四、本次改进：让"已知无源"在界面上可见

### 问题

比价矩阵里，小米在 5 个国家（de/tr/my/jp/ae/mx）**全是空白列**。
若不说明，用户会把"**官方没有这项数据**"误读成"**我们抓取失败**"
—— 这是数据可信度问题，而不只是显示问题。

### 改动

1. **`api/server.py`**：新增 `_kb_region_status()`；`/api/brands` 每条品牌附带
   `source_unavailable: [{country, status, note}]`。
   （note 在 KB 里有 3 个可能层级，需逐级回退：apple 在 recipe 级、
   xiaomi 在 `query.api` 级 —— 实测各品牌写法不统一。）

2. **`web/app.js`**：新增 `brandUnavailable()`，在矩阵说明区渲染
   > ℹ️ **xiaomi** 在以下区域**官方不提供**备件价询价：德国（官方未提供）、
   > 土耳其（官方未提供）… 这些列留白是**官方无此数据**，不是抓取失败。

3. **`tools/verify_oppo_matrix.py`**：新增 `kb_unavailable()` 与断言 ——
   若该品牌存在已知无源区域，页面**必须**渲染该说明横幅。

### 验证

```
$ python tools/verify_oppo_matrix.py --brand xiaomi --model "Xiaomi MIX Fold 2 玄夜黑"
结果: {'tableRendered': True, 'cellCount': 45, 'refCellCount': 0,
       'refBadgeCount': 0, 'hasRefText': False, 'unavailableNoteRendered': True}

[PASS] 矩阵渲染正常（45 个价格单元格）；旧端点污染机型残留 0 台；
       参考价单元格/徽标 0 个；已渲染『官方不提供备件价』区域说明
```

---

## 附录：品牌 × 区域覆盖矩阵（2026Q3）

格式：`机型数(本季有价机型数)`；`—` = 该品牌在该区域无任何机型条目。

| 品牌 | ae | cn | de | jp | mx | my | tr |
|---|---|---|---|---|---|---|---|
| apple | 170(170) | 139(139) | 170(170) | 166(166) | 170(170) | 170(170) | **—** |
| oppo | 120(120) | 574(574) | 25(25) | 26(26) | 64(64) | 171(171) | 60(60) |
| samsung | 55(55) | 9(9) | 366(366) | 71(71) | **—** | 80(80) | 84(84) |
| vivo | 90(86) | 428(428) | **—** | **—** | 36(36) | 150(141) | 55(54) |
| xiaomi | **—** | 2510(2375) | **—** | **—** | **—** | **—** | **—** |

（`—` 全部为上文所列的已知无源区域；vivo/ae、vivo/my、vivo/tr、xiaomi/cn 的
"有价 < 机型数"是"机型在该区域目录中但官方未定价"，属正常。）

---

## 待探索

- **vivo/de**：唯一 `unverified` 的区域 —— 尚未验证 vivo 德国官网是否提供备件价询价，
  值得优先探测（成本低、可能带来一个新区域的覆盖）。
