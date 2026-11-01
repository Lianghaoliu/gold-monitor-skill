# gold-monitor-skill

一套零维护的黄金 / 工商银行积存金监控：抓数 → 估值 → 技术面 → 情绪面 → 飞书卡片。

[![skill](https://img.shields.io/badge/Claude%20Code-skill-blue)](#安装为-claude-code-skill)

## 它能做什么

每 30 分钟检查一次，在**真正触发**时才推消息，平时不发：

| 触发条件 | 推送 |
|---|---|
| 积存金 ≤ 支撑① | 买入提醒（红） |
| 积存金 ≥ 阻力① | 卖出提醒（绿） |
| 金债比 ≤ 0.6 / ≥ 1.23 | 长期低估 / 高估提醒 |
| MA5 上穿 / 下穿 MA60（日线收盘确认） | 金叉 / 死叉提醒 |

每条卡片固定带一行「操作」，把估值、技术、宏观、情绪压成一句可执行的话：

```
操作：831–848 挂单买入首笔，≤1/3 仓（情绪偏冷、资金流出，故只试探）
```

## 数据面

- **估值**：金债比 = 金价 ÷ (10Y 美债收益率% × 1000)，0.6–1.23 六档，各档价位随 10Y 动态反推
- **技术**：MA5/MA60 定趋势，布林带（20, 2）定位置，只认日线收盘
- **情绪**：SPDR 黄金 ETF 持仓（官方 XLSX）、VIX 恐慌指数（CBOE）、内外盘溢价（上金所 Au99.99 vs 国际折算）

## 三个容易踩的坑（本项目的核心）

1. **积存金不是国际金折算价。** 国内长期溢价 1–3%，直接拿 `$金价 × 汇率 ÷ 31.1` 当积存金价，买卖点会整体偏移。本项目以上金所 Au99.99 为积存金定价基准。
2. **不同日期不能相减。** 拿周五的沪金收盘减周日的国际现货，会凭空算出一个几点的假溢价。所有跨日期比较都被代码结构挡住。
3. **过期数据不许冒充有效。** SPDR 超 5 天、VIX 超 4 天、上金所行情超 24 小时一律剔除，并在简报里写明「已过期 N 天」。全部取不到时，卡片写「情绪数据缺失，按计划执行」。

## 安装

```bash
git clone https://github.com/Lianghaoliu/gold-monitor-skill.git
cd gold-monitor-skill
pip install requests openpyxl

cp references/config.example.json scripts/gold_config.json
export GOLD_WEBHOOK_URL="https://open.feishu.cn/open-apis/bot/v2/hook/你的机器人地址"
```

飞书侧：群设置 → 群机器人 → 添加「自定义机器人」，复制 Webhook。

## 使用

```bash
python scripts/gold_monitor.py --brief     # 当前快照，不发消息
python scripts/gold_monitor.py --dry-run   # 演练，打印卡片但不发送
python scripts/gold_monitor.py --selftest  # 离线自测（22 项）
python scripts/gold_monitor.py             # 正式运行
```

正式模式只在工作日 9:00–21:00 之间执行，其余时段直接退出。

## 定时运行（Windows）

任务计划程序 → 创建任务：

- 触发器：每天 09:00 开始，每 30 分钟重复，持续 12 小时
- 操作：程序 `python`，参数 `scripts\gold_monitor.py`，起始于本仓库根目录
- 把 `GOLD_WEBHOOK_URL` 写进系统环境变量（或任务里用 `setx` 提前设置）

Linux/macOS 用 cron：

```cron
*/30 9-20 * * 1-5 cd /path/to/gold-monitor-skill && GOLD_WEBHOOK_URL=... python3 scripts/gold_monitor.py
```

## 配置要点

`scripts/gold_config.json` 里 **`cny_support1` / `cny_resistance1` 可以整个删掉** —— 脚本会按金债比 0.8 / 1.0 档自动推导，阈值随 10Y 收益率动态走，这是「零维护」的关键。

`sentiment.thresholds` 用来调情绪打分门槛；把 `direction` 设成 `cold`/`warm`/`neutral` 可人工覆盖自动判断。

`macro` 块（美联储、非农、通胀）是定性判断，脚本只读不写，需要每天更新 —— 超过 1 天未更新会在简报里标「已过期 N 天」。

## 已知限制

- **VIX 在北京时间只能是上一交易日收盘**，因为指数只在美股时段发布。卡片和简报都会标日期，不假装实时。
- **周末/长假没有上金所行情**，此时溢价不可用，积存金价退回国际金折算并标「折算」。
- **`macro` 块需要人工维护**，其余全部自动。
- CBOE 和东方财富在部分网络环境会被拦截，脚本带重试和本地缓存兜底。

## 作为 Claude Code Skill 安装

把本仓库放进你的 skills 目录即可（`Skills` 会读 `SKILL.md` 的 frontmatter）：

```bash
git clone https://github.com/Lianghaoliu/gold-monitor-skill.git ~/.claude/skills/gold-monitor
```

## 推送（针对国内网络）

`push.sh` 会在推送失败时自动重试，专门对付 `github.com:443` 的间歇性超时：

```bash
./push.sh              # 推当前分支，默认重试 5 次
PUSH_RETRIES=10 ./push.sh
```

被拒绝（远程有新提交）或凭据失效时不会无谓重试，直接退出并给出下一步命令。

## License

MIT
