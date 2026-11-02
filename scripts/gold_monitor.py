#!/usr/bin/env python3
"""
积存金价格监控脚本 — 每30分钟执行（工作日 9:00-20:00）。
数据源: api.gold-api.com (国际金价) + open.er-api.com (汇率) + FRED DGS10 (10Y美债收益率)。
阈值来自 gold_config.json，由 Claude 每日分析更新。
触发时通过飞书 Webhook 推送卡片提醒。
金债比 = 金价 / (10Y收益率% × divisor)，0.6–1.23 为长期估值区间，动态反推金价支撑/阻力。
"""

import csv
import io
import json
import logging
import os
import re
import sys
from datetime import datetime
from urllib.request import Request, urlopen

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.environ.get("GOLD_CONFIG") or os.path.join(SCRIPT_DIR, "gold_config.json")
LOG_FILE = os.path.join(SCRIPT_DIR, "gold_monitor.log")
TECH_STATE_FILE = os.path.join(SCRIPT_DIR, "gold_tech_state.json")
SENT_STATE_FILE = os.path.join(SCRIPT_DIR, "gold_sent_state.json")
WEBHOOK_URL = os.environ.get("GOLD_WEBHOOK_URL") or ""  # 必填：飞书自定义机器人 Webhook
DRY_RUN = False  # --dry-run 时只打印卡片，不发送、不写技术状态

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    encoding="utf-8",
)


def _http_get(url, timeout=25, headers=None):
    """GET 原始字节。优先 requests —— 个别域名（如 api.spdrgoldshares.com）
    的证书链会让 urllib 直接抛 FileNotFoundError。"""
    hdrs = headers or {"User-Agent": "Mozilla/5.0"}
    try:
        import requests
        resp = requests.get(url, headers=hdrs, timeout=timeout)
        resp.raise_for_status()
        return resp.content
    except ImportError:
        pass
    except Exception as e:
        logging.warning(f"requests failed for {url}: {e}, falling back to urllib")
    req = Request(url, headers=hdrs)
    return urlopen(req, timeout=timeout).read()


def fetch_gold_price():
    req = Request("https://api.gold-api.com/price/XAU",
                  headers={"Accept": "application/json"})
    resp = urlopen(req, timeout=15)
    data = json.loads(resp.read().decode("utf-8"))
    price = float(data["price"])
    updated = data.get("updatedAtReadable", data.get("updatedAt", "unknown"))
    logging.info(f"Gold price: {price} USD/oz (updated: {updated})")
    return price, updated


def fetch_exchange_rate():
    req = Request("https://open.er-api.com/v6/latest/USD",
                  headers={"Accept": "application/json"})
    resp = urlopen(req, timeout=15)
    data = json.loads(resp.read().decode("utf-8"))
    rate = float(data["rates"]["CNY"])
    updated = data.get("time_last_update_utc", data.get("time_last_update_unix", "unknown"))
    logging.info(f"USD/CNY rate: {rate} (updated: {updated})")
    return rate, updated


def fetch_10y_yield():
    """10Y 美债收益率。主源 Treasury.gov 日度曲线，备用 FRED DGS10。
    返回 (yield_pct, date)。"""
    year = datetime.now().year
    sources = [
        ("Treasury",
         f"https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
         f"daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve"
         f"&field_tdr_date_value={year}&page&_format=csv"),
        ("FRED", "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10"),
    ]
    for name, url in sources:
        try:
            req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            text = urlopen(req, timeout=25).read().decode("utf-8", "ignore")
            if name == "Treasury":
                for row in csv.DictReader(io.StringIO(text)):
                    val = row.get("10 Yr")
                    if val:
                        return float(val), row["Date"]
            else:
                for line in reversed(text.strip().splitlines()):
                    parts = line.split(",")
                    if len(parts) < 2:
                        continue
                    try:
                        return float(parts[-1]), parts[0]
                    except ValueError:
                        continue
            logging.warning(f"10Y source {name} returned no valid row")
        except Exception as e:
            logging.warning(f"10Y source {name} failed: {e}")
    raise ValueError("all 10Y yield sources failed")


def _fetch_sina_daily(symbol="AU0", limit=400):
    """新浪沪金主力连续日线。返回升序 [(date, close), ...]。"""
    url = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/"
           f"var%20_{symbol}=/InnerFuturesNewService.getDailyKLine"
           f"?symbol={symbol}&_={int(datetime.now().timestamp())}")
    req = Request(url, headers={"User-Agent": "Mozilla/5.0",
                                "Referer": "https://finance.sina.com.cn/"})
    text = urlopen(req, timeout=20).read().decode("utf-8", "ignore")
    m = re.search(r"=\s*\((\[.*\])\)", text, re.S)
    if not m:
        raise ValueError("sina jsonp parse failed")
    rows = json.loads(m.group(1))
    bars = [(r["d"], float(r["c"])) for r in rows
            if r.get("d") and r.get("c") not in (None, "")]
    if not bars:
        raise ValueError("sina returned no daily bars")
    return bars[-limit:]


def _fetch_eastmoney_daily(series="aum", limit=400):
    """东方财富沪金主力日线（元/克）。返回升序 [(date, close), ...]。"""
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
           f"?secid=113.{series}&ut=fa5fd1943c7b386f172d6893dbfba10b"
           f"&fields1=f1,f2,f3,f4,f5,f6"
           f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58&klt=101&fqt=1"
           f"&end=20500101&lmt={limit}")
    headers = {"User-Agent": "Mozilla/5.0",
               "Referer": "https://quote.eastmoney.com/"}
    last_err = None
    for _ in range(2):
        try:
            req = Request(url, headers=headers)
            data = json.loads(urlopen(req, timeout=20).read().decode("utf-8"))
            klines = (data.get("data") or {}).get("klines") or []
            bars = []
            for line in klines:
                parts = line.split(",")
                if len(parts) < 3:
                    continue
                try:
                    bars.append((parts[0], float(parts[2])))
                except ValueError:
                    continue
            if bars:
                return bars
            last_err = ValueError("eastmoney returned no daily bars")
        except Exception as e:
            last_err = e
    raise ValueError(f"eastmoney daily fetch failed: {last_err}")


def fetch_gold_daily(series="aum", limit=400, sina_symbol="AU0"):
    """沪金日线：新浪主力连续为主，东方财富备用。"""
    errors = []
    try:
        return _fetch_sina_daily(sina_symbol, limit)
    except Exception as e:
        errors.append(f"sina: {e}")
    try:
        return _fetch_eastmoney_daily(series, limit)
    except Exception as e:
        errors.append(f"eastmoney: {e}")
    raise ValueError("; ".join(errors))


def fetch_domestic_gold():
    """上金所 Au99.99（元/克）——工行积存金报价的定价基准。

    返回 (价格, 行情时间, 涨跌幅%)。积存金实际买卖价 = 该价 ± 工行点差，
    点差不公开，所以卡片上写「积存金参考」，不冒充工行实际报价。
    """
    errors = []

    # 主源：东方财富实时报价（带行情时间戳）
    url = ("https://push2.eastmoney.com/api/qt/stock/get"
           "?secid=118.AU9999&fields=f43,f57,f58,f60,f86,f170")
    for _ in range(3):  # 代理偶发抖动，重试 3 次再换源
        try:
            raw = _http_get(url, timeout=15,
                            headers={"User-Agent": "Mozilla/5.0",
                                     "Referer": "https://quote.eastmoney.com/"})
            data = ((json.loads(raw.decode("utf-8")) or {}).get("data")) or {}
            price = data.get("f43")
            if price in (None, "-", 0):
                raise ValueError("empty quote")
            price = float(price) / 100.0
            ts = data.get("f86")
            ts_dt = datetime.fromtimestamp(ts) if ts else None
            pct = data.get("f170")
            pct = float(pct) / 100.0 if pct not in (None, "-") else None
            logging.info(f"SGE Au99.99 (eastmoney): {price} CNY/g (quote: {ts_dt})")
            return price, ts_dt, pct
        except Exception as e:
            errors.append(f"eastmoney: {e}")

    # 备源：上金所官网日线（[date, open, close, low, high]，只有收盘价）
    try:
        raw = _http_get("https://www.sge.com.cn/graph/Dailyhq?instid=Au99.99",
                        timeout=20, headers={"User-Agent": "Mozilla/5.0",
                                             "Referer": "https://www.sge.com.cn/"})
        rows = (json.loads(raw.decode("utf-8", "ignore")) or {}).get("time") or []
        date, _, close, _, _ = rows[-1]
        price = float(close)
        ts_dt = datetime.strptime(date, "%Y-%m-%d").replace(hour=15, minute=30)
        logging.info(f"SGE Au99.99 (sge.com.cn close): {price} CNY/g ({date})")
        return price, ts_dt, None
    except Exception as e:
        errors.append(f"sge: {e}")

    raise ValueError("; ".join(errors))


def fetch_vix():
    """VIX 恐慌指数。CBOE 官方 CSV 为主、Yahoo 为备，返回 (值, 日期, 来源)。

    注意：VIX 只在美股时段发布，北京时间运行时拿到的必然是上一交易日收盘，
    调用方必须把日期一起标注，不得当成实时值。
    """
    sources = [
        ("cboe", "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"),
        ("yahoo", "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX"
                  "?interval=1d&range=1mo"),
    ]
    def _parse(name, raw):
        if name == "cboe":
            text = raw.decode("utf-8-sig", "ignore")
            rows = list(csv.reader(io.StringIO(text)))
            for row in reversed(rows[1:]):
                if len(row) < 5:
                    continue
                try:
                    val = float(row[4])
                    date = datetime.strptime(row[0].strip(), "%m/%d/%Y").strftime("%Y-%m-%d")
                except ValueError:
                    continue
                if 8 <= val <= 90:  # 防 HTML 错误页被当成数字
                    return val, date, name
            raise ValueError("cboe returned no valid VIX row")
        data = json.loads(raw.decode("utf-8"))
        res = ((data.get("chart") or {}).get("result") or [])
        if not res:
            raise ValueError("yahoo empty result")
        node = res[0]
        val = (node.get("meta") or {}).get("regularMarketPrice")
        date = None
        if val is None:
            closes = (((node.get("indicators") or {}).get("quote") or [{}])[0]
                      .get("close") or [])
            stamps = node.get("timestamp") or []
            for i in range(len(closes) - 1, -1, -1):
                if closes[i] is not None:
                    val = closes[i]
                    if i < len(stamps):
                        date = datetime.utcfromtimestamp(stamps[i]).strftime("%Y-%m-%d")
                    break
        if val is None or not (8 <= float(val) <= 90):
            raise ValueError("yahoo returned no valid VIX value")
        return float(val), date or "unknown", name

    errors = []
    for name, url in sources:
        last_err = None
        for attempt in range(2):
            try:
                raw = _http_get(url, timeout=25,
                                headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"})
                return _parse(name, raw)
            except Exception as e:
                last_err = e
                logging.warning(f"VIX source {name} attempt {attempt + 1} failed: {e}")
        errors.append(f"{name}: {last_err}")
    raise ValueError("; ".join(errors))


def _spdr_date(value):
    """SPDR 表格的日期单元格 → YYYY-MM-DD。"""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    text = str(value).strip()
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def fetch_spdr():
    """SPDR Gold Shares 每日持仓。官方历史归档 XLSX，返回
    (吨数, 1日变动, 5日变动, 日期)。

    列按表头名定位而不是列号——官方改版时只降级、不误读。
    """
    import openpyxl

    url = ("https://api.spdrgoldshares.com/api/v1/historical-archive"
           "?product=gld&exchange=NYSE&lang=en")
    raw = _http_get(url, timeout=60)
    wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    sheet = None
    for name in wb.sheetnames:
        if "GLD" in name and "Historical" in name:
            sheet = wb[name]
            break
    if sheet is None:
        sheet = wb[wb.sheetnames[-1]]

    rows = [r for r in sheet.iter_rows(values_only=True) if any(c is not None for c in r)]
    header_idx = None
    for i, row in enumerate(rows):
        if any(isinstance(c, str) and "tonnes" in c.lower() for c in row if c):
            header_idx = i
            break
    if header_idx is None:
        raise ValueError("SPDR xlsx: header row with 'Tonnes' not found")

    header = [str(c).strip() if c is not None else "" for c in rows[header_idx]]

    def col(*needles):
        for idx, name in enumerate(header):
            low = name.lower()
            if any(n.lower() in low for n in needles):
                return idx
        return None

    i_date = col("Date")
    i_tonnes = col("Tonnes of Gold", "Tonnes")
    if i_date is None or i_tonnes is None:
        raise ValueError(f"SPDR xlsx: unexpected columns {header}")

    series = []
    for row in rows[header_idx + 1:]:
        if i_date >= len(row) or i_tonnes >= len(row):
            continue
        date = _spdr_date(row[i_date])
        try:
            tonnes = float(row[i_tonnes])
        except (TypeError, ValueError):
            continue
        if date and tonnes > 0:
            series.append((date, tonnes))
    if not series:
        raise ValueError("SPDR xlsx: no data rows")
    series.sort()

    tonnes, as_of = series[-1][1], series[-1][0]
    change_1d = tonnes - series[-2][1] if len(series) >= 2 else None
    change_5d = tonnes - series[-6][1] if len(series) >= 6 else None
    return tonnes, change_1d, change_5d, as_of


def _sma(values, n):
    if n <= 0 or len(values) < n:
        return None
    return sum(values[-n:]) / n


def _std(values, n):
    if n <= 1 or len(values) < n:
        return None
    window = values[-n:]
    mean = sum(window) / n
    var = sum((x - mean) ** 2 for x in window) / (n - 1)
    return var ** 0.5


def compute_tech(bars, fast=5, slow=60, m=20, k=2.0):
    """MA5/MA60 定趋势方向，布林带定价格位置。bars 升序 [(date, close)]。"""
    if len(bars) < slow + 1:
        raise ValueError(f"need >= {slow + 1} bars, got {len(bars)}")
    dates = [b[0] for b in bars]
    closes = [b[1] for b in bars]

    ma_fast = _sma(closes, fast)
    ma_slow = _sma(closes, slow)
    mid = _sma(closes, m)
    sd = _std(closes, m)
    ub = mid + k * sd
    lb = mid - k * sd
    close = closes[-1]

    trend = "多头" if ma_fast > ma_slow else "空头"
    if close <= lb:
        position = "下轨外（超卖）"
    elif close <= mid:
        position = "中轨下方（偏弱）"
    elif close <= ub:
        position = "中轨上方（偏强）"
    else:
        position = "上轨外（超买）"

    if trend == "多头":
        if close <= lb:
            combo = "多头回踩下轨：低吸机会，分批建仓"
        elif close > ub:
            combo = "多头冲上轨外：超买不追，持有或分批止盈"
        elif close >= mid:
            combo = "多头趋势、中轨上方：持有为主，回踩中轨可加"
        else:
            combo = "多头回踩中轨下方：观察企稳，守住下轨可低吸"
    else:
        if close > ub:
            combo = "空头冲上轨外：超买高抛，分批减仓"
        elif close > mid:
            combo = "空头反弹至中轨上方：高抛机会，不追多"
        elif close <= lb:
            combo = "空头弱势超卖：不抄底，等企稳/金叉"
        else:
            combo = "空头弱势下行：观望，等企稳/金叉"

    def ma_series(n):
        out = [None] * len(closes)
        total = 0.0
        for i, c in enumerate(closes):
            total += c
            if i >= n:
                total -= closes[i - n]
            if i >= n - 1:
                out[i] = total / n
        return out

    fseq = ma_series(fast)
    sseq = ma_series(slow)
    last_cross = None
    for i in range(1, len(closes)):
        a0, b0 = fseq[i - 1], sseq[i - 1]
        a1, b1 = fseq[i], sseq[i]
        if None in (a0, b0, a1, b1):
            continue
        if a0 - b0 <= 0 < a1 - b1:
            last_cross = (dates[i], "golden")
        elif a0 - b0 >= 0 > a1 - b1:
            last_cross = (dates[i], "death")

    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if dates[-1] > today:
        provisional = True
    elif dates[-1] == today:
        provisional = now.hour < 16
    else:
        provisional = False

    return {
        "as_of": dates[-1],
        "close": close,
        "ma_fast": round(ma_fast, 2),
        "ma_slow": round(ma_slow, 2),
        "boll_mid": round(mid, 2),
        "ub": round(ub, 2),
        "lb": round(lb, 2),
        "trend": trend,
        "position": position,
        "combo": combo,
        "last_cross_date": last_cross[0] if last_cross else None,
        "last_cross_type": last_cross[1] if last_cross else None,
        "cross_gap_pct": round((ma_fast - ma_slow) / close * 100, 2),
        "provisional": provisional,
        "params": {"fast": fast, "slow": slow, "m": m, "k": k},
    }


# ---------------------------------------------------------------- 市场情绪
# 三个指标：SPDR 持仓（ETF 资金流）、VIX（避险温度）、沪金内外盘溢价（国内买盘）。
# 卡片只输出一句 ≤25 字短语；原始数字只进 --brief。

SENT_FALLBACK = "情绪数据缺失，按计划执行"

_SENT_TEMP = {
    "cold": "情绪偏冷",
    "warm": "情绪偏暖",
    "neutral": "情绪中性",
    "mixed": "情绪分歧",
}

_SENT_ACTION = {
    "buy": {
        "cold": "故只试探",
        "warm": "可稍积极",
        "neutral": "按计划执行",
        "mixed": "按计划执行",
    },
    "sell": {
        "cold": "可正常止盈",
        "warm": "故暂缓减仓",
        "neutral": "按计划执行",
        "mixed": "按计划执行",
    },
}


def _age_days(date_str):
    """日期字符串距今天数，解析不了返回 None。"""
    try:
        return (datetime.now() - datetime.strptime(str(date_str), "%Y-%m-%d")).days
    except (ValueError, TypeError):
        return None


def compute_premium(domestic, domestic_ts, spot_usd, rate):
    """国内金 vs 国际金的溢价（%）。

    国内一侧用上金所 Au99.99 的最后一笔成交（积存金的定价基准），国际一侧用
    实时现货折算。两者时间戳不同，所以必须把国内行情时间一起返回并标注——
    但这里不存在「拿 10/30 的期货收盘减 11/01 的现货」那种错误，因为期货不是
    用户的交易标的，积存金跟的是 Au99.99。
    国内行情超过 24 小时未更新的，标记 stale，不再参与情绪投票。
    """
    if domestic is None or not spot_usd or not rate:
        return None
    intl_cny = float(spot_usd) * float(rate) / 31.1035
    if intl_cny <= 0:
        return None
    stale = False
    if domestic_ts:
        stale = (datetime.now() - domestic_ts).total_seconds() > 24 * 3600
    return {
        "pct": (float(domestic) / intl_cny - 1) * 100,
        "domestic": float(domestic),
        "intl": intl_cny,
        "ts": domestic_ts,
        "date": domestic_ts.strftime("%Y-%m-%d %H:%M") if domestic_ts else "unknown",
        "stale": stale,
    }


def _classify_spdr(spdr, th):
    if not spdr:
        return None
    chg = spdr.get("change_5d")
    if chg is None:
        chg = spdr.get("change_1d")
        low = th.get("spdr_1d_outflow_t", -1.0)
        high = th.get("spdr_1d_inflow_t", 1.0)
    else:
        low = th.get("spdr_5d_outflow_t", -2.0)
        high = th.get("spdr_5d_inflow_t", 2.0)
    if chg is None:
        return None
    if chg <= low:
        return "cold"
    if chg >= high:
        return "warm"
    return "neutral"


def _classify_vix(vix, th):
    if not vix or vix.get("value") is None:
        return None
    value = float(vix["value"])
    if value >= th.get("vix_high", 25.0):
        return "warm"
    if value <= th.get("vix_low", 15.0):
        return "cold"
    return "neutral"


def _classify_premium(prem, th):
    if not prem or prem.get("pct") is None:
        return None
    pct = prem["pct"]
    if pct >= th.get("premium_high_pct", 2.0):
        return "warm"
    if pct <= th.get("premium_low_pct", -0.5):
        return "cold"
    return "neutral"


def _aggregate(dirs, th):
    valid = [d for d in dirs if d in ("cold", "warm", "neutral")]
    if len(valid) < int(th.get("min_indicators", 2)):
        return "insufficient"
    if "cold" in valid and "warm" in valid:
        return "mixed"
    score = sum({"cold": -1, "warm": 1, "neutral": 0}[d] for d in valid)
    if score > 0:
        return "warm"
    if score < 0:
        return "cold"
    return "neutral"


def _flow_word(spdr_dir, vix_dir, prem_dir):
    if spdr_dir == "cold":
        return "资金流出"
    if spdr_dir == "warm":
        return "资金流入"
    if spdr_dir == "neutral":
        return "资金平稳"
    if prem_dir == "cold":
        return "沪金贴水"
    if prem_dir == "warm":
        return "溢价偏高"
    if vix_dir == "warm":
        return "避险升温"
    if vix_dir == "cold":
        return "避险降温"
    return None


def _override_direction(section, computed):
    """配置里 direction 非 auto 时人工值优先。"""
    forced = (section or {}).get("direction", "auto")
    if forced in ("cold", "warm", "neutral"):
        return forced
    return computed


def score_sentiment(th, spdr=None, vix=None, premium=None, cfg=None):
    """纯计算：三个指标数据 → 情绪结论。不发网络请求，便于自测。"""
    cfg = cfg or {}
    spdr_dir = _override_direction(cfg.get("spdr"), _classify_spdr(spdr, th))
    vix_dir = _override_direction(cfg.get("vix"), _classify_vix(vix, th))
    prem_dir = _override_direction(cfg.get("premium"), _classify_premium(premium, th))
    tendency = _aggregate([spdr_dir, vix_dir, prem_dir], th)
    return {
        "spdr": spdr, "vix": vix, "premium": premium,
        "spdr_dir": spdr_dir, "vix_dir": vix_dir, "premium_dir": prem_dir,
        "tendency": tendency,
        "flow_word": _flow_word(spdr_dir, vix_dir, prem_dir),
    }


def load_sent_state():
    try:
        with open(SENT_STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_sent_state(state):
    try:
        with open(SENT_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logging.warning(f"Failed to save sentiment state: {e}")


def compute_sentiment(cfg, domestic=None, domestic_ts=None, spot_usd=None,
                      rate=None, now=None, spdr=None, vix=None):
    """抓取 + 打分。任何一项失败都只降级，绝不抛异常。"""
    sen_cfg = (cfg or {}).get("sentiment") or {}
    th = sen_cfg.get("thresholds") or {}
    notes = []
    state = load_sent_state() if (spdr is None or vix is None) else {}

    if spdr is None:
        override = (sen_cfg.get("spdr") or {}).get("manual_override")
        if override:
            spdr = dict(override)
            spdr.setdefault("source", "manual")
        else:
            try:
                tonnes, c1, c5, as_of = fetch_spdr()
                spdr = {"tonnes": tonnes, "change_1d": c1, "change_5d": c5,
                        "as_of": as_of, "source": "spdr-api"}
            except Exception as e:
                notes.append(f"SPDR 获取失败：{e}")
    if spdr is None and state.get("spdr"):
        cached = state["spdr"]
        age = _age_days(cached.get("as_of"))
        if age is not None and age <= int(th.get("stale_days_spdr", 5)):
            spdr = dict(cached, source="cache")
            notes.append("SPDR 使用本地缓存")

    if vix is None:
        try:
            value, as_of, source = fetch_vix()
            vix = {"value": value, "as_of": as_of, "source": source}
        except Exception as e:
            notes.append(f"VIX 获取失败：{e}")
    if vix is None and state.get("vix"):
        cached = state["vix"]
        age = _age_days(cached.get("as_of"))
        if age is not None and age <= int(th.get("stale_days_vix", 4)):
            vix = dict(cached, source="cache")
            notes.append("VIX 使用本地缓存")

    if spdr and spdr.get("source") != "cache":
        state["spdr"] = {k: v for k, v in spdr.items() if k != "source"}
    if vix and vix.get("source") != "cache":
        state["vix"] = {k: v for k, v in vix.items() if k != "source"}
    if state:
        save_sent_state(state)

    premium = compute_premium(domestic, domestic_ts, spot_usd, rate)
    if premium is None:
        notes.append("内外盘溢价不可用（缺上金所报价或实时金价）")
    elif premium.get("stale"):
        notes.append(f"上金所行情截至 {premium['date']}，超过 24 小时未更新")

    # 过期数据不参与投票，且要说出过期了多久
    if spdr:
        age = _age_days(spdr.get("as_of"))
        spdr["age_days"] = age
        if age is not None and age > int(th.get("stale_days_spdr", 5)):
            spdr["stale"] = True
            notes.append(f"SPDR 数据已过期 {age} 天（{spdr.get('as_of')}）")
    if vix:
        age = _age_days(vix.get("as_of"))
        vix["age_days"] = age
        if age is not None and age > int(th.get("stale_days_vix", 4)):
            vix["stale"] = True
            notes.append(f"VIX 数据已过期 {age} 天（{vix.get('as_of')}）")

    result = score_sentiment(
        th,
        spdr=None if (spdr and spdr.get("stale")) else spdr,
        vix=None if (vix and vix.get("stale")) else vix,
        premium=None if (premium and premium.get("stale")) else premium,
        cfg=sen_cfg,
    )
    result.update({"spdr": spdr, "vix": vix, "premium": premium, "notes": notes})
    result["available"] = result["tendency"] in ("cold", "warm", "neutral", "mixed")
    result["clause_buy"] = sentiment_clause(result, "buy")
    result["clause_sell"] = sentiment_clause(result, "sell")
    return result


def sentiment_clause(sent, action="buy"):
    """情绪 → 一句 ≤25 字短语，插进卡片「操作」行。永不抛异常。"""
    try:
        if not sent or sent.get("tendency") in (None, "insufficient", "missing"):
            return SENT_FALLBACK
        temp = sent["tendency"]
        temp_word = _SENT_TEMP.get(temp)
        if not temp_word:
            return SENT_FALLBACK
        act = _SENT_ACTION.get(action, _SENT_ACTION["buy"]).get(temp, "按计划执行")
        flow = sent.get("flow_word")
        clause = f"{temp_word}、{flow}，{act}" if flow else f"{temp_word}，{act}"
        if len(clause) > 25:
            clause = f"{temp_word}，{act}"
        if len(clause) > 25:
            clause = temp_word
        return clause
    except Exception as e:
        logging.warning(f"sentiment_clause failed: {e}")
        return SENT_FALLBACK


def load_tech_state():
    try:
        with open(TECH_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_tech_state(tech):
    state = {
        "last_cross_date": tech.get("last_cross_date"),
        "last_cross_type": tech.get("last_cross_type"),
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    with open(TECH_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        logging.info(f"Config loaded: support1={cfg.get('cny_support1',cfg['support1'])}, "
                     f"resistance1={cfg.get('cny_resistance1',cfg['resistance1'])}")
        return cfg
    except Exception as e:
        logging.warning(f"Failed to load config ({e}), using defaults.")
        return None


# ---------- 工行积存金手续费与盈亏测算 ----------
# 工行现行规则（icbc.com.cn 积存金定投须知）：积存价与赎回价一致，没有点差；
# 积存、赎回各按金额的 0.5% 收手续费。手续费从金额里扣，所以买入的实际
# 成本单价是 价格÷(1-费率)，不是 价格×(1+费率)——差一点点，但别写错。
def fee_params(cfg):
    """手续费参数（小数）。缺省按工行标准费率：买卖各 0.5%、无点差。"""
    f = (cfg or {}).get("fees") or {}
    try:
        return {
            "buy": float(f.get("buy_rate_pct", 0.5)) / 100.0,
            "sell": float(f.get("sell_rate_pct", 0.5)) / 100.0,
            "spread": float(f.get("spread_cny_per_g", 0.0)),
        }
    except (TypeError, ValueError):
        logging.warning("fees 配置无法解析，按 0.5%/0.5%/0 点差处理")
        return {"buy": 0.005, "sell": 0.005, "spread": 0.0}


def buy_effective_cost(quote, cfg):
    """买入价 → 实际成本单价（含买入手续费与点差）。"""
    p = fee_params(cfg)
    if p["buy"] >= 1:
        return quote
    return quote / (1 - p["buy"]) + p["spread"]


def sell_net_price(quote, cfg):
    """赎回价 → 实际到手单价（扣赎回手续费与点差）。"""
    p = fee_params(cfg)
    return quote * (1 - p["sell"]) - p["spread"]


def breakeven_price(cost_per_g, cfg):
    """成本单价对应的保本赎回价：卖到这个价才不亏。"""
    p = fee_params(cfg)
    if p["sell"] >= 1:
        return cost_per_g
    return (cost_per_g + p["spread"]) / (1 - p["sell"])


def calc_sell(quote, cfg, grams=None, cost_per_g=None):
    """按赎回价 quote 测算到手金额、盈亏与保本价。持仓未填时只算到手价。"""
    p = fee_params(cfg)
    h = (cfg or {}).get("holding") or {}
    try:
        g = float(grams if grams is not None else (h.get("grams") or 0))
        c = float(cost_per_g if cost_per_g is not None else (h.get("cost_per_g") or 0))
    except (TypeError, ValueError):
        g = c = 0.0
    net = sell_net_price(quote, cfg)
    out = {"quote": quote, "net": net, "fee_rate_pct": p["sell"] * 100,
           "fee_px": quote * p["sell"], "spread": p["spread"],
           "grams": g, "cost_per_g": c if c > 0 else None}
    # 只知道成本、不知道克数时也要能算：每克盈亏与保本价不依赖克数
    if c > 0:
        out.update(breakeven=breakeven_price(c, cfg),
                   pnl_per_g=net - c,
                   pnl_pct=(net - c) / c * 100)
        if g > 0:
            proceeds, cost_total = net * g, c * g
            out.update(proceeds=proceeds, cost_total=cost_total,
                       pnl=proceeds - cost_total)
    return out


def below_breakeven(quote, cfg):
    """按此价卖出仍低于成本时返回亏损金额（元/克），否则 None。"""
    s = calc_sell(quote, cfg)
    be = s.get("breakeven")
    if be and quote < be - 0.05:
        return be - quote
    return None


def buy_cost_line(quote, cfg):
    """买入卡片用：实际成本单价 + 卖出保本价。"""
    try:
        p = fee_params(cfg)
        cost = buy_effective_cost(quote, cfg)
        be = breakeven_price(cost, cfg)
        return (f"成本：按 {quote:.0f} 买入实得 **{cost:.1f} 元/克**"
                f"（买入费 {p['buy'] * 100:g}%）｜卖出保本需 **{be:.0f}**")
    except Exception as e:  # 费率配置异常不能挡住提醒
        logging.warning(f"buy_cost_line failed: {e}")
        return ""


def sell_pnl_line(quote, cfg):
    """卖出卡片用：到手单价（有克数时再给总到账与总盈亏）。"""
    try:
        s = calc_sell(quote, cfg)
        line = f"到手：**{s['net']:.1f} 元/克**（扣赎回费 {s['fee_rate_pct']:g}%）"
        if s.get("pnl") is not None:
            line += (f"｜到账 **{s['proceeds']:,.0f} 元**，"
                     f"盈亏 **{s['pnl']:+,.0f} 元**")
        return line
    except Exception as e:
        logging.warning(f"sell_pnl_line failed: {e}")
        return ""


def breakeven_note(quote, cfg):
    """独立的一行保本提示：只给信息，不改阈值、不改操作建议。"""
    try:
        s = calc_sell(quote, cfg)
        be = s.get("breakeven")
        if not be:
            return ""
        px, pct = s.get("pnl_per_g"), s.get("pnl_pct")
        if px is None:
            return f"保本提示：保本价 **{be:.1f}** 元/克"
        word = "赚" if px >= 0 else "亏"
        need = f"，需再涨 **{abs(pct):.2f}%** 才回本" if px < 0 else ""
        return (f"保本提示：保本价 **{be:.1f}** 元/克｜此价卖出每克"
                f"{word} **{abs(px):.2f} 元（{abs(pct):.2f}%）**{need}")
    except Exception as e:
        logging.warning(f"breakeven_note failed: {e}")
        return ""


def print_fee_block(cny_price, cfg):
    """简报里的持仓与手续费段。"""
    p = fee_params(cfg)
    h = cfg.get("holding") or {}
    s = calc_sell(cny_price, cfg)
    print()
    print("## 持仓与手续费（工商银行积存金）")
    print(f"- 费率：买入 {p['buy'] * 100:g}%｜赎回 {p['sell'] * 100:g}%"
          f"｜点差 {p['spread']:g} 元/克（工行现行：买卖各 0.5%、无点差）")
    print(f"- 当前 {cny_price} 元/克 → 卖出到手 **{s['net']:.1f}**"
          f"｜买入实得成本 **{buy_effective_cost(cny_price, cfg):.1f}**")
    if s["cost_per_g"]:
        if s.get("pnl") is not None:
            print(f"- 持仓 {s['grams']:.2f} 克，成本 {s['cost_per_g']:.2f} 元/克"
                  f"（合计 {s['cost_total']:,.0f} 元）")
            print(f"- 按现价卖出：到账 **{s['proceeds']:,.0f} 元**，"
                  f"盈亏 **{s['pnl']:+,.0f} 元（{s['pnl_pct']:+.2f}%）**")
        else:
            print(f"- 持仓成本：{s['cost_per_g']:.2f} 元/克（未填克数，只算每克盈亏）")
            print(f"- 按现价卖出：每克 **{s['pnl_per_g']:+.2f} 元"
                  f"（{s['pnl_pct']:+.2f}%）**")
        print(f"- 保本价：**{s['breakeven']:.1f} 元/克**"
              f"（现价高于此线卖出才不亏）")
    else:
        print("- 持仓：未设置（在 gold_config.json 的 holding 里填 cost_per_g，"
              "填了 grams 还能算总盈亏）")
        print("- 或者跑 `--calc 价 --cost 成本 [--grams 克]` 临时测算")


def run_calc(price, grams=None, cost=None):
    """--calc：离线测算某个价格卖出赚还是赔。"""
    cfg = load_config() or {}
    s = calc_sell(price, cfg, grams=grams, cost_per_g=cost)
    bc = buy_effective_cost(price, cfg)
    print(f"## 积存金测算 @ 赎回价 {price:.2f} 元/克")
    print(f"- 扣除赎回手续费 {s['fee_rate_pct']:g}%"
          f"（{s['fee_px']:.2f} 元/克）"
          + (f"与点差 {s['spread']:g}" if s["spread"] else ""))
    print(f"- 到手单价：**{s['net']:.2f} 元/克**")
    if price > 0:
        print(f"- 若按此价买入：实得成本 **{bc:.2f} 元/克**，"
              f"卖出保本需 **{breakeven_price(bc, cfg):.2f}**")
    if s["cost_per_g"]:
        if s.get("pnl") is not None:
            print(f"- 持仓 {s['grams']:.2f} 克 × 成本 {s['cost_per_g']:.2f} 元/克"
                  f" = 投入 {s['cost_total']:,.2f} 元")
            print(f"- 卖出到账：**{s['proceeds']:,.2f} 元**")
            print(f"- 盈亏：**{s['pnl']:+,.2f} 元（{s['pnl_pct']:+.2f}%）**")
        else:
            print(f"- 持仓成本：{s['cost_per_g']:.2f} 元/克（未填克数）")
            print(f"- 每克盈亏：**{s['pnl_per_g']:+.2f} 元（{s['pnl_pct']:+.2f}%）**")
        print(f"- 保本价：**{s['breakeven']:.2f} 元/克**")
        gain = price - s["breakeven"]
        print(f"- 距保本：{gain:+.2f} 元/克"
              f"（{'已盈利' if gain >= 0 else '仍亏损，别卖'}）")
    else:
        print("- 未提供成本：加 `--cost 成本单价 --grams 克数` 可算盈亏")
    return 0


def send_feishu_cny(usd_price, cny_price, direction, cfg, ratio_text="", sent=None,
                    basis=None):
    """发送积存金买卖提醒卡片。sent 为 compute_sentiment 的结果，可为 None。
    basis='spot' 表示上金所报价取不到、退回国际金折算——卡片上必须标出来。"""
    now = datetime.now().strftime("%m-%d %H:%M")
    price_tag = "（折算）" if basis == "spot" else ""
    ratio_line = f"金债比：{ratio_text}\n" if ratio_text else ""
    macro = cfg.get("macro") or {}
    macro_line = (f"宏观面：{macro['bias_label']}（截至 {macro['as_of']}）\n"
                  if macro.get("bias_label") else "")
    clause = sentiment_clause(sent, direction) if sent else ""
    suffix = f"（{clause}）" if clause else ""

    if direction == "buy":
        template = "red"
        title = "买入提醒 — 积存金"
        body = (
            f"**积存金已跌破买入支撑位！**\n\n"
            f"当前积存金参考价{price_tag}：**{cny_price} 元/克**\n"
            f"国际金价：{usd_price:,.2f} 美元/盎司\n"
            f"{ratio_line}{macro_line}"
            f"买入阈值：≤ {cfg['cny_support1']} 元/克\n"
            f"{buy_cost_line(cny_price, cfg)}\n\n"
            f"操作：{cny_price}–{cfg['cny_support1']} 挂单买入首笔，≤1/3 仓{suffix}"
        )
        tag_text = f"买入信号 | {now}"
    else:
        template = "green"
        title = "卖出提醒 — 积存金"
        note = breakeven_note(cny_price, cfg)
        note_line = f"{note}\n" if note else ""
        body = (
            f"**积存金已突破卖出阻力位！**\n\n"
            f"当前积存金参考价{price_tag}：**{cny_price} 元/克**\n"
            f"国际金价：{usd_price:,.2f} 美元/盎司\n"
            f"{ratio_line}{macro_line}"
            f"卖出阈值：≥ {cfg['cny_resistance1']} 元/克\n"
            f"{sell_pnl_line(cny_price, cfg)}\n{note_line}\n"
            f"操作：{cfg['cny_resistance1']}–{cny_price} 挂单卖出首笔，≤1/3 仓{suffix}"
        )
        tag_text = f"卖出信号 | {now}"

    result = _post_card(template, title, body, tag_text)
    logging.info(f"Feishu {direction} notification sent: {result}")
    return result


def send_feishu_ratio(ratio, usd_price, cny_price, direction, cfg, levels_text="",
                      sent=None):
    """金债比触及 0.6/1.23 长期估值边界时推送。"""
    now = datetime.now().strftime("%m-%d %H:%M")
    r = cfg["gold_bond_ratio"]
    level_block = f"\n**长期估值档位**\n{levels_text}\n" if levels_text else ""
    macro = cfg.get("macro") or {}
    macro_line = (f"宏观面：{macro['bias_label']}（截至 {macro['as_of']}）\n"
                  if macro.get("bias_label") else "")
    clause = sentiment_clause(sent, "buy" if direction == "low" else "sell") if sent else ""
    suffix = f"（{clause}）" if clause else ""

    if direction == "low":
        template = "red"
        title = "金债比低估提醒"
        body = (
            f"**金债比已跌至长期强支撑区（≤ {r['support']}）**\n\n"
            f"当前金债比：**{ratio:.3f}**\n"
            f"国际金价：{usd_price} 美元/盎司（{cny_price} 元/克）\n"
            f"{macro_line}{level_block}"
            f"\n操作：低估区分批建仓首笔，≤1/3 仓{suffix}"
        )
        tag_text = f"金债比低估 | {now}"
    else:
        template = "green"
        title = "金债比高估提醒"
        body = (
            f"**金债比已升至长期强阻力区（≥ {r['resistance']}）**\n\n"
            f"当前金债比：**{ratio:.3f}**\n"
            f"国际金价：{usd_price} 美元/盎司（{cny_price} 元/克）\n"
            f"{macro_line}{sell_pnl_line(cny_price, cfg)}\n"
            f"{breakeven_note(cny_price, cfg)}\n{level_block}"
            f"\n操作：高估区分批止盈，≤1/3 仓{suffix}"
        )
        tag_text = f"金债比高估 | {now}"

    result = _post_card(template, title, body, tag_text)
    logging.info(f"Feishu ratio {direction} notification sent: {result}")
    return result


def _post_card(template, title, body, tag_text):
    if DRY_RUN:
        print(f"[DRY-RUN] {template} | {title}\n{body}\n{tag_text}\n")
        return {"code": 0, "dry_run": True}
    if not WEBHOOK_URL:
        raise RuntimeError("GOLD_WEBHOOK_URL 未设置：请先 export 飞书机器人 Webhook 地址")
    card = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": template,
                "title": {"content": title, "tag": "plain_text"},
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": body}},
                {"tag": "hr"},
                {"tag": "note", "elements": [{"tag": "plain_text", "content": tag_text}]},
            ],
        },
    }
    data = json.dumps(card).encode("utf-8")
    req = Request(WEBHOOK_URL, data=data,
                  headers={"Content-Type": "application/json; charset=utf-8"})
    resp = urlopen(req, timeout=15)
    return json.loads(resp.read().decode("utf-8"))


def send_feishu_tech(tech, cfg, ratio_text="", sent=None, cny_price=None):
    """MA5/MA60 金叉/死叉（日线收盘确认）推送。"""
    now = datetime.now().strftime("%m-%d %H:%M")
    direction = tech["last_cross_type"]
    macro = cfg.get("macro") or {}
    macro_line = (f"宏观面：{macro['bias_label']}（截至 {macro['as_of']}）\n"
                  if macro.get("bias_label") else "")
    ratio_line = f"金债比：{ratio_text}\n" if ratio_text else ""
    clause = (sentiment_clause(sent, "buy" if direction == "golden" else "sell")
              if sent else "")
    suffix = f"（{clause}）" if clause else ""
    detail = (
        f"交叉日期：{tech['last_cross_date']}（日线收盘）\n"
        f"收盘价：**{tech['close']:.2f} 元/克**\n"
        f"MA5：{tech['ma_fast']:.2f}｜MA60：{tech['ma_slow']:.2f}"
        f"（差 {tech['cross_gap_pct']:+.2f}%）\n"
        f"布林带：中轨 {tech['boll_mid']:.2f}｜上轨 {tech['ub']:.2f}｜"
        f"下轨 {tech['lb']:.2f}\n"
        f"当前位置：{tech['position']}\n"
        f"{ratio_line}{macro_line}\n"
        f"组合判断：{tech['combo']}\n"
    )

    if direction == "golden":
        template = "red"
        title = "金叉提醒 — 沪金 MA5/MA60"
        body = (f"**MA5 上穿 MA60，中期趋势转多（金叉确认）**\n\n"
                f"{detail}\n操作：回踩中轨/下轨分批建仓，跌破前低止损{suffix}")
        tag_text = f"金叉信号 | {now}"
    else:
        template = "green"
        title = "死叉提醒 — 沪金 MA5/MA60"
        fee_line = sell_pnl_line(cny_price, cfg) if cny_price else ""
        note = breakeven_note(cny_price, cfg) if cny_price else ""
        body = (f"**MA5 下穿 MA60，中期趋势转空（死叉确认）**\n\n"
                f"{detail}"
                + (f"{fee_line}\n" if fee_line else "")
                + (f"{note}\n" if note else "")
                + f"\n操作：反弹中轨/上轨分批减仓，不抄底{suffix}")
        tag_text = f"死叉信号 | {now}"

    result = _post_card(template, title, body, tag_text)
    logging.info(f"Feishu tech {direction} notification sent: {result}")
    return result


def should_run():
    """工作日 9:00-20:00 执行。"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    if now.hour < 9 or now.hour >= 21:
        return False
    return True


def skip_reason():
    """返回跳过原因，None 表示可以执行。"""
    now = datetime.now()
    if now.weekday() >= 5:
        return "周末休市"
    if now.hour < 9:
        return f"距开盘还有{9 - now.hour}小时{60 - now.minute if now.minute > 0 else 0}分"
    if now.hour >= 21:
        return "已收盘"
    return None


def print_brief(usd_price, gold_updated, rate, fx_updated, cny_price,
                ratio, ratio_support, ratio_resistance,
                yield_pct, yield_date, level_prices, cfg, tech=None,
                sent=None, domestic_ts=None):
    """输出带数据时间戳的实时快照，供日报模板使用。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"## 黄金数据快照（本地抓取时间 {now}）")
    print(f"- 国际金价：${usd_price:,.2f}/oz（源更新：{gold_updated}）")
    print(f"- USDCNY：{rate:.4f}（源更新：{fx_updated}）")
    if domestic_ts:
        print(f"- 积存金参考：{cny_price} 元/克（上金所 Au99.99，"
              f"行情 {domestic_ts.strftime('%Y-%m-%d %H:%M')}；工行实际报价含点差）")
    else:
        print(f"- 积存金参考：{cny_price} 元/克（**折算值**：上金所报价获取失败，"
              f"按国际金×汇率折算，误差约 2–3%）")
    if yield_pct is not None:
        print(f"- 10Y美债：{float(yield_pct):.2f}%（数据日期：{yield_date}，Treasury.gov/FRED）")
        print(f"- 金债比：{ratio:.4f}（= 金价÷(10Y%×1000)；区间 {ratio_support}–{ratio_resistance}）")
    else:
        print("- 10Y美债：获取失败，金债比暂缺")
    print()
    print("## 金债比档位（随10Y动态反推）")
    print("| 档位 | 含义 | 国际金价 | 积存金 |")
    print("| --- | --- | --- | --- |")
    for lr, lbl, u, c in level_prices:
        print(f"| {lr:g} | {lbl} | ${u:,.0f}/oz | {c:,.0f} 元/克 |")
    if ratio is not None and level_prices:
        nearest = min(level_prices, key=lambda x: abs(ratio - x[0]))
        print(f"\n当前位置：**{ratio:.3f}**，最近档位 **{nearest[0]:g}（{nearest[1]}）**，"
              f"距离 {abs(ratio - nearest[0]):.3f}")
    macro = cfg.get("macro") or {}
    as_of = macro.get("as_of", "未知")
    stale = ""
    try:
        age = (datetime.now() - datetime.strptime(as_of, "%Y-%m-%d")).days
        if age > 1:
            stale = f"，已过期{age}天，必须更新"
    except (ValueError, TypeError):
        pass
    print()
    print("## 宏观与情绪（每次报告前必须更新，禁止沿用旧值）")
    print(f"- 宏观偏向：{macro.get('bias_label', '未设置')}（截至 {as_of}{stale}）")
    print(f"- 美联储：{macro.get('fed', '待更新')}")
    print(f"- 非农：{macro.get('nfp', '待更新')}")
    print(f"- 通胀：{macro.get('inflation', '待更新')}")
    print(f"- 金债比解读：{macro.get('ratio_view', '待更新')}")
    if sent:
        label = {"cold": "偏冷", "warm": "偏暖", "neutral": "中性",
                 "mixed": "分歧", "insufficient": "数据不足"}.get(
                     sent.get("tendency"), "缺失")
        print(f"- 情绪结论：**{label}** → {sent.get('clause_buy')}")
        spdr = sent.get("spdr")
        if spdr:
            c1 = spdr.get("change_1d")
            c5 = spdr.get("change_5d")
            flag = "，已过期不计入" if spdr.get("stale") else ""
            print(f"- SPDR：{spdr.get('tonnes', 0):.2f} 吨"
                  f"（日 {c1:+.2f}｜5日 {c5:+.2f}；截至 {spdr.get('as_of')}，"
                  f"{spdr.get('source')}）{flag}"
                  if c1 is not None and c5 is not None else
                  f"- SPDR：{spdr.get('tonnes', 0):.2f} 吨"
                  f"（截至 {spdr.get('as_of')}，{spdr.get('source')}）{flag}")
        else:
            print("- SPDR：未获取")
        vix = sent.get("vix")
        if vix:
            flag = "，已过期不计入" if vix.get("stale") else ""
            print(f"- VIX：{vix.get('value'):.2f}（{vix.get('as_of')}，"
                  f"{vix.get('source')}）→ "
                  f"{ {'cold': '偏冷', 'warm': '偏暖', 'neutral': '中性'}.get(sent.get('vix_dir'), '未判定') }"
                  f"{flag}")
        else:
            print("- VIX：未获取")
        prem = sent.get("premium")
        if prem:
            flag = "，已过期不计入" if prem.get("stale") else ""
            print(f"- 内外盘溢价：{prem['pct']:+.2f}%（上金所 {prem['domestic']:.2f} "
                  f"vs 国际折算 {prem['intl']:.2f} 元/克；国内行情 {prem['date']}）"
                  f"{flag}")
        else:
            print("- 内外盘溢价：未获取")
        for note in sent.get("notes") or []:
            print(f"- 数据提示：{note}")
    print()
    print("## 技术面（MA5/MA60 定趋势，布林带定位置；东方财富沪金主力 元/克）")
    if tech is None:
        print("- 技术数据获取失败，待重试")
    else:
        flag = "（盘中，待收盘确认）" if tech["provisional"] else ""
        print(f"- 数据日期：{tech['as_of']}{flag}，收盘：{tech['close']:.2f} 元/克")
        print(f"- MA5：{tech['ma_fast']:.2f}｜MA60：{tech['ma_slow']:.2f}"
              f"（差 {tech['cross_gap_pct']:+.2f}%）｜趋势：**{tech['trend']}**")
        print(f"- 布林带（M={tech['params']['m']}，K={tech['params']['k']:g}）："
              f"中轨 {tech['boll_mid']:.2f}｜上轨 {tech['ub']:.2f}｜下轨 {tech['lb']:.2f}")
        print(f"- 价格位置：**{tech['position']}**")
        cross_label = {"golden": "金叉", "death": "死叉"}.get(tech.get("last_cross_type"), "无")
        print(f"- 最近交叉：{cross_label}（{tech.get('last_cross_date') or '无'}）")
        print(f"- 组合判断：{tech['combo']}")
        if cny_price <= tech["lb"]:
            intraday_pos = "下轨外（盘中超卖）"
        elif cny_price <= tech["boll_mid"]:
            intraday_pos = "中轨下方（盘中偏弱）"
        elif cny_price <= tech["ub"]:
            intraday_pos = "中轨上方（盘中偏强）"
        else:
            intraday_pos = "上轨外（盘中超买）"
        print(f"- 盘中参考：积存金 {cny_price} 元/克 → {intraday_pos}"
              f"（信号以日线收盘确认为准）")
    print_fee_block(cny_price, cfg)


def run_selftest():
    """纯离线自测：文案表、长度上限、溢价口径、降级路径。"""
    th = {"spdr_5d_inflow_t": 2.0, "spdr_5d_outflow_t": -2.0,
          "spdr_1d_inflow_t": 1.0, "spdr_1d_outflow_t": -1.0,
          "vix_high": 25.0, "vix_low": 15.0,
          "premium_high_pct": 2.0, "premium_low_pct": -0.5,
          "min_indicators": 2}
    fails = []

    def check(name, cond, detail=""):
        print(f"{'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  ' + str(detail)}")
        if not cond:
            fails.append(name)

    def score(spdr=None, vix=None, prem=None):
        return score_sentiment(th, spdr=spdr, vix=vix, premium=prem, cfg={})

    cold = score(spdr={"change_5d": -3.0}, vix={"value": 18.0})
    check("偏冷判定", cold["tendency"] == "cold", cold["tendency"])
    check("偏冷-买入措辞",
          sentiment_clause(cold, "buy") == "情绪偏冷、资金流出，故只试探",
          sentiment_clause(cold, "buy"))
    check("偏冷-卖出措辞",
          sentiment_clause(cold, "sell") == "情绪偏冷、资金流出，可正常止盈",
          sentiment_clause(cold, "sell"))

    warm = score(spdr={"change_5d": 3.0}, vix={"value": 20.0})
    check("偏暖判定", warm["tendency"] == "warm", warm["tendency"])
    check("偏暖-买入措辞",
          sentiment_clause(warm, "buy") == "情绪偏暖、资金流入，可稍积极",
          sentiment_clause(warm, "buy"))

    mixed = score(spdr={"change_5d": -3.0}, vix={"value": 30.0})
    check("分歧判定", mixed["tendency"] == "mixed", mixed["tendency"])
    check("分歧措辞", "按计划执行" in sentiment_clause(mixed, "buy"),
          sentiment_clause(mixed, "buy"))

    thin = score(spdr={"change_5d": -3.0})
    check("指标不足降级", thin["tendency"] == "insufficient", thin["tendency"])
    check("指标不足文案", sentiment_clause(thin, "buy") == SENT_FALLBACK)

    check("无数据文案", sentiment_clause(None, "buy") == SENT_FALLBACK)

    for t in ("cold", "warm", "neutral", "mixed"):
        for act in ("buy", "sell"):
            c = sentiment_clause({"tendency": t, "flow_word": "资金流出"}, act)
            check(f"长度上限 {t}/{act}", len(c) <= 25, f"{len(c)} {c}")

    # 溢价：必须用上金所 Au99.99，不能拿沪金期货 855.18 减国际现价
    check("溢价 缺数据 → None", compute_premium(None, None, 3876.4, 6.671) is None)
    prem = compute_premium(833.0, datetime(2026, 10, 30, 23, 58), 3876.4, 6.671)
    check("溢价 正常计算", prem is not None and abs(prem["pct"]) < 1.5,
          prem and round(prem["pct"], 2))
    check("溢价 跨日标记过期", prem is not None and prem["stale"] is True)
    fake = compute_premium(855.18, datetime(2026, 10, 30), 3876.4, 6.671)
    check("旧口径（期货价）会算出假溢价，已不再使用",
          fake is not None and fake["pct"] > 2.0, fake and round(fake["pct"], 2))

    # 手续费与盈亏：工行买卖各 0.5%、无点差
    icbc = {"fees": {"buy_rate_pct": 0.5, "sell_rate_pct": 0.5,
                     "spread_cny_per_g": 0}}
    check("赎回到手价 800 → 796.0", abs(sell_net_price(800, icbc) - 796.0) < 1e-9,
          sell_net_price(800, icbc))
    check("买入成本 800 → 804.02（费从金额扣，是除不是乘）",
          abs(buy_effective_cost(800, icbc) - 800 / 0.995) < 1e-9,
          buy_effective_cost(800, icbc))
    check("保本价 804.02 → 808.06",
          abs(breakeven_price(800 / 0.995, icbc) - 800 / 0.995 / 0.995) < 1e-9,
          breakeven_price(800 / 0.995, icbc))
    rt = calc_sell(800, icbc, grams=10, cost_per_g=800 / 0.995)
    check("原价买卖一轮亏约 1%（双边各 0.5%）",
          -1.5 < rt["pnl_pct"] < -0.9, round(rt["pnl_pct"], 3))
    check("盈亏金额 = 到手 - 成本",
          abs(rt["pnl"] - (rt["proceeds"] - rt["cost_total"])) < 1e-9, rt["pnl"])
    zero = {"fees": {"buy_rate_pct": 0, "sell_rate_pct": 0, "spread_cny_per_g": 0}}
    check("零费率时到手价=赎回价", abs(sell_net_price(800, zero) - 800) < 1e-9)
    check("缺配置时按工行默认费率", abs(sell_net_price(1000, {}) - 995.0) < 1e-9,
          sell_net_price(1000, {}))
    no_hold = calc_sell(800, icbc)
    check("未设持仓不瞎算盈亏", "pnl" not in no_hold)
    buy_line = buy_cost_line(831, icbc)
    sell_line = sell_pnl_line(848, {"fees": icbc["fees"],
                                    "holding": {"grams": 10, "cost_per_g": 800}})
    check("买入卡片成本行非空", "保本" in buy_line, buy_line)
    check("卖出卡片带总到账与盈亏",
          "到账" in sell_line and "盈亏" in sell_line, sell_line)
    check("卖出卡片到手价已扣费", "843.8" in sell_line, sell_line)

    # 只知道成本、不知道克数
    cost_only = {"fees": icbc["fees"], "holding": {"cost_per_g": 848}}
    co = calc_sell(837, cost_only)
    check("只填成本也能算每克盈亏", "pnl" not in co and co.get("pnl_per_g") is not None,
          co.get("pnl_per_g"))
    check("只填成本不漏算总盈亏", "proceeds" not in co)
    check("848 成本 + 0.5% 赎回 → 保本 852.26",
          abs(co["breakeven"] - 848 / 0.995) < 1e-6, round(co["breakeven"], 2))
    check("837 卖出手续费后亏约 1.8%", -2.0 < co["pnl_pct"] < -1.5,
          round(co["pnl_pct"], 2))
    check("低于保本价给出亏损提示", below_breakeven(837, cost_only) is not None,
          below_breakeven(837, cost_only))
    check("高于保本价不提示亏损", below_breakeven(860, cost_only) is None)
    # 保本提示是一行独立信息，不改阈值也不改操作建议
    note_below = breakeven_note(837, cost_only)
    check("低于保本价：提示亏多少", "亏" in note_below and "需再涨" in note_below,
          note_below)
    note_above = breakeven_note(860, cost_only)
    check("高于保本价：提示赚多少且不出现需再涨",
          "赚" in note_above and "需再涨" not in note_above, note_above)
    check("未设成本时不给保本提示", breakeven_note(837, icbc) == "")
    check("提示里带保本价", "852.3" in breakeven_note(837, cost_only),
          breakeven_note(837, cost_only))
    # 操作建议仍由行情阈值决定，不因成本价改写
    check("操作行文案不受持仓成本影响",
          "挂单卖出" not in note_below and "操作" not in note_below, note_below)

    print(f"\n{'全部通过' if not fails else '失败 ' + str(len(fails)) + ' 项'}")
    return 0 if not fails else 1


def main():
    global DRY_RUN
    if "--selftest" in sys.argv:
        sys.exit(run_selftest())
    if "--calc" in sys.argv:
        args = sys.argv[sys.argv.index("--calc") + 1:]
        try:
            price = float(args[0])
        except (IndexError, ValueError):
            print("用法：--calc 赎回价 [--grams 克数] [--cost 成本单价]")
            sys.exit(2)
        opts = {}
        for flag in ("--grams", "--cost"):
            if flag in args:
                try:
                    opts[flag] = float(args[args.index(flag) + 1])
                except (IndexError, ValueError):
                    print(f"{flag} 后面要跟数字")
                    sys.exit(2)
        sys.exit(run_calc(price, opts.get("--grams"), opts.get("--cost")))
    if "--dry-run" in sys.argv:
        DRY_RUN = True

    brief_mode = "--brief" in sys.argv
    if not brief_mode and not DRY_RUN and not should_run():
        reason = skip_reason() or "非交易时段"
        logging.info(f"Skipped: {reason}")
        print(f"[SKIP] {reason}")
        sys.exit(0)

    logging.info("=" * 50)
    logging.info("Gold monitor started (ICBC 积存金 mode)")

    try:
        usd_price, gold_updated = fetch_gold_price()
        rate, fx_updated = fetch_exchange_rate()
    except Exception as e:
        logging.error(f"Failed to fetch prices: {e}")
        sys.exit(1)

    intl_cny = usd_price * rate / 31.1035
    domestic = domestic_ts = domestic_pct = None
    try:
        domestic, domestic_ts, domestic_pct = fetch_domestic_gold()
    except Exception as e:
        logging.warning(f"Domestic gold quote failed ({e}), "
                        f"falling back to spot conversion")
    # 积存金按上金所 Au99.99 定价；取不到才退回国际金折算
    # 报价超过 24 小时未更新的（周末、长假），不能拿来触发买卖，
    # 否则会拿周五收盘价对比今天的国际金，发出假信号。
    fresh = (domestic is not None and domestic_ts is not None
             and (datetime.now() - domestic_ts).total_seconds() <= 24 * 3600)
    if domestic is not None and not fresh:
        logging.warning(f"Domestic quote stale ({domestic_ts}), falling back to spot conversion")
        domestic = domestic_pct = domestic_ts = None
    cny_price = int(round(domestic if domestic else intl_cny))
    price_basis = "sge" if domestic else "spot"
    domestic_factor = (float(domestic) / intl_cny) if (domestic and intl_cny > 0) else 1.0
    logging.info(f"Gold: {usd_price} USD/oz, USDCNY: {rate}, "
                 f"上金所: {domestic}, 积存金参考: {cny_price} CNY/g")

    cfg = load_config()
    if cfg is None:
        print(f"[OK] {usd_price} USD/oz ~ {cny_price} CNY/g | no config, skipped")
        sys.exit(0)

    # 金债比：最高优先级价格参考（0.6/0.8/1.0/1.23 四档）
    ratio = None
    ratio_text = ""
    ratio_support = ratio_resistance = None
    level_prices = []
    levels_text = ""
    yield_pct = yield_date = None
    ratio_block = cfg.get("gold_bond_ratio") or {}
    if ratio_block:
        divisor = float(ratio_block.get("divisor", 1000))
        ratio_support = float(ratio_block.get("support", 0.6))
        ratio_resistance = float(ratio_block.get("resistance", 1.23))
        try:
            yield_pct, yield_date = fetch_10y_yield()
            logging.info(f"10Y yield: {yield_pct}% ({yield_date})")
        except Exception as e:
            yield_pct = ratio_block.get("yield_10y")
            yield_date = ratio_block.get("yield_date", "config")
            logging.warning(f"Failed to fetch 10Y yield ({e}), "
                            f"using config {yield_pct}% ({yield_date})")
        if yield_pct:
            ratio = usd_price / (float(yield_pct) * divisor)
            for lv in ratio_block.get("levels", []):
                lr = float(lv["ratio"])
                lv_usd = lr * float(yield_pct) * divisor
                # 档位折算成国内价（含内外盘溢价），否则拿国际价对比积存金会偏 2–3%
                lv_cny = lv_usd * rate / 31.1035 * domestic_factor
                level_prices.append((lr, lv.get("label", ""), lv_usd, lv_cny))
            if level_prices:
                levels_text = "\n".join(
                    f"- {lr:g} → ${u:,.0f}/oz ≈ {c:,.0f} 元/克（{lbl}）"
                    for lr, lbl, u, c in level_prices
                )
                nearest = min(level_prices, key=lambda x: abs(ratio - x[0]))
                ratio_text = (f"{ratio:.3f}（区间 {ratio_support}–{ratio_resistance}；"
                              f"最近档位 {nearest[0]:g}≈{nearest[3]:.0f}元/克；"
                              f"10Y {float(yield_pct):.2f}% {yield_date}）")
                logging.info("Gold-bond ratio: %.4f\nGold-bond levels:\n%s",
                             ratio, levels_text)
            else:
                ratio_text = (f"{ratio:.3f}（区间 {ratio_support}–{ratio_resistance}，"
                              f"10Y {float(yield_pct):.2f}% {yield_date}）")
                logging.info(f"Gold-bond ratio: {ratio:.4f}")

    tech = None
    tech_block = cfg.get("tech") or {}
    try:
        bars = fetch_gold_daily(tech_block.get("series", "aum"),
                                sina_symbol=tech_block.get("sina_symbol", "AU0"))
        tech = compute_tech(
            bars,
            fast=int(tech_block.get("ma_fast", 5)),
            slow=int(tech_block.get("ma_slow", 60)),
            m=int(tech_block.get("boll_m", 20)),
            k=float(tech_block.get("boll_k", 2)),
        )
        logging.info("Tech: close=%.2f ma5=%.2f ma60=%.2f trend=%s position=%s",
                     tech["close"], tech["ma_fast"], tech["ma_slow"],
                     tech["trend"], tech["position"])
    except Exception as e:
        logging.warning(f"Failed to compute tech indicators: {e}")

    # 市场情绪：SPDR 持仓 + VIX + 内外盘溢价。任何失败都降级，不影响提醒发送。
    try:
        sent = compute_sentiment(cfg, domestic=domestic, domestic_ts=domestic_ts,
                                 spot_usd=usd_price, rate=rate)
    except Exception as e:
        logging.error(f"Sentiment computation failed: {e}")
        sent = None

    if brief_mode:
        print_brief(usd_price, gold_updated, rate, fx_updated, cny_price,
                    ratio, ratio_support, ratio_resistance,
                    yield_pct, yield_date, level_prices, cfg, tech,
                    sent=sent, domestic_ts=domestic_ts)
        return

    triggered = False

    # 价格阈值缺省时按金债比档位自动推导（0.8 近端支撑 / 1.0 阻力），
    # 这样阈值随 10Y 收益率动态走，不需要人工维护。
    if "cny_support1" not in cfg and level_prices:
        lv = [c for r_, _, _, c in level_prices if abs(r_ - 0.8) < 1e-9]
        if lv:
            cfg["cny_support1"] = int(round(lv[0]))
    if "cny_resistance1" not in cfg and level_prices:
        lv = [c for r_, _, _, c in level_prices if abs(r_ - 1.0) < 1e-9]
        if lv:
            cfg["cny_resistance1"] = int(round(lv[0]))
    cny_support = cfg.get("cny_support1", 830)
    cny_resistance = cfg.get("cny_resistance1", 847)

    if cny_price <= cny_support:
        logging.info(f"BUY SIGNAL: {cny_price} <= cny_support1 ({cny_support})")
        try:
            send_feishu_cny(usd_price, cny_price, "buy", cfg,
                            ratio_text=ratio_text, sent=sent, basis=price_basis)
        except Exception as e:
            logging.error(f"Failed to send buy alert: {e}")
        triggered = True

    if cny_price >= cny_resistance:
        logging.info(f"SELL SIGNAL: {cny_price} >= cny_resistance1 ({cny_resistance})")
        try:
            send_feishu_cny(usd_price, cny_price, "sell", cfg,
                            ratio_text=ratio_text, sent=sent, basis=price_basis)
        except Exception as e:
            logging.error(f"Failed to send sell alert: {e}")
        triggered = True

    if ratio is not None:
        if ratio <= ratio_support:
            logging.info(f"RATIO LOW: {ratio:.4f} <= {ratio_support}")
            try:
                send_feishu_ratio(ratio, usd_price, cny_price, "low", cfg,
                                  levels_text, sent=sent)
            except Exception as e:
                logging.error(f"Failed to send ratio low alert: {e}")
            triggered = True
        elif ratio >= ratio_resistance:
            logging.info(f"RATIO HIGH: {ratio:.4f} >= {ratio_resistance}")
            try:
                send_feishu_ratio(ratio, usd_price, cny_price, "high", cfg,
                                  levels_text, sent=sent)
            except Exception as e:
                logging.error(f"Failed to send ratio high alert: {e}")
            triggered = True

    if tech is not None:
        if tech["provisional"]:
            logging.info(f"Tech bar {tech['as_of']} provisional, waiting for close")
        elif tech.get("last_cross_date") and tech.get("last_cross_type"):
            state = load_tech_state()
            if state is None:
                if not DRY_RUN:  # 演练不能消费掉真实交叉信号
                    save_tech_state(tech)
                logging.info(f"Tech state initialized: {tech['last_cross_type']} "
                             f"on {tech['last_cross_date']}")
            elif (state.get("last_cross_date") != tech["last_cross_date"]
                  or state.get("last_cross_type") != tech["last_cross_type"]):
                logging.info(f"TECH CROSS: {tech['last_cross_type']} on "
                             f"{tech['last_cross_date']}")
                try:
                    send_feishu_tech(tech, cfg, ratio_text=ratio_text, sent=sent,
                                     cny_price=cny_price)
                except Exception as e:
                    logging.error(f"Failed to send tech alert: {e}")
                triggered = True
                if not DRY_RUN:
                    save_tech_state(tech)

    ratio_out = f" 金债比={ratio:.3f}" if ratio is not None else ""
    if level_prices:
        ratio_out += " [" + " ".join(
            f"{lr:g}:{c:.0f}" for lr, _, _, c in level_prices
        ) + "元/克]"
    macro = cfg.get("macro") or {}
    if macro.get("bias_label"):
        ratio_out += f" 宏观={macro['bias_label']}"
    if sent and sent.get("tendency"):
        ratio_out += f" 情绪={sent['tendency']}"
    tech_out = ""
    if tech is not None:
        tech_out = f" 技术={tech['trend']}/{tech['position']}"
    if triggered:
        print(f"[ALERT] {usd_price:,.2f} USD/oz ~ {cny_price} CNY/g{ratio_out}{tech_out} | 提醒已发送")
    else:
        print(f"[OK] {usd_price:,.2f} USD/oz ~ {cny_price} CNY/g{ratio_out}{tech_out} | "
              f"支撑={cny_support} 阻力={cny_resistance}")


if __name__ == "__main__":
    main()
