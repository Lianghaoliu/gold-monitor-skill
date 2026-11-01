---
name: gold-monitor
description: 监控国际金价与工行积存金，计算金债比六档估值、MA5/MA60 技术面、SPDR/VIX/内外盘溢价三项市场情绪，并在触及支撑阻力、金债比边界、均线交叉时通过飞书推送带操作建议的卡片。当用户询问金价、积存金买卖点、金债比、黄金技术面或要设置/调整黄金提醒时使用。
---

# 黄金 / 积存金监控与提醒

一套零维护的黄金监控：抓数 → 估值 → 技术面 → 情绪面 → 飞书卡片。

## 何时使用

- 用户问"金价现在怎么样 / 该不该买积存金 / 金债比到哪了"
- 用户要设置或调整金价提醒、支撑阻力、推送渠道
- 用户要看技术面（MA5/MA60、布林带）或情绪面（ETF 持仓、VIX、内外盘溢价）

## 快速开始

```bash
# 1. 配置
cp references/config.example.json scripts/gold_config.json
export GOLD_WEBHOOK_URL="https://open.feishu.cn/open-apis/bot/v2/hook/xxxx"

# 2. 看当前快照（不发消息）
python scripts/gold_monitor.py --brief

# 3. 演练（打印卡片但不发送、不写状态）
python scripts/gold_monitor.py --dry-run

# 4. 自测（纯离线，不需要网络）
python scripts/gold_monitor.py --selftest

# 5. 正式运行（工作日 9:00–21:00 之外会自动跳过）
python scripts/gold_monitor.py
```

## 数据口径（重要，决定结论对不对）

| 指标 | 来源 | 口径 |
|---|---|---|
| 国际金价 | api.gold-api.com | 美元/盎司，实时 |
| 积存金参考价 | 上金所 Au99.99（东方财富 118.AU9999 主 / 上金所官网日线备） | 元/克；超 24 小时未更新则不用于触发，退回国际金折算并标注「折算」 |
| 10Y 美债 | Treasury.gov 主 / FRED DGS10 备 | %；金债比 = 金价 ÷ (10Y% × 1000) |
| 沪金日线 | 新浪 AU0 主 / 东方财富 113.aum 备 | 元/克，用于 MA5/MA60、布林带 |
| SPDR 持仓 | api.spdrgoldshares.com 历史归档 XLSX | 吨，T+1 |
| VIX | CBOE CSV 主 / Yahoo 备 | 仅在美股时段发布，北京时间拿到的是上一交易日收盘 |
| 内外盘溢价 | 上金所 Au99.99 vs 国际金折算 | %，两边行情时间戳不同，超 24 小时标过期且不参与判定 |

**必须守住的规矩：**

1. **积存金不能拿国际金折算价当基准。** 国内有 1–3% 的溢价，直接用会系统性低估，买卖点全错。
2. **不同日期的价格不能相减。** 拿 10/30 的收盘减 11/01 的现货，会凭空算出几个点的假溢价。
3. **过期数据不投票。** SPDR 超 5 天、VIX 超 4 天、上金所行情超 24 小时，一律剔除并在简报里写明原因，不许当成中性。
4. **取不到就说取不到。** 情绪三项全失败时，卡片写「情绪数据缺失，按计划执行」，绝不编造倾向。

## 输出

### 控制台简报 `--brief`

数据快照 → 金债比六档表 → 宏观与情绪（原始数字 + 数据日期 + 过期提示）→ 技术面。

### 飞书卡片

只在触发时推送，红色=买入类，绿色=卖出类：

- 积存金 ≤ 支撑① → 买入提醒
- 积存金 ≥ 阻力① → 卖出提醒
- 金债比 ≤ 0.6 / ≥ 1.23 → 低估 / 高估提醒
- MA5 上穿 / 下穿 MA60（日线收盘确认）→ 金叉 / 死叉提醒

卡片底部固定一行「操作」，情绪压成一句短语嵌在括号里，例如：

```
操作：831–848 挂单买入首笔，≤1/3 仓（情绪偏冷、资金流出，故只试探）
```

情绪短语由三项指标打分聚合：冷/暖/中性，出现分歧记「情绪分歧」，有效指标不足 2 个记「数据不足」。

## 配置

`scripts/gold_config.json`：

- `cny_support1` / `cny_resistance1` **可以删掉**，脚本按金债比 0.8 / 1.0 档自动推导，阈值随 10Y 动态走。
- `sentiment.thresholds` 控制情绪打分门槛，`direction` 设为 `cold`/`warm`/`neutral` 可人工覆盖。
- `macro` 块是人工/AI 每日更新的定性判断，脚本只读不写。

## 状态文件

| 文件 | 作用 |
|---|---|
| `gold_tech_state.json` | 记录最近一次已提醒的均线交叉，防止重复推送 |
| `gold_sent_state.json` | SPDR/VIX 最近成功值，抓取失败时降级使用 |
| `gold_monitor.log` | 运行日志 |

`--dry-run` **不会**写 `gold_tech_state.json`，否则演练会把真实交叉信号吃掉。

## 环境变量

| 变量 | 说明 |
|---|---|
| `GOLD_WEBHOOK_URL` | 必填，飞书自定义机器人 Webhook |
| `GOLD_CONFIG` | 可选，指定其他配置文件路径 |

## 依赖

```bash
pip install requests openpyxl
```

`requests` 用于 SPDR 接口（该域名证书链在部分 Windows 环境下会让 urllib 抛 `FileNotFoundError`），`openpyxl` 用于解析 XLSX。
