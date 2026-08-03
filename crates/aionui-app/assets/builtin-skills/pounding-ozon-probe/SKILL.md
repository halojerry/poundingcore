---
name: pounding-ozon-probe
description: >
  Ozon 上架工具。当用户发送 1688 链接时直接上架，发送 Ozon 链接时直接跟卖。
  当用户说"帮我找蓝海产品""帮我选品"且没有给链接时，去 Ozon 中国站自动选品。
  支持批量上架、以图搜款。
---

# pounding-ozon-probe — 工具手册

## 1. 概述

pounding-ozon-probe 是跨境电商上架工具，覆盖从选品到上架 Ozon 的完整流程。

**你的角色**：操作员。你用以下命令完成工作。每条命令封装了完整的业务逻辑，你只需按场景选择并执行。

**所有命令在 `skill/` 目录下执行，使用 `python3.12 scripts/cli.py`。**

---

## 2. 环境准备（首次使用）

### 2.1 安装依赖

```bash
cd skill && pip3.12 install -r requirements.txt
```

### 2.2 获取凭证

| 凭证 | 用途 | 获取方式 |
|------|------|----------|
| MXOU_TOKEN | 云端 AI 服务密钥 | 自动从 `~/.pounding/config.json` 读取（pounding 桌面端用户无需手动设置）。没有则向用户索取。 |
| 1688 AK | 1688 商品搜索 | 浏览器打开 https://clawhub.1688.com 登录后复制 |
| Ozon Client ID + API Key | Ozon API | Ozon 卖家后台 → 设置 → API 密钥 |

三个凭证一次性问完用户。MXOU_TOKEN 自动读到了就跳过。

```bash
python3.12 scripts/cli.py set_token --token <MXOU_TOKEN>
python3.12 scripts/cli.py set_ak --ak <1688_AK>
python3.12 scripts/cli.py set_store --name "主店铺" --client-id <CLIENT_ID> --api-key <API_KEY>
```

### 2.3 验证配置

```bash
python3.12 scripts/cli.py check
```

全部 ✅ 后方可执行业务操作。如有 ❌，按提示修复。

### 2.4 环境要求

- Python 3.12（必须）
- Google Chrome（工具自动启动，用户无需手动打开）

---

## 3. 意图路由

**先判断用户意图，再选管线。每次操作前重新判断，不因上下文而惯性选择。**

```
用户输入
  ├─ 有 1688 URL？              → 【管线 A】1688 直接上架
  ├─ 有 Ozon URL？              → 【管线 B】Ozon 跟卖
  ├─ "有什么好跟卖的"？无 URL    → 【管线 C】Ozon 中国站发现 → 跟卖
  └─ "帮我选品上架"？无 URL      → 【管线 D】1688 搜索/图搜 → 直接上架
```

**关键规则：**
- 有 URL = 直接处理该 URL，不去别的平台搜索
- 无 URL = 根据用户意图选管线 C 或 D
- 蓝海评分只在管线 C 中使用
- 管线 C（跟卖选品）和管线 D（选品上架）要区分："跟卖"→C，"上架"→D

---

## 4. 命令参考

### 管线 A：1688 上架

**触发**：用户消息含 `1688.com` 链接，或管线 B 降级

```bash
python3.12 scripts/cli.py graph --url "https://detail.1688.com/offer/xxx.html" --store "主店铺"
```

- **输入**：1688 商品 URL、店铺名
- **输出**：JSON `{summary, envelope, submit_result}`
  - `summary`：商品摘要（标题、价格、重量、尺寸、图片数、属性数、供应商）
  - `envelope`：完整的 GraphInput 信封（发给 Worker 的数据）
  - `submit_result`：Worker 提交结果（见 §5）
- **自动完成**：CDP 抓取 1688 → 组装信封 → 提交 Worker

### 管线 B：Ozon 跟卖

**触发**：用户消息含 `ozon.ru` 链接

```bash
python3.12 scripts/cli.py follow --ozon-url "https://www.ozon.ru/product/xxx/" --store "主店铺" --auto-submit
```

- **输入**：Ozon 商品 URL、店铺名
- **输出**：JSON `{summary, envelope, submit_result}`
- **自动完成**：CDP 抓取 Ozon → 图搜 1688 同款 → 组装信封 → 提交 Worker

**降级**：Ozon 页面禁止复制（DataDome 拦截）时：
1. 用 Ozon Widget API 获取产品信息
2. 用产品图片在 1688 图搜同款
3. 走管线 A（直接上架，不走跟卖）

### 管线 C：跟卖选品（Discover v2）

**触发**：用户说"有什么好产品可以跟卖"、"帮我找可以跟卖的"（无 URL）

```bash
# ① 有关键词：搜索 → 全量采集 → 表格展示 → 交互挑选 → 批量找货源
python3.12 scripts/cli.py discover --keyword "宠物用品"

# ② 无关键词：直接打开 Ozon 中国站（highlight 页）滚动懒加载采集
python3.12 scripts/cli.py discover --max-products 30

# ③ 自动筛选规则（跳过交互）：月销量≥200 且 广告占比≤30% 且 跟卖≤20
python3.12 scripts/cli.py discover --keyword "宠物用品" --rules "monthly_sales>=200,drr<=30,seller_count<=20"

# ④ 价格区间过滤（RUB）：区间外产品标记 ⏭️价区间外，不参与挑选/运营指标查询
python3.12 scripts/cli.py discover --keyword "收纳" --min-price 300 --max-price 2000

# ⑤ 指定页面 URL 直接采集（搜索页/类目页）
python3.12 scripts/cli.py discover --url "https://www.ozon.ru/search/?text=собака"

# ⑥ 挑选 + 货源后确认提交 Worker
python3.12 scripts/cli.py discover --keyword "宠物用品" --auto-submit

# ⑦ 不查 seller.ozon.ru 运营指标（未登录卖家后台时自动降级，无需手动加）
python3.12 scripts/cli.py discover --keyword "宠物用品" --no-analytics
```

- **输入**：搜索关键词 或 Ozon 页面 URL 或 无（→ 中国站懒加载）
- **流程（v2，先采集后分析）**：
  1. **采集**：有 `--keyword` → 真实搜索页 `/search/?text=`；有 `--url` → 直接采集该页；都无 → 中国站 highlight 页滚动懒加载。结果容器限定（`.tile-root`），滚动到底部触发懒加载 + 等待渲染 + 翻页 + 去重
  2. **全量数据**：widget API（价格/标题/图/品牌/评分/评论数）+ 跟卖数/最低价 + **seller.ozon.ru 运营指标**（月销量/增长率/广告占比/上架天数——需卖家后台已登录，未登录自动降级，表格运营列显示 `—`）
  3. **表格分析挑选**：全量表格展示（含拒绝原因/状态）→ 人工按序号挑选 或 `--rules` 自动筛选 —— **此时不花 1688 配额**
  4. **批量货源**：只对选中的产品 1688 识图（CDP 图搜 → AK 图搜 → AK 关键词三级，含重试）→ 利润计算（真实重量/佣金）→ 蓝海评分 → 确认 → 提交
- **输出**：候选产品列表（全量落盘 `data/discovery/`，CSV 可导出）
- **规则字段**：`monthly_sales / gmv / drr / seller_count / margin / price / create_days / sales_growth / rating`
- **表格符号**：`✅可挑` 待分析 · `⚠️夹带?` 标题不含关键词 · `⏭️价区间外` 超价格区间 · `💰有利` 符合条件 · `⚠️利润低` 利润不足 · `❌无货源` 1688 没匹配到 · `—` 运营列无数据（卖家后台未登录）

**展示候选列表后，等用户确认再提交。不替用户选择。**

### 管线 D：选品上架

**触发**：用户说"帮我选品上架"、给关键词但没给 URL，意图是"上架"而非"跟卖"

**子路径 D1：1688 图搜**
```bash
python3.12 scripts/cli.py image_search --image "https://example.com/image.jpg"
```
- **输入**：图片 URL 或本地路径
- **输出**：JSON `{success, results: [{offer_id, title, price, image, shop_name}]}`

**子路径 D2：Ozon 选品**
```bash
python3.12 scripts/cli.py discover --keyword "宠物用品"
# 无关键词 → 直接采集中国站（highlight 页懒加载）
python3.12 scripts/cli.py discover --max-products 30
```
Discover v2 四阶段：采集（搜索/中国站懒加载）→ 全量数据（含运营指标）→ 表格挑选 → 批量 1688 货源 → 确认提交（详见管线 C）。

### 批量处理

```bash
python3.12 scripts/batch_test.py --urls-file urls.txt --submit
```

URL 文件混合 1688/Ozon 链接，自动识别管线。

---

## 5. Worker 响应处理

CLI 命令输出中的 `submit_result` 字段包含 Worker 的响应。按以下模板回复用户。

### 5.1 提交成功

Worker 返回：
```json
{"ok": true, "task_id": "550e8400-...", "message": "Task submitted to queue"}
```

回复用户：
> ✅ 任务已提交到云端处理
> - 任务 ID：`{task_id}`
> - 预计耗时：10–20 分钟（类目匹配 → AI 生图 → Ozon 上架 → 审核）
> - 流程完成后我会通知你。如有问题 Worker 会自动重试修复。

### 5.2 提交失败

| Worker 错误码 | 原因 | 回复用户 |
|--------------|------|----------|
| `TOKEN_INVALID` / `TOKEN_MISSING` | MXOU_TOKEN 无效或缺失 | "凭证无效，请重新设置 MXOU_TOKEN：`python3.12 scripts/cli.py set_token --token <你的token>`" |
| `TOKEN_DISABLED` / `TOKEN_EXPIRED` | 账户被禁用或过期 | "账户已被禁用或过期，请联系管理员。" |
| `INSUFFICIENT_BALANCE` | 余额不足 | "账户余额不足（{detail.remain_quota}），请充值后重试。" |
| `RATE_LIMITED` | 请求太频繁 | "请求太频繁，请稍后再试（每分钟限制 {limit} 次）。" |
| `INVALID_REQUEST` | 信封数据不完整 | "产品数据不完整：{message}。请检查 1688 商品页是否正常加载，或重试。" |
| `TASK_SUBMIT_FAILED` | 队列写入失败 | "任务入队失败，Worker 内部错误。请稍后重试。" |
| `SERVICE_UNAVAILABLE` | 服务不可用 | "云端服务暂时不可用，请稍后重试。" |
| `INTERNAL_ERROR` | 未知内部错误 | "Worker 内部错误：{message}。请稍后重试，如持续出现请联系技术支持。" |
| 网络错误（ConnectionError） | Worker 不可达 | "无法连接云端服务。请检查网络连接和 WORKER_URL 配置。" |
| 网络错误（Timeout） | 请求超时 | "云端服务响应超时，请稍后重试。" |

### 5.3 查询进度

用户问"进度"、"完成了没"时：

- 任务提交后处于云端异步处理中，CLI 工具不提供实时进度查询
- 告知用户：任务正在云端处理中（类目匹配 → AI 生图 → Ozon 上传 → 审核），预计 10–20 分钟
- 不要频繁调用 Worker API 轮询状态

---

## 6. 决策边界

| 操作 | 策略 | 说明 |
|------|------|------|
| `check`、`pip install`、`set_store`、`set_token`、`set_ak` | 自动执行 | 环境准备类操作，无需确认 |
| `graph`、`follow`（含 `--auto-submit`） | 自动执行 | 用户给了明确 URL，直接上架 |
| `discover` 选品后的最终提交 | 必须确认 | 展示候选列表，等用户说"提交" |
| 批量处理 | 必须确认 | 影响面大，需用户明确确认 |
| 利润率高低、候选产品优劣 | 展示不表态 | 陈列数据，不替用户判断 |

---

## 7. 错误处理

| 错误 | 回复用户 |
|------|----------|
| 1688 验证码拦截 | "1688 出现验证码，请在 Chrome 浏览器中滑动验证后按 Enter 继续。" |
| 1688 未登录 | "1688 未登录，请在 Chrome 中打开 1688.com 登录后告诉我。" |
| Ozon DataDome 拦截 | "Ozon 页面被反爬拦截，请在 Chrome 中访问一次 Ozon 后告诉我。" |
| 1688 AK 缺失 | "缺少 1688 AK。请执行：`python3.12 scripts/cli.py set_ak --ak <你的AK>`" |
| Ozon 店铺未配置 | "店铺未配置。请执行：`python3.12 scripts/cli.py set_store --name '店铺名' --client-id <ID> --api-key <KEY>`" |
| 图搜无结果 | "1688 上未找到同款产品。要不要试试用关键词搜索？" |
| Worker 返回错误 | 按 §5.2 错误码表回复用户 |

**遇到任何错误，描述问题并引导用户修复。不自己修代码、不自己探索项目结构。**

---

## 8. 常见越界行为

| 错误行为 | 后果 | 正确做法 |
|---------|------|---------|
| 自己写 Python 代码调 API | 逻辑不完整、缺错误处理 | 用 `cli.py` 命令 |
| 自己探索项目目录结构 | 浪费时间、可能改错文件 | 看本文档 |
| 给 Ozon URL 还去算蓝海评分 | 逻辑混乱 | 有 URL 直接处理 |
| 给 1688 URL 还去 Ozon 搜索 | 多余操作 | 有 URL 直接处理 |
| 替用户决定"这个利润太低不上了" | 用户失去控制权 | 展示数据让用户决定 |
| 在用户没说"提交"时就提交 Worker | 用户没确认就上架 | 等用户明确说"提交" |
| 对话长了就忘记意图路由规则 | 管线混乱 | 每次操作前重读 §3 |
| 把蓝海逻辑混入跟卖流程 | 数据错误 | 蓝海只在管线 C |

---

## 9. 参考文件

| 文件 | 用途 |
|------|------|
| `envelope_example.json` | 完整信封结构示例（单 SKU + 跟卖两种模式） |
| `field_mapping.md` | 1688/Ozon 字段 → 信封字段的映射规则 |

---

## 10. 更新与旧包升级

**自动更新（v0.18.0 起，默认开启）**：每次运行命令时，若 COS 上有新版本，
会自动备份旧文件 → 覆盖升级 → 失败自动回滚（`data/` 凭证/登录态/缓存全程保留），
升级成功后提示重启终端。

- 关闭自动更新：`export SKILL_AUTO_UPDATE=0`，退回「提示 + 手动 `skill update`」模式。
- 手动更新：`python3.12 scripts/cli.py update`

**旧包升级（v0.12.0 之前的包没有 updater，不会自动提示）**：

```bash
# 1. 从最新 GitHub Release 下载 bootstrap_update.py 到 skill 包目录
#    https://github.com/halojerry/ozon-worker/releases
# 2. 运行（会下载最新包 → sha256 校验 → 覆盖升级 → 失败回滚）
python3.12 bootstrap_update.py
```

如果运行 `graph`/`follow` 提示「未找到 scripts.cloud_probe（版本过旧）」，
按上面 bootstrap 升级即可。手动确认当前版本：`python3.12 scripts/cli.py update`
（显示「已是最新」即正常）。
