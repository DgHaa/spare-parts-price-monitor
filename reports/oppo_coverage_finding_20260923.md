# OPPO 非CN区域覆盖问题：根因修正、真正的解与执行结果

日期：2026-09-23　｜　状态：**已执行完成**（含数据修正、端点切换、重抓与回归验证）

---

## 一、结论（推翻了 2026-09-22/23 的两次判断）

| 此前判断 | 实际情况 |
|---|---|
| 1,123 台是"OPPO 在德国/阿联酋等地有售、但官方没公布备件价" | ❌ **全部是旧端点 `/cnw/v1/GetPartPrice` 污染的机型行残留**，不属于这些区域 |
| de 只有 10~18 台机型是"OPPO 侧的在售精选限制" | ❌ **de 的真实机型目录有 192 台**。10 台只是 `getProduct`（在售精选）的口径 |
| 解法只能是"换第三方维修商价目源" | ❌ **OPPO 自己就有更全的端点**：`/basic/v1/getProductInfo` |

**一句话**：不是"买不到价"，而是①机型表本身被污染了、②用错了机型表端点。

---

## 二、铁证

### 2.1 这 1,123 台 100% 来自被弃用的旧端点

```
1,123 台的 models.source_url 全部匹配  /cnw/v1/GetPartPrice   → 100.0%
```

该端点已在 2026-09-22 因"忽略 area 参数、把中国 CNY 价当各国本地币"被判定为污染源并加硬阻断。
**当时只清退了它的价格行（13,836 行），没有清退它创建的机型行** —— 残留至今。

### 2.2 名单内容自证是"中国市场机型表"

| 污染类型 | 台数 | 样本 |
|---|---|---|
| 被当成 OPPO 抓进来的 **一加 OnePlus** 机型 | **186** | `一加 12`、`OnePlus Watch`、`一加 9R 5G` |
| **中国专供版本命名**（海外无此版本） | **72** | `Find X2 Pro 兰博基尼版`、`Find X3 Pro 火星探索版`、`OPPO 手环 名侦探柯南限定版` |
| **中国市场产品线** | 若干 | `OPPO 智能电视 K9 43/55/65/75英寸`、`OPPO 智能电视 R1`、`OPPO 手环` |
| 名字含中文（中国命名口径） | **498** | — |

### 2.3 与真实可抓机型"零重叠"

按 **(区域 + 归一化机型名)** 严格比对：

| 区域 | 旧端点残留 | REBORN 真实已抓 | 同名重叠 |
|---|---|---|---|
| de | 196 | 10 | **0** |
| ae | 174 | 99 | **0** |
| tr | 196 | 29 | **0** |
| mx | 190 | 53 | **0** |
| my | 174 | 124 | **0** |
| jp | 193 | 26 | **0** |

两套名单**完全不重合**：REBORN 抓到的机型全用国际命名，**0 台含中文**。
这不是"同一批机型的有价/无价"，而是**两批不同的机型**。

> 因此 `f2e7e20` 里"给这 886 台补 CN 参考价"建立在错误前提上：
> 给"该区域目录里根本不存在的机型"填中国价格 —— 即使打了「参考·中国」徽标，
> 用户仍会误读为"德国有这台机、只是参考中国价"，而事实是**德国没有这台机**。

---

## 三、真正的解：`/basic/v1/getProductInfo`

### 3.1 两者口径对比

| 区域 | `getProduct`（原用） | `getProductInfo` | 倍数 |
|---|---|---|---|
| de | 10 | **192** | 19.2× |
| ae | 99 | **196** | 2.0× |
| tr | 29 | **175** | 6.0× |
| mx | 53 | **136** | 2.6× |
| my | 124 | **254** | 2.0× |
| jp | 26 | **47** | 1.8× |
| cn | 465 | **620** | 1.3× |

### 3.2 它直接可用（字段兼容 + 能取价）

`getProductInfo` 返回 `marketingModelName` / `marketingModelCode`，
**与 `getProduct` 字段名完全一致** → 下游 `getPartPriceNew` 取价链路无需改动。

实测 de `OPPO Find X9` → `屏幕/Screen Component = 215.00`，与现库真实价一致。

它还额外提供：`certifiedModels`（**CPH 全球编码**，如 `["CPH2911","OPG09","PMW110"]`）、
`productCategoryCode`（跨区域稳定：01=手机/02=穿戴/03=音频/05=配件/08=平板/22=手机配件）、
`productSeriesName`、产品图 URL。CPH 码可作为将来桥接第三方源/跨区匹配的锚点。

### 3.3 全量实测（1,000 台逐台取价，0 请求失败）

44% 的目录机型能取到价；**560 台"有码无价"** = 机型在该区域目录里确实存在、但官方未公布价
—— 这才是"官方未公布价"的**权威判定**（此前无从判断）。

---

## 四、已执行的修正与结果

### 4.1 清退污染机型行

`tools/purge_legacy_oppo_models.py`

- 判据：`source_url` 仍是旧端点 **且** 本季无任何"非参考价"快照（双保险）。
  该判据可靠的原因：`db.upsert_model` 去重键是 `(brand_id, country_code, model_key)`，
  `model_key` 默认取 `name` —— 真机型被抓到时会同名复用同一行并把 `source_url`
  覆写为 REBORN 地址；污染机型永远不会被发现，`source_url` 恒停在旧端点。
- 执行结果：删除 **models 1,123 / parts 12,095 / price_snapshots 12,095**
  （含 `f2e7e20` 写入的全部 `is_reference=1` 参考价行）。
- 复核：剩余旧端点机型 **0**、孤儿快照 **0**。

### 4.2 机型表切到 `getProductInfo`

- `references/kb/oppo.json`：7 个区域 `api.product_list` → `/basic/v1/getProductInfo`；
  新增 `category_filter_by_code` 字段位（当前为 null）。
- `crawler/run.py`：品类过滤兼容两种口径（`categoryName` / `productCategoryCode`）；
  新增 `_is_dirty_catalog_entry()` 剔除目录里的脏条目（实测 `OPPOXSend To Os`/`OPPOXSend To ES`）。
- `db.py`：修复 `captured_model_keys()` —— 该函数原先把**参考价行**也算作"本季已抓到价"，
  导致那些"仅因参考价而存在行"的机型被永久跳过、再也拿不到真实价
  （de 重抓时因此漏掉 20 台）。现加 `AND COALESCE(ps.is_reference,0)=0`。

### 4.3 覆盖提升实测结果

| 区域 | 修正前 | **修正后** | 增量 |
|---|---|---|---|
| de | 10 | **25** | **+15（2.5×）** |
| ae | 99 | **120** | +21 |
| tr | 29 | **60** | **+31（2.1×）** |
| mx | 53 | **64** | +11 |
| my | 124 | **171** | **+47** |
| jp | 26 | 26 | 0 |
| **非CN合计** | **341** | **466** | **+125（+37%）** |
| cn（参考） | 461 | **574** | +113 |

真实价行数（非CN）：**3,685 行**。全部为当地官方价（`is_reference=0`）。

### 4.4 停用 CN 参考价回退

`crawler/run.py::_apply_reference_fallback` 已停止自动调用（保留函数体并标注 DEPRECATED），
`crawler/reference_prices.py` 模块文档已记录前提修正与正确适用面
（须以 `references/catalog/oppo_<cc>.json` 收窄，**不得**再以"本季无快照"为判据）。

### 4.5 展示与文案

- `web/app.js`：矩阵留白单元格加 tooltip，说明"该区域官方未公布此项备件价
  —— 可能不在当地产品目录中，或在目录中但官方未定价。不做推算、不借他国价填充"。
- `brands.price_caveat` 更新为准确表述（旧端点污染的范围、现有数据口径、覆盖差异的真实成因）。

---

## 五、复现与回滚

```bash
# 抓取 OPPO 区域产品目录（getProductInfo），落盘 references/catalog/
python tools/oppo_catalog.py

# 预览 / 执行 旧端点污染机型清退（--apply 前自动备份 DB）
python tools/purge_legacy_oppo_models.py
python tools/purge_legacy_oppo_models.py --apply

# 用新端点重抓（KB 已指向 getProductInfo）
python crawler/run.py --brand oppo

# 比价矩阵健康度回归（UI + DB 断言）
python tools/verify_oppo_matrix.py --shot out_oppo_matrix.png
```

**回滚**：本次两步不可逆操作前均自动备份，直接从对应备份恢复即可。

| 时点 | 备份文件 |
|---|---|
| 修正前（含 12,095 参考价行 / 1,123 污染机型） | `backups/spare_parts_before_coverage_fix_20260923_111221.db` |
| 清退前 | `backups/spare_parts_before_purge_legacy_models_<ts>.db` |

**回归验证结果**：`[PASS]` —— 矩阵渲染正常（162 个价格单元格）；
旧端点污染机型残留 0 台；参考价单元格/徽标 0 个。

---

## 附：一个容易误判的点

页面上出现「一加 / OnePlus / 智能电视 / 手环 / 兰博基尼」等词**不代表污染**：

- OPPO **中国**官网的备件价页本身就包含**一加机型**与**智能电视**
  （一加在华售后服务已并入 OPPO），来源是 REBORN，属真实数据；
- 「手环」还大量出现在**小米中国**的机型名里；
- 「兰博基尼」是**小米** Redmi K70 至尊冠军版的联名款。

故"幽灵机型"的判据必须落在 **DB 层的 `source_url`**，不能用整页文本搜关键词
（本项目的回归脚本初版即因此误报，已修正）。
