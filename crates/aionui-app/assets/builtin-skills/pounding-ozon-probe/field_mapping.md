# 字段映射参考

1688 / Ozon 数据 → 信封字段的映射规则。

## 1688 → 信封字段

| 1688 字段 | 信封字段 | 转换规则 |
|-----------|---------|---------|
| 商品 ID | `draft.item_id` | 直传 |
| 商品标题 | `draft.title` | 中文原文，Worker 自动翻译俄语 |
| 商品图片 | `draft.images[]` | URL 数组，优先 ww1200 质量 |
| SKU 价格 | `draft.purchase_cost` | 代表变体价格 + 国内运费(freightCny) |
| 重量 | `draft.weight` | **克（int）**。若 1688 返回 kg，Skill 自动 ×1000 |
| 尺寸 | `draft.dimensions` | **mm（int）**。若 1688 返回 cm，Skill 自动 ×10 |
| 属性 | `draft.attributes` | dict[中文属性名→值] |
| 供应商 | `draft.supplier` | 供应商名称，Worker 填充制造商属性 |
| SKU | `draft.sku_id` | 1688 SKU ID |
| 详情页 URL | `draft.purchase_url` | 直传 |

## Ozon → 信封字段（跟卖时）

| Ozon 字段 | 信封字段 | 说明 |
|-----------|---------|------|
| product_id | `draft.ozon_product_id` | 跟卖目标产品 ID |
| 竞品图片 | `draft.images[]` | 用于图搜 1688 同款 |
| 类目 | `draft.ozon_category` | {description_category_id, type_id} |

## 单位转换规则

| 字段 | 1688 原始单位 | 信封单位 | 转换 |
|------|-------------|---------|------|
| weight | kg 或 g | **克（int）** | < 10g 且尺寸 > 50mm → 自动 ×1000 |
| dimensions | cm | **mm（int）** | max_dim < 200 → 自动 ×10 |
| price | CNY | **CNY（float）** | 直传，Worker 负责定价 |

## 图片顺序规范

Worker 按以下顺序排列图片：

1. `detail` — 详情图
2. `scene_1/2/3` — 场景图
3. `comparison` — 对比图
4. `social_proof` — 社交证明图
5. `multi_angle` — 多角度图（倒数第二）
6. `white_bg` — 白底图（最后）

`primary_image` = `main_image`（营销主图，单独指定）

## 定价公式

```
售价 = 总成本 × (1 + 利润率) / (1 - 佣金率)
总成本 = 采购成本(CNY) + 物流费率(按重量) + 生图费用
```

- CNY 店铺不使用 fx_buffer（无汇率风险）
- 物流费率从 `logistics_rates` 表查询（按重量区间）
- 兜底物流费率：0.05 CNY/g
