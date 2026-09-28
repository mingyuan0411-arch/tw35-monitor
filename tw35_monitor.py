#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Taiwan Stock 3~5% Short-Term Long Monitor v1.0
===============================================

台股版 35 雷達，核心沿用目前 35 v1.9 邏輯，但改成台股專用：

1H  -> 主趨勢
15m -> READY 前置條件
5m  -> ENTRY 最後觸發

重要規則：
- 只做趨勢多
- 不自動下單，最後由人工決定
- READY 前先估算空間：>= 2.5% 才進 1 分鐘快掃
- 5m 達標後重新估算：>= 2.0% 才發正式 ENTRY
- 5m 達標但空間不足 -> ENTRY_CHECK
- READY / ENTRY_CHECK 後，每 60 秒快掃，最多 5 輪
- 台股只在台北時間 09:00~13:30、週一至週五監控
- 非交易日 / 無新資料時不發交易訊號
- 顯示 5m 成交額、近 1H 成交額、5m 量能比
- 成交額以「成交量 x 價格」估算，單位為 TWD
- 考慮台股單日漲停約 +10% 上限，不把目標估到漲停之外
"""

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, time as dt_time
from email.header import Header
from pathlib import Path
from zoneinfo import ZoneInfo


# ============================================================
# 標的
# ============================================================

TW_STOCKS = {
    "0050":"0050.TW", "2330":"2330.TW", "2317":"2317.TW", "2454":"2454.TW", "2308":"2308.TW",
    "2881":"2881.TW", "2882":"2882.TW", "2891":"2891.TW",
    "1216":"1216.TW", "1301":"1301.TW", "1303":"1303.TW", "2002":"2002.TW",
    "2412":"2412.TW", "2603":"2603.TW", "1101":"1101.TW",
}

TW_NAMES = {
    "0050":"元大台灣50", "2330":"台積電", "2317":"鴻海", "2454":"聯發科", "2308":"台達電",
    "2881":"富邦金", "2882":"國泰金", "2891":"中信金",
    "1216":"統一", "1301":"台塑", "1303":"南亞", "2002":"中鋼",
    "2412":"中華電", "2603":"長榮", "1101":"台泥",
}


# ============================================================
# 參數
# ============================================================

ONE_H = 60 * 60
FIFTEEN_M = 15 * 60
FIVE_M = 5 * 60

MIN_READY_SPACE_PCT = 2.5
MIN_ENTRY_SPACE_PCT = 2.0
# 台股成本預設採保守上限，可由環境變數改成你的券商實際折扣。
TW_COMMISSION_RATE = float(os.getenv("TW35_COMMISSION_RATE", "0.001425"))
TW_STOCK_SELL_TAX = float(os.getenv("TW35_STOCK_SELL_TAX", "0.003"))
TW_ETF_SELL_TAX = float(os.getenv("TW35_ETF_SELL_TAX", "0.001"))

def tw_sell_tax(code):
    return TW_ETF_SELL_TAX if code == "0050" else TW_STOCK_SELL_TAX

def tw_net_exit_price(entry, code, desired_net_pct):
    if not entry or entry <= 0: return None
    buy_cost = entry * (1 + TW_COMMISSION_RATE)
    sell_deduct = TW_COMMISSION_RATE + tw_sell_tax(code)
    return buy_cost * (1 + desired_net_pct/100.0) / (1 - sell_deduct)

FAST_SCAN_SECONDS = 60
FAST_SCAN_ROUNDS = 5

SUMMARY_INTERVAL = 30 * 60

TW_TZ = ZoneInfo("Asia/Taipei")
TW_OPEN = dt_time(9, 0)
TW_CLOSE = dt_time(13, 30)

STATE_DIR = Path(".tw35_state")
STATE_FILE = STATE_DIR / "state.json"


# ============================================================
# ntfy
# ============================================================

NTFY_SERVER = os.getenv(
    "NTFY_SERVER",
    "https://ntfy.sh"
).rstrip("/")

# 優先使用台股專用 topic；沒設時可沿用原 35 topic
NTFY_TOPIC = (
    os.getenv("NTFY_TOPIC_TW35", "").strip()
    or os.getenv("NTFY_TOPIC_SHORT35", "").strip()
)

GITHUB_EVENT_NAME = os.getenv(
    "GITHUB_EVENT_NAME",
    ""
).strip()

MANUAL_RUN = (
    GITHUB_EVENT_NAME == "workflow_dispatch"
)


# ============================================================
# 基礎工具
# ============================================================

def now_iso():
    return datetime.now(timezone.utc).isoformat()


def price_text(v):
    if v is None:
        return "N/A"
    if abs(v) >= 100:
        return f"{v:.2f}"
    if abs(v) >= 10:
        return f"{v:.3f}"
    if abs(v) >= 1:
        return f"{v:.4f}"
    return f"{v:.6f}"


def pct_text(v):
    if v is None:
        return "N/A"
    return f"{v:+.2f}%"


def money_twd(v):
    if v is None:
        return "N/A"
    v = float(v)
    if abs(v) >= 100_000_000:
        return f"{v / 100_000_000:.2f} 億"
    if abs(v) >= 10_000:
        return f"{v / 10_000:.2f} 萬"
    return f"{v:,.0f}"


def pct_change(base_price, current_price):
    if (
        base_price is None
        or current_price is None
        or base_price <= 0
    ):
        return None

    return (
        current_price
        / base_price
        - 1
    ) * 100


def tw_market_open(now_utc=None):
    now_utc = now_utc or datetime.now(timezone.utc)
    tw_now = now_utc.astimezone(TW_TZ)

    if tw_now.weekday() >= 5:
        return False

    t = tw_now.time().replace(tzinfo=None)

    return (
        TW_OPEN <= t < TW_CLOSE
    )


def tw_trade_date(now_utc=None):
    now_utc = now_utc or datetime.now(timezone.utc)
    return now_utc.astimezone(TW_TZ).date().isoformat()


# ============================================================
# Yahoo Finance Chart API
# ============================================================

YAHOO_HOSTS = [
    "https://query1.finance.yahoo.com",
    "https://query2.finance.yahoo.com",
]


def yahoo_get(symbol, interval, range_text, retries=4):
    params = urllib.parse.urlencode({
        "interval": interval,
        "range": range_text,
        "includePrePost": "false",
        "events": "div,splits",
    })

    last_error = None

    for attempt in range(retries):
        host = YAHOO_HOSTS[attempt % len(YAHOO_HOSTS)]
        url = (
            f"{host}/v8/finance/chart/"
            f"{urllib.parse.quote(symbol)}?{params}"
        )

        try:
            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": (
                        "Mozilla/5.0 "
                        "(Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 Chrome/124 Safari/537.36"
                    ),
                }
            )

            with urllib.request.urlopen(
                req,
                timeout=25
            ) as resp:
                data = json.load(resp)

            result = (
                data
                .get("chart", {})
                .get("result")
            )

            if not result:
                err = data.get("chart", {}).get("error")
                raise RuntimeError(
                    f"Yahoo no result: {err}"
                )

            return result[0]

        except Exception as e:
            last_error = e
            time.sleep(min(2 ** attempt, 6))

    raise RuntimeError(
        f"Yahoo request failed: {last_error}"
    )


def fetch(symbol, interval, range_text):
    data = yahoo_get(
        symbol,
        interval,
        range_text
    )

    timestamps = data.get("timestamp") or []

    indicators = data.get(
        "indicators",
        {}
    )

    quotes = (
        indicators.get("quote")
        or [{}]
    )[0]

    opens = quotes.get("open") or []
    highs = quotes.get("high") or []
    lows = quotes.get("low") or []
    closes = quotes.get("close") or []
    volumes = quotes.get("volume") or []

    rows = []

    for i, ts in enumerate(timestamps):
        try:
            o = opens[i]
            h = highs[i]
            l = lows[i]
            c = closes[i]
            v = volumes[i]
        except IndexError:
            continue

        if (
            o is None
            or h is None
            or l is None
            or c is None
        ):
            continue

        volume = float(v or 0)
        close = float(c)

        rows.append({
            "t": int(ts),
            "o": float(o),
            "h": float(h),
            "l": float(l),
            "c": close,
            "v": volume,

            # 台股現貨成交額估算：股數 x 價格
            "amount": abs(volume * close),
        })

    rows.sort(
        key=lambda x: x["t"]
    )

    return rows


def completed_only(rows, step, now_ts):
    return [
        r
        for r in rows
        if r["t"] + step <= now_ts
    ]


def data_is_fresh(rows, max_age_seconds):
    if not rows:
        return False

    now_ts = int(
        datetime.now(
            timezone.utc
        ).timestamp()
    )

    latest_t = rows[-1]["t"]

    return (
        now_ts - latest_t
        <= max_age_seconds
    )


# ============================================================
# 指標
# ============================================================

def sma(values, n, i):
    if i + 1 < n:
        return None

    return sum(
        values[i - n + 1:i + 1]
    ) / n


def true_range(current, previous):
    if previous is None:
        return current["h"] - current["l"]

    return max(
        current["h"] - current["l"],
        abs(current["h"] - previous["c"]),
        abs(current["l"] - previous["c"]),
    )


def add_indicators(rows):
    closes = [
        r["c"]
        for r in rows
    ]

    volumes = [
        r["v"]
        for r in rows
    ]

    trs = []

    for i, r in enumerate(rows):
        previous = (
            rows[i - 1]
            if i > 0
            else None
        )

        trs.append(
            true_range(
                r,
                previous
            )
        )

    for i, r in enumerate(rows):
        r["ma5"] = sma(closes, 5, i)
        r["ma10"] = sma(closes, 10, i)
        r["ma20"] = sma(closes, 20, i)
        r["ma60"] = sma(closes, 60, i)

        r["ma20_prev"] = (
            sma(closes, 20, i - 1)
            if i >= 20
            else None
        )

        r["vma5"] = sma(volumes, 5, i)
        r["vma20"] = sma(volumes, 20, i)
        r["atr14"] = sma(trs, 14, i)


# ============================================================
# 訊號
# ============================================================

def daily_long_allowed(r):
    need=[r.get("ma20"),r.get("ma20_prev"),r.get("ma5"),r.get("ma10")]
    if any(x is None for x in need): return False
    return (r["c"] >= r["ma20"]*0.98 and r["ma20"] >= r["ma20_prev"]*0.995) or (r["c"] > r["ma20"] and r["ma5"] >= r["ma10"]*0.98)

def one_hour_trend(r):
    needed = [
        r.get("ma5"),
        r.get("ma10"),
        r.get("ma20"),
        r.get("ma60"),
        r.get("ma20_prev"),
    ]

    if any(
        x is None
        for x in needed
    ):
        return False

    return (
        r["c"] >= r["ma20"] * 0.990
        and r["ma20"] >= r["ma20_prev"] * 0.995
        and r["ma5"] >= r["ma10"] * 0.985
    )


def fifteen_min_ready(r):
    needed = [
        r.get("ma5"),
        r.get("ma10"),
        r.get("ma20"),
    ]

    if any(
        x is None
        for x in needed
    ):
        return False

    return (
        r["c"] >= r["ma20"] * 0.995
        and
        r["ma5"] >= r["ma10"] * 0.995
    )


def five_min_entry(current, previous):
    needed = [
        current.get("ma5"),
        current.get("ma10"),
        current.get("ma20"),
        current.get("vma5"),
        current.get("vma20"),
        previous.get("ma5"),
        previous.get("ma10"),
    ]

    if any(
        x is None
        for x in needed
    ):
        return False

    cross_up = (
        current["ma5"] > current["ma10"]
        and
        previous["ma5"] <= previous["ma10"]
    )

    already_strong = (
        current["ma5"] > current["ma10"]
        and
        current["c"] > current["ma20"]
    )

    volume_ok = (
        current["vma5"] > current["vma20"]
    )

    return (
        (
            cross_up
            or already_strong
        )
        and volume_ok
    )


# ============================================================
# 目標 / 空間
# ============================================================

def recent_resistance(current_price, r1, r15):
    candidates = []

    for r in r1[-40:]:
        if r["h"] > current_price:
            candidates.append(r["h"])

    for r in r15[-80:]:
        if r["h"] > current_price:
            candidates.append(r["h"])

    if not candidates:
        return None

    return min(candidates)


def estimate_potential(
    r1,
    r15,
    latest5
):
    latest1 = r1[-1]
    entry_price = latest5["c"]

    resistance = recent_resistance(
        entry_price,
        r1,
        r15
    )

    atr = latest1.get("atr14")

    atr_pct = None

    if (
        atr is not None
        and entry_price > 0
    ):
        atr_pct = (
            atr
            / entry_price
            * 100
        )

    resistance_pct = None

    if (
        resistance is not None
        and resistance > entry_price
    ):
        resistance_pct = (
            resistance
            / entry_price
            - 1
        ) * 100

    atr_target_pct = (
        atr_pct * 2.0
        if atr_pct is not None
        else None
    )

    volume_ratio = None

    if (
        latest5.get("vma5")
        and latest5.get("vma20")
    ):
        volume_ratio = (
            latest5["vma5"]
            / latest5["vma20"]
        )

    trend_bonus = 0.0

    if (
        latest1.get("ma5") is not None
        and latest1.get("ma10") is not None
        and latest1.get("ma20") is not None
        and latest1["ma5"]
        > latest1["ma10"]
        > latest1["ma20"]
    ):
        trend_bonus += 0.5

    if volume_ratio is not None:
        if volume_ratio >= 1.5:
            trend_bonus += 0.8
        elif volume_ratio >= 1.2:
            trend_bonus += 0.4

    candidates = []

    if resistance_pct is not None:
        candidates.append(
            resistance_pct
        )

    if atr_target_pct is not None:
        candidates.append(
            atr_target_pct
        )

    if candidates:
        base_potential = min(
            candidates
        )
    else:
        base_potential = 3.0

    potential_pct = max(
        0.5,
        base_potential + trend_bonus
    )

    # 35 策略只關注 3~5% 級距，不做過度樂觀延伸
    potential_pct = min(
        potential_pct,
        5.0
    )

    # 台股單日價格限制保護：
    # 目標不可超過「目前價 + 9.5%」的安全上限。
    potential_pct = min(
        potential_pct,
        9.5
    )

    target_price = (
        entry_price
        * (
            1 + potential_pct / 100
        )
    )

    tp1_pct = min(
        potential_pct * 0.60,
        3.0
    )

    tp2_pct = min(
        potential_pct,
        5.0
    )

    tp1 = (
        entry_price
        * (
            1 + tp1_pct / 100
        )
    )

    tp2 = (
        entry_price
        * (
            1 + tp2_pct / 100
        )
    )

    if potential_pct >= 4.5:
        grade = "HIGH"
    elif potential_pct >= 3.0:
        grade = "OK"
    else:
        grade = "LOW"

    return {
        "entry_price": entry_price,
        "target_price": target_price,
        "potential_pct": potential_pct,
        "grade": grade,
        "resistance": resistance,
        "resistance_pct": resistance_pct,
        "atr_pct": atr_pct,
        "tp1": tp1,
        "tp2": tp2,
        "volume_ratio": volume_ratio,
    }


# ============================================================
# 流量
# ============================================================

def current_5m_amount(r5):
    if not r5:
        return None
    return r5[-1].get("amount")


def rolling_1h_amount(r5):
    if not r5:
        return None

    values = [
        r.get("amount")
        for r in r5[-12:]
        if r.get("amount") is not None
    ]

    return (
        sum(values)
        if values
        else None
    )


# ============================================================
# 單一股票分析
# ============================================================

def analyze(code, yahoo_symbol):
    now_ts = int(
        datetime.now(
            timezone.utc
        ).timestamp()
    )

    r1d = completed_only(fetch(yahoo_symbol, "1d", "1y"), ONE_D, now_ts)

    # Yahoo intraday 支援的範圍不同，所以分開抓
    r1 = completed_only(
        fetch(
            yahoo_symbol,
            "60m",
            "3mo"
        ),
        ONE_H,
        now_ts
    )

    r15 = completed_only(
        fetch(
            yahoo_symbol,
            "15m",
            "60d"
        ),
        FIFTEEN_M,
        now_ts
    )

    # 5m 保留最新正在形成中的 K 棒，
    # READY 快掃時可在第 1~4 分鐘提前抓到 ENTRY。
    r5 = fetch(
        yahoo_symbol,
        "5m",
        "10d"
    )

    if (
        len(r1d) < 30
        or len(r1) < 65
        or len(r15) < 65
        or len(r5) < 65
    ):
        return {
            "code": code,
            "name": TW_NAMES.get(code, code),
            "symbol": yahoo_symbol,
            "status": "WAIT_HISTORY",
        }

    add_indicators(r1d)
    add_indicators(r1)
    add_indicators(r15)
    add_indicators(r5)

    latest1d = r1d[-1]
    latest1 = r1[-1]
    latest15 = r15[-1]
    latest5 = r5[-1]
    prev5 = r5[-2]

    # 台股開盤期間，最新 5m 資料若超過 20 分鐘沒更新，
    # 視為假日 / API 延遲 / 無新成交，不發訊號。
    if (
        tw_market_open()
        and not data_is_fresh(
            r5,
            20 * 60
        )
    ):
        return {
            "code": code,
            "name": TW_NAMES.get(code, code),
            "symbol": yahoo_symbol,
            "status": "STALE_DATA",
            "price": latest5["c"],
        }

    d1_allowed = daily_long_allowed(latest1d)
    h1 = one_hour_trend(latest1)
    m15 = fifteen_min_ready(latest15)
    m5 = five_min_entry(
        latest5,
        prev5
    )

    potential = None

    amount_5m = current_5m_amount(r5)
    amount_1h = rolling_1h_amount(r5)

    if (
        d1_allowed
        and h1
        and m15
    ):
        potential = estimate_potential(
            r1,
            r15,
            latest5
        )

        potential["amount_5m"] = amount_5m
        potential["amount_1h"] = amount_1h
        ep = potential.get("entry_price") or latest5["c"]
        potential["breakeven_after_cost"] = tw_net_exit_price(ep, code, 0.0)
        potential["net_tp3"] = tw_net_exit_price(ep, code, 3.0)
        potential["net_tp5"] = tw_net_exit_price(ep, code, 5.0)
        potential["commission_rate"] = TW_COMMISSION_RATE
        potential["sell_tax_rate"] = tw_sell_tax(code)

        current_space = potential.get(
            "potential_pct"
        )

        if m5:
            if (
                current_space is not None
                and current_space
                >= MIN_ENTRY_SPACE_PCT
            ):
                status = "ENTRY"
            else:
                status = "ENTRY_CHECK"

        elif (
            current_space is not None
            and current_space
            >= MIN_READY_SPACE_PCT
        ):
            status = "READY"

        else:
            status = "WATCH"

    elif d1_allowed:
        status = "WATCH"

    else:
        status = "NO_SIGNAL"

    return {
        "code": code,
        "name": TW_NAMES.get(code, code),
        "symbol": yahoo_symbol,
        "status": status,
        "1d_long_allowed": d1_allowed,
        "price": latest5["c"],
        "price_1h": latest1["c"],
        "price_15m": latest15["c"],
        "price_5m": latest5["c"],
        "1h_trend": h1,
        "15m_ready": m15,
        "5m_entry": m5,
        "potential": potential,
        "amount_5m": amount_5m,
        "amount_1h": amount_1h,
    }


# ============================================================
# ntfy
# ============================================================

def send_ntfy(
    title,
    msg,
    priority="default",
    tags="bell"
):
    if not NTFY_TOPIC:
        print(
            "NTFY_TOPIC_TW35 / NTFY_TOPIC_SHORT35 未設定"
        )
        return False

    safe_title = str(
        Header(
            title,
            "utf-8"
        )
    )

    req = urllib.request.Request(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=msg.encode("utf-8"),
        method="POST",
        headers={
            "Title": safe_title,
            "Priority": priority,
            "Tags": tags,
            "Content-Type": (
                "text/plain; charset=utf-8"
            ),
        }
    )

    try:
        with urllib.request.urlopen(
            req,
            timeout=20
        ) as resp:
            print(
                "NTFY:",
                resp.status,
                title
            )
            return True

    except Exception as e:
        print(
            "NTFY ERROR:",
            title,
            str(e)
        )
        return False


# ============================================================
# State
# ============================================================

def load_state():
    STATE_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    if not STATE_FILE.exists():
        return {
            "symbols": {},
            "last_summary_utc": None,
        }

    try:
        with STATE_FILE.open(
            "r",
            encoding="utf-8"
        ) as f:
            state = json.load(f)

        state.setdefault(
            "symbols",
            {}
        )

        state.setdefault(
            "last_summary_utc",
            None
        )

        return state

    except Exception as e:
        print(
            "STATE LOAD ERROR:",
            str(e)
        )

        return {
            "symbols": {},
            "last_summary_utc": None,
        }


def save_state(state):
    STATE_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    with STATE_FILE.open(
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(
            state,
            f,
            ensure_ascii=False,
            indent=2
        )


def reset_cycle(symbol_state):
    keep = {
        "status",
        "updated_utc",
        "tw_trade_date",
    }

    keys = list(
        symbol_state.keys()
    )

    for key in keys:
        if key not in keep:
            symbol_state.pop(
                key,
                None
            )


def update_stage_memory(
    r,
    symbol_state,
    previous
):
    status = r["status"]
    price = r.get("price")

    if price is not None:
        symbol_state[
            "current_price"
        ] = price

    symbol_state[
        "last_seen_utc"
    ] = now_iso()

    if (
        previous in (
            "NO_SIGNAL",
            "UNKNOWN",
            "STALE_DATA",
        )
        and
        status in (
            "WATCH",
            "READY",
            "ENTRY_CHECK",
            "ENTRY",
        )
    ):
        reset_cycle(
            symbol_state
        )

    if (
        status in (
            "WATCH",
            "READY",
            "ENTRY_CHECK",
            "ENTRY",
        )
        and
        symbol_state.get(
            "watch_price"
        )
        is None
    ):
        symbol_state[
            "watch_price"
        ] = price

        symbol_state[
            "watch_time"
        ] = now_iso()

    if (
        status in (
            "READY",
            "ENTRY_CHECK",
            "ENTRY",
        )
        and
        symbol_state.get(
            "ready_price"
        )
        is None
    ):
        symbol_state[
            "ready_price"
        ] = price

        symbol_state[
            "ready_time"
        ] = now_iso()

    if (
        status == "ENTRY"
        and
        symbol_state.get(
            "entry_price"
        )
        is None
    ):
        p = (
            r.get("potential")
            or {}
        )

        symbol_state[
            "entry_price"
        ] = price

        symbol_state[
            "entry_time"
        ] = now_iso()

        symbol_state[
            "entry_target_price"
        ] = p.get(
            "target_price"
        )

        symbol_state[
            "entry_original_potential_pct"
        ] = p.get(
            "potential_pct"
        )

        symbol_state[
            "entry_grade"
        ] = p.get(
            "grade"
        )

        symbol_state[
            "entry_tp1"
        ] = p.get(
            "tp1"
        )

        symbol_state[
            "entry_tp2"
        ] = p.get(
            "tp2"
        )

        symbol_state[
            "entry_resistance"
        ] = p.get(
            "resistance"
        )

        symbol_state[
            "entry_atr_pct"
        ] = p.get(
            "atr_pct"
        )

        symbol_state[
            "entry_volume_ratio"
        ] = p.get(
            "volume_ratio"
        )

        symbol_state[
            "entry_amount_5m"
        ] = p.get(
            "amount_5m"
        )

        symbol_state[
            "entry_amount_1h"
        ] = p.get(
            "amount_1h"
        )


def stage_stats(symbol_state):
    current = symbol_state.get(
        "current_price"
    )

    watch_price = symbol_state.get(
        "watch_price"
    )

    ready_price = symbol_state.get(
        "ready_price"
    )

    entry_price = symbol_state.get(
        "entry_price"
    )

    target_price = symbol_state.get(
        "entry_target_price"
    )

    remaining_potential = None

    if (
        target_price is not None
        and current is not None
        and current > 0
    ):
        remaining_potential = (
            target_price
            / current
            - 1
        ) * 100

    return {
        "current_price": current,
        "watch_price": watch_price,
        "ready_price": ready_price,
        "entry_price": entry_price,
        "target_price": target_price,
        "watch_to_now": pct_change(
            watch_price,
            current
        ),
        "ready_to_now": pct_change(
            ready_price,
            current
        ),
        "entry_to_now": pct_change(
            entry_price,
            current
        ),
        "remaining_potential": (
            remaining_potential
        ),
    }


def build_stage_block(symbol_state):
    s = stage_stats(
        symbol_state
    )

    lines = []

    if s["watch_price"] is not None:
        lines.append(
            "WATCH："
            + price_text(
                s["watch_price"]
            )
        )

    if s["ready_price"] is not None:
        lines.append(
            "READY："
            + price_text(
                s["ready_price"]
            )
        )

    if s["entry_price"] is not None:
        lines.append(
            "ENTRY："
            + price_text(
                s["entry_price"]
            )
        )

    lines.append(
        "目前："
        + price_text(
            s["current_price"]
        )
    )

    lines.append("")

    if s["watch_to_now"] is not None:
        lines.append(
            "WATCH→目前："
            + pct_text(
                s["watch_to_now"]
            )
        )

    if s["ready_to_now"] is not None:
        lines.append(
            "READY→目前："
            + pct_text(
                s["ready_to_now"]
            )
        )

    if s["entry_to_now"] is not None:
        lines.append(
            "ENTRY→目前："
            + pct_text(
                s["entry_to_now"]
            )
        )

    return "\n".join(
        lines
    )


# ============================================================
# 通知
# ============================================================

def send_entry(
    r,
    symbol_state
):
    p = (
        r.get("potential")
        or {}
    )

    stats = stage_stats(
        symbol_state
    )

    volume_ratio = p.get(
        "volume_ratio"
    )

    volume_ratio_text = (
        f"{volume_ratio:.2f}x"
        if volume_ratio is not None
        else "N/A"
    )

    msg = (
        f"股票：{r['code']} {r['name']}\n"
        f"資料代號：{r['symbol']}\n\n"

        f"{build_stage_block(symbol_state)}\n\n"

        f"✅ 1H=True\n"
        f"✅ 15m=True\n"
        f"✅ 5m=True\n"
        f"✅ 正式 ENTRY 空間 ≥"
        f"{MIN_ENTRY_SPACE_PCT:.1f}%\n\n"

        f"預估目標："
        f"{price_text(p.get('target_price'))}\n"

        f"剩餘預估空間："
        f"{pct_text(p.get('potential_pct'))}\n"

        f"潛力等級："
        f"{p.get('grade', 'N/A')}\n\n"

        f"TP1："
        f"{price_text(p.get('tp1'))}\n"

        f"TP2："
        f"{price_text(p.get('tp2'))}\n"
        f"含成本損益兩平：{price_text(p.get('breakeven_after_cost'))}\n"
        f"淨利3%出場價：{price_text(p.get('net_tp3'))}\n"
        f"淨利5%出場價：{price_text(p.get('net_tp5'))}\n"
        f"買入/賣出手續費率：{p.get('commission_rate', 0)*100:.4f}% / 邊\n"
        f"賣出證交稅率：{p.get('sell_tax_rate', 0)*100:.3f}%\n"

        f"最近壓力："
        f"{price_text(p.get('resistance'))}\n"

        f"1H ATR："
        f"{pct_text(p.get('atr_pct'))}\n\n"

        f"5m成交額：約 NT$"
        f"{money_twd(p.get('amount_5m'))}\n"

        f"近1H成交額：約 NT$"
        f"{money_twd(p.get('amount_1h'))}\n"

        f"5m量能比："
        f"{volume_ratio_text}\n\n"

        f"程式只發訊號，不自動下單。\n"
        f"請人工複核後決定是否進場。"
    )

    send_ntfy(
        f"台股 3-5% ENTRY {r['code']}",
        msg,
        "high",
        "chart_with_upwards_trend,bell"
    )


def notify_status(
    r,
    state
):
    code = r["code"]
    status = r["status"]

    symbol_state = (
        state
        .setdefault(
            "symbols",
            {}
        )
        .setdefault(
            code,
            {}
        )
    )

    previous = symbol_state.get(
        "status",
        "UNKNOWN"
    )

    # 每個台股交易日重新建立訊號週期
    today = tw_trade_date()

    if (
        symbol_state.get(
            "tw_trade_date"
        )
        != today
    ):
        reset_cycle(
            symbol_state
        )

        symbol_state[
            "status"
        ] = "UNKNOWN"

        symbol_state[
            "tw_trade_date"
        ] = today

        previous = "UNKNOWN"

    print(
        f"{code}: "
        f"{previous} -> {status}"
    )

    update_stage_memory(
        r,
        symbol_state,
        previous
    )

    if status == previous:
        return

    if status == "ENTRY":
        send_entry(
            r,
            symbol_state
        )

    elif status == "ENTRY_CHECK":
        p = (
            r.get("potential")
            or {}
        )

        send_ntfy(
            f"台股 3-5% ENTRY CHECK {code}",
            (
                f"股票：{code} "
                f"{r['name']}\n\n"

                f"{build_stage_block(symbol_state)}\n\n"

                f"1H=True\n"
                f"15m=True\n"
                f"5m=True\n\n"

                f"5m 已達標，但目前預估空間："
                f"{pct_text(p.get('potential_pct'))}\n"

                f"正式 ENTRY 最低要求："
                f"+{MIN_ENTRY_SPACE_PCT:.2f}%\n\n"

                f"5m成交額：約 NT$"
                f"{money_twd(p.get('amount_5m'))}\n"

                f"近1H成交額：約 NT$"
                f"{money_twd(p.get('amount_1h'))}\n\n"

                f"目前不列正式 ENTRY，繼續追蹤。"
            ),
            "default",
            "eyes"
        )

    elif status == "READY":
        p = (
            r.get("potential")
            or {}
        )

        send_ntfy(
            f"台股 3-5% READY {code}",
            (
                f"股票：{code} "
                f"{r['name']}\n\n"

                f"{build_stage_block(symbol_state)}\n\n"

                f"1H=True\n"
                f"15m=True\n"
                f"5m=False\n\n"

                f"目前預估目標："
                f"{price_text(p.get('target_price'))}\n"

                f"目前預估空間："
                f"{pct_text(p.get('potential_pct'))}\n"

                f"READY 最低要求："
                f"+{MIN_READY_SPACE_PCT:.2f}%\n\n"

                f"✅ 空間足夠，進入 1 分鐘快掃。\n"
                f"5m 達標後會重新估算，"
                f"剩餘空間 ≥"
                f"{MIN_ENTRY_SPACE_PCT:.1f}% "
                f"才發正式 ENTRY。"
            ),
            "default",
            "eyes"
        )

    elif status == "WATCH":
        p = (
            r.get("potential")
            or {}
        )

        if r.get(
            "15m_ready"
        ):
            detail = (
                f"1H=True\n"
                f"15m=True\n"
                f"5m=False\n\n"

                f"目前預估目標："
                f"{price_text(p.get('target_price'))}\n"

                f"目前預估空間："
                f"{pct_text(p.get('potential_pct'))}\n"

                f"READY 最低要求："
                f"+{MIN_READY_SPACE_PCT:.2f}%\n\n"

                f"⚠️ 空間不足，暫不進入快掃。\n"
                f"等待價格或上方空間改善。"
            )
        else:
            detail = (
                f"1H=True\n"
                f"15m=False\n"
                f"5m=False\n\n"
                f"等待 15m READY。"
            )

        send_ntfy(
            f"台股 3-5% WATCH {code}",
            (
                f"股票：{code} "
                f"{r['name']}\n\n"
                f"{build_stage_block(symbol_state)}\n\n"
                f"{detail}"
            ),
            "default",
            "eyes"
        )

    elif status == "NO_SIGNAL":
        if previous in (
            "WATCH",
            "READY",
            "ENTRY_CHECK",
            "ENTRY",
        ):
            send_ntfy(
                f"台股 3-5% invalid {code}",
                (
                    f"股票：{code} "
                    f"{r['name']}\n\n"
                    f"{build_stage_block(symbol_state)}\n\n"
                    f"前一狀態：{previous}\n"
                    f"目前短打環境失效。\n"
                    f"暫停做多。"
                ),
                "default",
                "warning"
            )

    symbol_state[
        "status"
    ] = status

    symbol_state[
        "updated_utc"
    ] = now_iso()


# ============================================================
# 摘要
# ============================================================

def summary_due(
    state,
    force=False
):
    if force:
        return True

    last = state.get(
        "last_summary_utc"
    )

    if not last:
        return True

    try:
        last_dt = datetime.fromisoformat(
            last
        )

        now = datetime.now(
            timezone.utc
        )

        return (
            now - last_dt
        ).total_seconds() >= SUMMARY_INTERVAL

    except Exception:
        return True


def send_summary(
    results,
    state,
    error_count,
    force=False
):
    if not summary_due(
        state,
        force=force
    ):
        return

    buckets = {
        "ENTRY": [],
        "ENTRY_CHECK": [],
        "READY": [],
        "WATCH": [],
    }

    for r in results:
        status = r.get("status")

        if status in buckets:
            label = (
                f"{r['code']} "
                f"{r['name']}"
            )

            p = r.get("potential") or {}

            if (
                status in (
                    "ENTRY",
                    "ENTRY_CHECK",
                    "READY",
                )
                and
                p.get("potential_pct")
                is not None
            ):
                label += (
                    f"({p['potential_pct']:+.1f}%)"
                )

            buckets[
                status
            ].append(
                label
            )

    def show(items):
        return (
            "、".join(items)
            if items
            else "無"
        )

    message = (
        f"[TW-STOCK]\n"
        f"ENTRY："
        f"{show(buckets['ENTRY'])}\n"
        f"ENTRY_CHECK："
        f"{show(buckets['ENTRY_CHECK'])}\n"
        f"READY："
        f"{show(buckets['READY'])}\n"
        f"WATCH："
        f"{show(buckets['WATCH'])}\n\n"
        f"本輪錯誤：{error_count}\n"
        f"主巡查：每10分鐘；"
        f"READY/ENTRY_CHECK 後每1分鐘快掃。"
    )

    success = send_ntfy(
        "台股 3-5% Monitor Summary",
        message,
        "default",
        "bar_chart"
    )

    if success:
        state[
            "last_summary_utc"
        ] = now_iso()


# ============================================================
# 執行單一結果
# ============================================================

def process_result(
    r,
    state
):
    code = r["code"]

    if r["status"] in (
        "WAIT_HISTORY",
        "STALE_DATA",
    ):
        print(
            f"{code:<6} "
            f"{r['status']}"
        )
        return

    p = (
        r.get("potential")
        or {}
    )

    extra = ""

    if p.get(
        "potential_pct"
    ) is not None:
        extra = (
            f" potential="
            f"{pct_text(p.get('potential_pct'))}"
        )

    print(
        f"{code:<6} "
        f"{r['status']:<12} "
        f"price={price_text(r['price'])} "
        f"1H={r['1h_trend']} "
        f"15m={r['15m_ready']} "
        f"5m={r['5m_entry']}"
        f"{extra}"
    )

    notify_status(
        r,
        state
    )


# ============================================================
# READY 快掃
# ============================================================

def fast_scan_ready(state):
    for round_no in range(
        1,
        FAST_SCAN_ROUNDS + 1
    ):
        candidates = []

        for code, symbol_state in (
            state
            .get(
                "symbols",
                {}
            )
            .items()
        ):
            if symbol_state.get(
                "status"
            ) not in (
                "READY",
                "ENTRY_CHECK",
            ):
                continue

            if code not in TW_STOCKS:
                continue

            candidates.append(code)

        if not candidates:
            print(
                "FAST SCAN: no READY / ENTRY_CHECK symbols"
            )
            return

        if not tw_market_open():
            print(
                "FAST SCAN: Taiwan market closed"
            )
            return

        print(
            f"\nFAST SCAN "
            f"{round_no}/{FAST_SCAN_ROUNDS} "
            f"| wait {FAST_SCAN_SECONDS}s "
            f"| symbols={','.join(candidates)}"
        )

        time.sleep(
            FAST_SCAN_SECONDS
        )

        for code in candidates:
            try:
                r = analyze(
                    code,
                    TW_STOCKS[code]
                )

                process_result(
                    r,
                    state
                )

                save_state(
                    state
                )

            except Exception as e:
                print(
                    f"{code}: "
                    f"FAST SCAN ERROR {e}"
                )

            time.sleep(
                0.15
            )


# ============================================================
# MAIN
# ============================================================

def main():
    print(
        "Taiwan Stock 3~5% Monitor | v1.0"
    )

    print(
        "UTC:",
        now_iso()
    )

    print(
        "TW market open:",
        tw_market_open()
    )

    print(
        "Manual run:",
        MANUAL_RUN
    )

    state = load_state()

    # 非台股交易時間，手動執行只顯示狀態，不掃市場。
    if not tw_market_open():
        print(
            "TW-STOCK SKIP: outside "
            "09:00-13:30 Taipei time"
        )
        save_state(state)
        return

    results = []
    errors = []

    for code, yahoo_symbol in TW_STOCKS.items():
        try:
            r = analyze(
                code,
                yahoo_symbol
            )

            results.append(r)

            process_result(
                r,
                state
            )

        except Exception as e:
            errors.append(
                (
                    code,
                    str(e)
                )
            )

            print(
                f"{code:<6} ERROR {e}"
            )

        time.sleep(
            0.15
        )

    send_summary(
        results,
        state,
        len(errors),
        force=MANUAL_RUN
    )

    save_state(
        state
    )

    # READY 已先通過 2.5% 空間門檻。
    # 才留下 runner 做每分鐘快掃。
    fast_scan_ready(
        state
    )

    save_state(
        state
    )

    print(
        "\nERROR COUNT:",
        len(errors)
    )

    print(
        "STATE FILE:",
        STATE_FILE
    )


if __name__ == "__main__":
    main()
