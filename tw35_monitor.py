#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
TW35 Monitor v1.2
=================

台股 3~5% 趨勢短打雷達

狀態：
NO_SIGNAL
WATCH
READY
ENTRY

核心：
1H  -> 主趨勢
15m -> READY
5m  -> ENTRY + 量能

功能：
- WATCH / READY / ENTRY 階段價格記憶
- 目前價
- 各階段 -> 目前 漲跌幅
- ENTRY 原始預估目標
- 原始預估空間
- 剩餘預估空間
- TP1 / TP2
- 最近壓力
- 1H ATR
- 5m量能比
- ntfy通知
- 30分鐘摘要
- 手動Run立即摘要

台股專用：
- 09:00~13:30盤中才允許ENTRY通知
- 接近漲停不追
- 開盤大幅跳空時前30分鐘不追
- 收盤後可手動測試，但不發正式ENTRY進場通知
- 自動抓最近實際交易日的開盤跳空

v1.2：
- 監控池擴充為20檔
- 電子 / 金融 / 航運 / 傳產 / 民生 / 電信 / ETF

資料：
Yahoo Finance 公開圖表資料
不需API Key
不自動下單
"""

import json
import os
import time
import urllib.parse
import urllib.request

from datetime import datetime, timezone, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo


# ============================================================
# 基本設定
# ============================================================

TAIPEI = ZoneInfo("Asia/Taipei")

YAHOO_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"

SUMMARY_INTERVAL = 30 * 60

STATE_DIR = Path(".tw35_state")
STATE_FILE = STATE_DIR / "state.json"


# ============================================================
# 20檔跨產業監控池
# ============================================================

SYMBOLS = {

    # ========================================================
    # 電子 / 科技
    # ========================================================

    "2330": {
        "name": "台積電",
        "ticker": "2330.TW",
    },

    "2454": {
        "name": "聯發科",
        "ticker": "2454.TW",
    },

    "2308": {
        "name": "台達電",
        "ticker": "2308.TW",
    },

    "3017": {
        "name": "奇鋐",
        "ticker": "3017.TW",
    },

    "2317": {
        "name": "鴻海",
        "ticker": "2317.TW",
    },


    # ========================================================
    # 金融
    # ========================================================

    "2881": {
        "name": "富邦金",
        "ticker": "2881.TW",
    },

    "2882": {
        "name": "國泰金",
        "ticker": "2882.TW",
    },

    "2884": {
        "name": "玉山金",
        "ticker": "2884.TW",
    },

    "2886": {
        "name": "兆豐金",
        "ticker": "2886.TW",
    },


    # ========================================================
    # 航運 / 航空
    # ========================================================

    "2603": {
        "name": "長榮",
        "ticker": "2603.TW",
    },

    "2609": {
        "name": "陽明",
        "ticker": "2609.TW",
    },

    "2615": {
        "name": "萬海",
        "ticker": "2615.TW",
    },

    "2610": {
        "name": "華航",
        "ticker": "2610.TW",
    },


    # ========================================================
    # 傳產 / 原物料
    # ========================================================

    "2002": {
        "name": "中鋼",
        "ticker": "2002.TW",
    },

    "1301": {
        "name": "台塑",
        "ticker": "1301.TW",
    },

    "1303": {
        "name": "南亞",
        "ticker": "1303.TW",
    },


    # ========================================================
    # 民生 / 電信
    # ========================================================

    "1216": {
        "name": "統一",
        "ticker": "1216.TW",
    },

    "2412": {
        "name": "中華電",
        "ticker": "2412.TW",
    },


    # ========================================================
    # ETF 基準
    # ========================================================

    "0050": {
        "name": "元大台灣50",
        "ticker": "0050.TW",
    },

    "0056": {
        "name": "元大高股息",
        "ticker": "0056.TW",
    },
}


# ============================================================
# NTFY
# ============================================================

NTFY_SERVER = os.getenv(
    "NTFY_SERVER",
    "https://ntfy.sh"
).rstrip("/")

NTFY_TOPIC = os.getenv(
    "NTFY_TOPIC_TW35",
    ""
).strip()


# ============================================================
# GitHub
# ============================================================

GITHUB_EVENT_NAME = os.getenv(
    "GITHUB_EVENT_NAME",
    ""
).strip()

MANUAL_RUN = (
    GITHUB_EVENT_NAME == "workflow_dispatch"
)


# ============================================================
# 時間
# ============================================================

def now_utc():
    return datetime.now(timezone.utc)


def now_taipei():
    return datetime.now(TAIPEI)


def now_iso():
    return now_utc().isoformat()


def market_open_now():

    now = now_taipei()

    if now.weekday() >= 5:
        return False

    t = now.time()

    return (
        dt_time(9, 0)
        <= t
        <= dt_time(13, 30)
    )


def early_session():

    now = now_taipei()

    if now.weekday() >= 5:
        return False

    t = now.time()

    return (
        dt_time(9, 0)
        <= t
        < dt_time(9, 30)
    )


# ============================================================
# 顯示工具
# ============================================================

def price_text(v):

    if v is None:
        return "N/A"

    if v >= 1000:
        return f"{v:.1f}"

    if v >= 100:
        return f"{v:.2f}"

    if v >= 10:
        return f"{v:.2f}"

    return f"{v:.3f}"


def pct_text(v):

    if v is None:
        return "N/A"

    return f"{v:+.2f}%"


def pct_change(start, end):

    if (
        start is None
        or end is None
        or start <= 0
    ):
        return None

    return (
        end / start - 1
    ) * 100


# ============================================================
# Yahoo API
# ============================================================

def yahoo_get(
    ticker,
    interval,
    range_,
    retries=4
):

    params = urllib.parse.urlencode({
        "interval": interval,
        "range": range_,
        "includePrePost": "false",
        "events": "div,splits",
    })

    url = (
        f"{YAHOO_BASE}/{ticker}"
        f"?{params}"
    )

    last_error = None

    for attempt in range(retries):

        try:

            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent":
                        "Mozilla/5.0 TW35-Monitor/1.2",

                    "Accept":
                        "application/json",
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

                raise RuntimeError(
                    f"No Yahoo data: {ticker}"
                )

            return result[0]

        except Exception as e:

            last_error = e

            time.sleep(
                min(
                    2 ** attempt,
                    6
                )
            )

    raise RuntimeError(
        f"Yahoo request failed "
        f"{ticker}: {last_error}"
    )


# ============================================================
# Yahoo K線轉換
# ============================================================

def parse_chart(result):

    timestamps = result.get(
        "timestamp",
        []
    ) or []

    indicators = result.get(
        "indicators",
        {}
    )

    quotes = indicators.get(
        "quote",
        []
    )

    if not quotes:
        return []

    q = quotes[0]

    opens = q.get("open", [])
    highs = q.get("high", [])
    lows = q.get("low", [])
    closes = q.get("close", [])
    volumes = q.get("volume", [])

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

        rows.append({
            "t": int(ts),
            "o": float(o),
            "h": float(h),
            "l": float(l),
            "c": float(c),
            "v": float(v or 0),
        })

    rows.sort(
        key=lambda x: x["t"]
    )

    return rows


def fetch_chart(
    ticker,
    interval,
    range_
):

    result = yahoo_get(
        ticker,
        interval,
        range_
    )

    rows = parse_chart(
        result
    )

    meta = result.get(
        "meta",
        {}
    )

    return rows, meta


# ============================================================
# 交易日工具
# ============================================================

def row_local_date(row):

    return (
        datetime
        .fromtimestamp(
            row["t"],
            timezone.utc
        )
        .astimezone(
            TAIPEI
        )
        .date()
    )


def group_rows_by_date(rows):

    groups = {}

    for r in rows:

        d = row_local_date(r)

        groups.setdefault(
            d,
            []
        ).append(r)

    return groups


def latest_trade_day_info(
    r5,
    rd
):

    if not r5:

        return {
            "trade_date": None,
            "open_price": None,
            "previous_close": None,
            "gap_pct": None,
        }

    intraday_groups = (
        group_rows_by_date(
            r5
        )
    )

    trade_dates = sorted(
        intraday_groups.keys()
    )

    if not trade_dates:

        return {
            "trade_date": None,
            "open_price": None,
            "previous_close": None,
            "gap_pct": None,
        }

    latest_trade_date = (
        trade_dates[-1]
    )

    latest_day_rows = (
        intraday_groups[
            latest_trade_date
        ]
    )

    latest_day_rows.sort(
        key=lambda x: x["t"]
    )

    open_price = (
        latest_day_rows[0]["o"]
        if latest_day_rows
        else None
    )

    daily_by_date = {}

    for r in rd:

        d = row_local_date(r)

        daily_by_date[d] = r

    earlier_daily_dates = sorted(
        [
            d
            for d in daily_by_date.keys()
            if d < latest_trade_date
        ]
    )

    previous_close = None

    if earlier_daily_dates:

        prev_date = (
            earlier_daily_dates[-1]
        )

        previous_close = (
            daily_by_date[
                prev_date
            ]["c"]
        )

    gap_pct = (
        pct_change(
            previous_close,
            open_price
        )
        if (
            previous_close is not None
            and open_price is not None
        )
        else None
    )

    return {
        "trade_date":
            latest_trade_date,

        "open_price":
            open_price,

        "previous_close":
            previous_close,

        "gap_pct":
            gap_pct,
    }


# ============================================================
# 均線與ATR
# ============================================================

def sma(values, n, i):

    if i + 1 < n:
        return None

    return sum(
        values[
            i - n + 1:
            i + 1
        ]
    ) / n


def true_range(
    current,
    previous
):

    if previous is None:

        return (
            current["h"]
            - current["l"]
        )

    return max(
        current["h"] - current["l"],
        abs(
            current["h"]
            - previous["c"]
        ),
        abs(
            current["l"]
            - previous["c"]
        ),
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

        r["ma5"] = sma(
            closes,
            5,
            i
        )

        r["ma10"] = sma(
            closes,
            10,
            i
        )

        r["ma20"] = sma(
            closes,
            20,
            i
        )

        r["ma60"] = sma(
            closes,
            60,
            i
        )

        r["ma20_prev"] = (
            sma(
                closes,
                20,
                i - 1
            )
            if i >= 20
            else None
        )

        r["vma5"] = sma(
            volumes,
            5,
            i
        )

        r["vma20"] = sma(
            volumes,
            20,
            i
        )

        r["atr14"] = sma(
            trs,
            14,
            i
        )


# ============================================================
# 訊號條件
# ============================================================

def one_hour_trend(r):

    needed = [
        r.get("ma5"),
        r.get("ma10"),
        r.get("ma20"),
        r.get("ma20_prev"),
    ]

    if any(
        x is None
        for x in needed
    ):
        return False

    return (
        r["c"] > r["ma20"]

        and

        r["ma20"]
        > r["ma20_prev"]

        and

        r["ma5"]
        > r["ma10"]

        and

        r["ma10"]
        > r["ma20"]
    )


def fifteen_ready(r):

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
        r["c"]
        >= r["ma20"] * 0.995

        and

        r["ma5"]
        >= r["ma10"] * 0.995
    )


def five_entry(
    current,
    previous
):

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
        current["ma5"]
        > current["ma10"]

        and

        previous["ma5"]
        <= previous["ma10"]
    )

    already_strong = (
        current["ma5"]
        > current["ma10"]

        and

        current["c"]
        > current["ma20"]
    )

    volume_ok = (
        current["vma5"]
        > current["vma20"]
    )

    return (
        (
            cross_up
            or already_strong
        )

        and

        volume_ok
    )


# ============================================================
# 最近上方壓力
# ============================================================

def recent_resistance(
    price,
    r1,
    daily
):

    candidates = []

    for r in r1[-30:]:

        if r["h"] > price:

            candidates.append(
                r["h"]
            )

    for r in daily[-60:]:

        if r["h"] > price:

            candidates.append(
                r["h"]
            )

    if not candidates:
        return None

    return min(
        candidates
    )


# ============================================================
# ENTRY 潛力
# ============================================================

def estimate_potential(
    r1,
    daily,
    latest5
):

    latest1 = r1[-1]

    entry_price = latest5["c"]

    resistance = recent_resistance(
        entry_price,
        r1,
        daily
    )

    atr = latest1.get(
        "atr14"
    )

    atr_pct = None

    if (
        atr is not None
        and entry_price > 0
    ):

        atr_pct = (
            atr / entry_price
        ) * 100


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


    atr_target_pct = None

    if atr_pct is not None:

        atr_target_pct = (
            atr_pct * 2.0
        )


    volume_ratio = None

    if (
        latest5.get("vma5")
        is not None

        and

        latest5.get("vma20")
        not in (
            None,
            0
        )
    ):

        volume_ratio = (
            latest5["vma5"]
            /
            latest5["vma20"]
        )


    trend_bonus = 0.0

    if (
        latest1.get("ma5")
        is not None

        and

        latest1.get("ma10")
        is not None

        and

        latest1.get("ma20")
        is not None
    ):

        if (
            latest1["ma5"]
            > latest1["ma10"]
            > latest1["ma20"]
        ):

            trend_bonus += 0.4


    if volume_ratio is not None:

        if volume_ratio >= 1.5:

            trend_bonus += 0.7

        elif volume_ratio >= 1.2:

            trend_bonus += 0.3


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


    potential_pct = (
        base_potential
        + trend_bonus
    )

    potential_pct = max(
        0.5,
        min(
            potential_pct,
            8.0
        )
    )


    target_price = (
        entry_price
        * (
            1
            + potential_pct / 100
        )
    )


    tp1_pct = min(
        potential_pct,
        3.0
    )

    tp2_pct = min(
        potential_pct,
        5.0
    )


    tp1 = (
        entry_price
        * (
            1
            + tp1_pct / 100
        )
    )

    tp2 = (
        entry_price
        * (
            1
            + tp2_pct / 100
        )
    )


    if potential_pct >= 4.5:

        grade = "HIGH"

    elif potential_pct >= 3.0:

        grade = "OK"

    else:

        grade = "LOW"


    return {
        "entry_price":
            entry_price,

        "target_price":
            target_price,

        "potential_pct":
            potential_pct,

        "grade":
            grade,

        "resistance":
            resistance,

        "resistance_pct":
            resistance_pct,

        "atr_pct":
            atr_pct,

        "volume_ratio":
            volume_ratio,

        "tp1":
            tp1,

        "tp2":
            tp2,
    }


# ============================================================
# 單檔分析
# ============================================================

def analyze(
    code,
    info
):

    ticker = info["ticker"]
    name = info["name"]


    r5, meta5 = fetch_chart(
        ticker,
        "5m",
        "5d"
    )

    r15, _ = fetch_chart(
        ticker,
        "15m",
        "5d"
    )

    r1, _ = fetch_chart(
        ticker,
        "60m",
        "1mo"
    )

    rd, _ = fetch_chart(
        ticker,
        "1d",
        "3mo"
    )


    if (
        len(r5) < 25
        or
        len(r15) < 25
        or
        len(r1) < 25
        or
        len(rd) < 10
    ):

        return {
            "code": code,
            "name": name,
            "ticker": ticker,
            "status": "WAIT_HISTORY",
        }


    add_indicators(r5)
    add_indicators(r15)
    add_indicators(r1)
    add_indicators(rd)


    latest5 = r5[-1]
    previous5 = r5[-2]

    latest15 = r15[-1]
    latest1 = r1[-1]


    current_price = (
        meta5.get(
            "regularMarketPrice"
        )
    )


    if current_price is None:

        current_price = (
            latest5["c"]
        )


    current_price = float(
        current_price
    )


    # ========================================================
    # 最近實際交易日資料
    # ========================================================

    trade_info = (
        latest_trade_day_info(
            r5,
            rd
        )
    )


    previous_close = (
        trade_info[
            "previous_close"
        ]
    )

    first_open = (
        trade_info[
            "open_price"
        ]
    )

    gap_pct = (
        trade_info[
            "gap_pct"
        ]
    )

    trade_date = (
        trade_info[
            "trade_date"
        ]
    )


    day_change_pct = (
        pct_change(
            previous_close,
            current_price
        )
        if previous_close
        else None
    )


    # ========================================================
    # 訊號
    # ========================================================

    h1 = one_hour_trend(
        latest1
    )

    m15 = fifteen_ready(
        latest15
    )

    m5 = five_entry(
        latest5,
        previous5
    )


    if (
        h1
        and m15
        and m5
    ):

        status = "ENTRY"

    elif (
        h1
        and m15
    ):

        status = "READY"

    elif h1:

        status = "WATCH"

    else:

        status = "NO_SIGNAL"


    # ========================================================
    # 台股風控
    # ========================================================

    near_limit_up = (
        day_change_pct is not None
        and
        day_change_pct >= 8.5
    )


    early_gap_block = (
        gap_pct is not None
        and
        gap_pct >= 3.0
        and
        early_session()
    )


    market_open = (
        market_open_now()
    )


    entry_allowed = (
        status == "ENTRY"

        and

        market_open

        and

        not near_limit_up

        and

        not early_gap_block
    )


    block_reason = None


    if (
        status == "ENTRY"
        and
        not market_open
    ):

        block_reason = (
            "目前非台股盤中"
        )


    elif (
        status == "ENTRY"
        and
        near_limit_up
    ):

        block_reason = (
            "接近漲停區，不追價"
        )


    elif (
        status == "ENTRY"
        and
        early_gap_block
    ):

        block_reason = (
            "開盤跳空>=3%，09:30前不追"
        )


    potential = None

    if status == "ENTRY":

        potential = estimate_potential(
            r1,
            rd,
            latest5
        )


    return {
        "code":
            code,

        "name":
            name,

        "ticker":
            ticker,

        "status":
            status,

        "price":
            current_price,

        "1h":
            h1,

        "15m":
            m15,

        "5m":
            m5,

        "trade_date":
            str(trade_date)
            if trade_date
            else None,

        "first_open":
            first_open,

        "previous_close":
            previous_close,

        "day_change_pct":
            day_change_pct,

        "gap_pct":
            gap_pct,

        "market_open":
            market_open,

        "near_limit_up":
            near_limit_up,

        "early_gap_block":
            early_gap_block,

        "entry_allowed":
            entry_allowed,

        "block_reason":
            block_reason,

        "potential":
            potential,
    }


# ============================================================
# ntfy
# ============================================================

def send_ntfy(
    title,
    message,
    priority="default",
    tags="bell"
):

    if not NTFY_TOPIC:

        print(
            "NTFY_TOPIC_TW35 未設定"
        )

        return False


    req = urllib.request.Request(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=message.encode(
            "utf-8"
        ),
        method="POST",
        headers={
            "Title":
                title,

            "Priority":
                priority,

            "Tags":
                tags,

            "Content-Type":
                "text/plain; charset=utf-8",
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


    except Exception:

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


# ============================================================
# 新週期清除
# ============================================================

def reset_cycle(s):

    keys = [
        "watch_price",
        "watch_time",
        "ready_price",
        "ready_time",
        "entry_price",
        "entry_time",
        "entry_target_price",
        "entry_original_potential_pct",
        "entry_grade",
        "entry_tp1",
        "entry_tp2",
        "entry_resistance",
        "entry_atr_pct",
        "entry_volume_ratio",
    ]


    for key in keys:

        s.pop(
            key,
            None
        )


# ============================================================
# 階段價格記憶
# ============================================================

def update_stage_memory(
    r,
    s,
    previous
):

    status = r["status"]

    price = r["price"]


    s["current_price"] = price

    s["last_seen_utc"] = (
        now_iso()
    )


    if (
        previous
        in (
            "NO_SIGNAL",
            "UNKNOWN"
        )

        and

        status
        in (
            "WATCH",
            "READY",
            "ENTRY"
        )
    ):

        reset_cycle(s)

        s["current_price"] = price


    if (
        status
        in (
            "WATCH",
            "READY",
            "ENTRY"
        )

        and

        s.get(
            "watch_price"
        )
        is None
    ):

        s["watch_price"] = price
        s["watch_time"] = now_iso()


    if (
        status
        in (
            "READY",
            "ENTRY"
        )

        and

        s.get(
            "ready_price"
        )
        is None
    ):

        s["ready_price"] = price
        s["ready_time"] = now_iso()


    if (
        status == "ENTRY"

        and

        s.get(
            "entry_price"
        )
        is None
    ):

        p = (
            r.get("potential")
            or {}
        )

        s["entry_price"] = price

        s["entry_time"] = now_iso()

        s["entry_target_price"] = (
            p.get("target_price")
        )

        s[
            "entry_original_potential_pct"
        ] = p.get(
            "potential_pct"
        )

        s["entry_grade"] = (
            p.get("grade")
        )

        s["entry_tp1"] = (
            p.get("tp1")
        )

        s["entry_tp2"] = (
            p.get("tp2")
        )

        s["entry_resistance"] = (
            p.get("resistance")
        )

        s["entry_atr_pct"] = (
            p.get("atr_pct")
        )

        s["entry_volume_ratio"] = (
            p.get("volume_ratio")
        )


# ============================================================
# 階段統計
# ============================================================

def stage_stats(s):

    current = s.get(
        "current_price"
    )

    watch = s.get(
        "watch_price"
    )

    ready = s.get(
        "ready_price"
    )

    entry = s.get(
        "entry_price"
    )

    target = s.get(
        "entry_target_price"
    )


    remaining = None

    if (
        target is not None
        and
        current is not None
        and
        current > 0
    ):

        remaining = (
            target / current - 1
        ) * 100


    return {
        "current":
            current,

        "watch":
            watch,

        "ready":
            ready,

        "entry":
            entry,

        "target":
            target,

        "watch_to_now":
            pct_change(
                watch,
                current
            ),

        "ready_to_now":
            pct_change(
                ready,
                current
            ),

        "entry_to_now":
            pct_change(
                entry,
                current
            ),

        "remaining":
            remaining,
    }


# ============================================================
# 顯示階段
# ============================================================

def stage_block(s):

    x = stage_stats(s)

    lines = []


    if x["watch"] is not None:

        lines.append(
            "WATCH："
            + price_text(
                x["watch"]
            )
        )


    if x["ready"] is not None:

        lines.append(
            "READY："
            + price_text(
                x["ready"]
            )
        )


    if x["entry"] is not None:

        lines.append(
            "ENTRY："
            + price_text(
                x["entry"]
            )
        )


    lines.append(
        "目前："
        + price_text(
            x["current"]
        )
    )


    lines.append("")


    if x["watch_to_now"] is not None:

        lines.append(
            "WATCH→目前："
            + pct_text(
                x["watch_to_now"]
            )
        )


    if x["ready_to_now"] is not None:

        lines.append(
            "READY→目前："
            + pct_text(
                x["ready_to_now"]
            )
        )


    if x["entry_to_now"] is not None:

        lines.append(
            "ENTRY→目前："
            + pct_text(
                x["entry_to_now"]
            )
        )


    return "\n".join(lines)


# ============================================================
# ENTRY 通知
# ============================================================

def send_entry(
    r,
    s
):

    stats = stage_stats(s)

    original_pct = s.get(
        "entry_original_potential_pct"
    )

    target = s.get(
        "entry_target_price"
    )

    grade = s.get(
        "entry_grade",
        "N/A"
    )

    tp1 = s.get(
        "entry_tp1"
    )

    tp2 = s.get(
        "entry_tp2"
    )

    resistance = s.get(
        "entry_resistance"
    )

    atr = s.get(
        "entry_atr_pct"
    )

    volume = s.get(
        "entry_volume_ratio"
    )


    volume_text = (
        f"{volume:.2f}"
        if volume is not None
        else "N/A"
    )


    message = (
        f"{r['code']} {r['name']}\n\n"

        f"{stage_block(s)}\n\n"

        f"交易日："
        f"{r['trade_date']}\n"

        f"前收："
        f"{price_text(r['previous_close'])}\n"

        f"開盤："
        f"{price_text(r['first_open'])}\n"

        f"開盤跳空："
        f"{pct_text(r['gap_pct'])}\n"

        f"當日漲跌："
        f"{pct_text(r['day_change_pct'])}\n\n"

        f"原始預估目標："
        f"{price_text(target)}\n"

        f"原始預估空間："
        f"{pct_text(original_pct)}\n"

        f"剩餘預估空間："
        f"{pct_text(stats['remaining'])}\n"

        f"潛力等級：{grade}\n\n"

        f"TP1：{price_text(tp1)}\n"

        f"TP2：{price_text(tp2)}\n"

        f"最近壓力："
        f"{price_text(resistance)}\n\n"

        f"1H ATR："
        f"{pct_text(atr)}\n"

        f"5m量能比："
        f"{volume_text}\n"
    )


    if r["entry_allowed"]:

        message += (
            "\nENTRY有效，人工複核後決定進場。"
        )

        send_ntfy(
            f"TW35 ENTRY {r['code']}",
            message,
            "high",
            "chart_with_upwards_trend,bell"
        )


    else:

        message += (
            f"\nENTRY條件成立但暫不追："
            f"{r['block_reason']}"
        )

        send_ntfy(
            f"TW35 ENTRY BLOCK {r['code']}",
            message,
            "default",
            "warning"
        )


# ============================================================
# 狀態通知
# ============================================================

def notify_status(
    r,
    state
):

    code = r["code"]

    status = r["status"]


    s = (
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


    previous = s.get(
        "status",
        "UNKNOWN"
    )


    print(
        f"{code} {r['name']}: "
        f"{previous} -> {status}"
    )


    update_stage_memory(
        r,
        s,
        previous
    )


    if status == previous:
        return


    if status == "ENTRY":

        send_entry(
            r,
            s
        )


    elif status == "READY":

        send_ntfy(
            f"TW35 READY {code}",
            (
                f"{code} {r['name']}\n\n"

                f"{stage_block(s)}\n\n"

                f"交易日："
                f"{r['trade_date']}\n"

                f"前收："
                f"{price_text(r['previous_close'])}\n"

                f"開盤："
                f"{price_text(r['first_open'])}\n"

                f"跳空："
                f"{pct_text(r['gap_pct'])}\n\n"

                f"1H=True\n"
                f"15m=True\n"
                f"5m=False\n\n"

                f"等待5m ENTRY。"
            ),
            "default",
            "eyes"
        )


    elif status == "WATCH":

        send_ntfy(
            f"TW35 WATCH {code}",
            (
                f"{code} {r['name']}\n\n"

                f"{stage_block(s)}\n\n"

                f"交易日："
                f"{r['trade_date']}\n"

                f"前收："
                f"{price_text(r['previous_close'])}\n"

                f"開盤："
                f"{price_text(r['first_open'])}\n"

                f"跳空："
                f"{pct_text(r['gap_pct'])}\n\n"

                f"1H=True\n"
                f"15m=False\n\n"

                f"等待READY。"
            ),
            "default",
            "eyes"
        )


    elif status == "NO_SIGNAL":

        if previous in (
            "WATCH",
            "READY",
            "ENTRY"
        ):

            send_ntfy(
                f"TW35 INVALID {code}",
                (
                    f"{code} {r['name']}\n\n"

                    f"{stage_block(s)}\n\n"

                    f"前一狀態："
                    f"{previous}\n"

                    f"短打環境失效。"
                ),
                "default",
                "warning"
            )


    s["status"] = status

    s["updated_utc"] = (
        now_iso()
    )


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

        last_dt = (
            datetime
            .fromisoformat(last)
        )

        return (
            (
                now_utc()
                - last_dt
            ).total_seconds()
            >= SUMMARY_INTERVAL
        )

    except Exception:

        return True


def send_summary(
    results,
    state,
    errors,
    force=False
):

    if not summary_due(
        state,
        force
    ):

        print(
            "SUMMARY: not due"
        )

        return


    groups = {
        "ENTRY": [],
        "READY": [],
        "WATCH": [],
    }


    for r in results:

        status = r.get(
            "status"
        )

        if status not in groups:
            continue


        code = r["code"]
        name = r["name"]


        if status == "ENTRY":

            s = (
                state
                .get(
                    "symbols",
                    {}
                )
                .get(
                    code,
                    {}
                )
            )

            remaining = (
                stage_stats(s)
                .get("remaining")
            )

            if remaining is not None:

                groups["ENTRY"].append(
                    f"{code}{name}"
                    f"({remaining:+.1f}%)"
                )

            else:

                groups["ENTRY"].append(
                    f"{code}{name}"
                )


        else:

            groups[status].append(
                f"{code}{name}"
            )


    def join(items):

        return (
            "、".join(items)
            if items
            else "無"
        )


    message = (
        "台股3~5%雷達\n\n"

        f"ENTRY："
        f"{join(groups['ENTRY'])}\n"

        f"READY："
        f"{join(groups['READY'])}\n"

        f"WATCH："
        f"{join(groups['WATCH'])}\n\n"

        f"監控標的："
        f"{len(SYMBOLS)}檔\n"

        f"目前盤中："
        f"{market_open_now()}\n"

        f"錯誤數："
        f"{len(errors)}"
    )


    if send_ntfy(
        "TW35 Monitor Summary",
        message,
        "default",
        "bar_chart"
    ):

        state[
            "last_summary_utc"
        ] = now_iso()


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "TW35 Monitor | v1.2"
    )

    print(
        "Cross-sector symbols:",
        len(SYMBOLS)
    )

    print(
        "Taipei:",
        now_taipei().isoformat()
    )

    print(
        "Market open:",
        market_open_now()
    )

    print(
        "Manual run:",
        MANUAL_RUN
    )


    state = load_state()

    results = []

    errors = []


    for code, info in SYMBOLS.items():

        try:

            r = analyze(
                code,
                info
            )

            results.append(r)


            if (
                r["status"]
                == "WAIT_HISTORY"
            ):

                print(
                    f"{code} "
                    f"{info['name']} "
                    f"WAIT_HISTORY"
                )

                continue


            print(
                f"{code} "
                f"{info['name']:<8} "

                f"{r['status']:<10} "

                f"price="
                f"{price_text(r['price'])} "

                f"trade_date="
                f"{r['trade_date']} "

                f"prev="
                f"{price_text(r['previous_close'])} "

                f"open="
                f"{price_text(r['first_open'])} "

                f"gap="
                f"{pct_text(r['gap_pct'])} "

                f"day="
                f"{pct_text(r['day_change_pct'])} "

                f"1H="
                f"{r['1h']} "

                f"15m="
                f"{r['15m']} "

                f"5m="
                f"{r['5m']} "

                f"allowed="
                f"{r['entry_allowed']}"
            )


            notify_status(
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
                f"{code} "
                f"{info['name']} "
                f"ERROR {e}"
            )


        time.sleep(
            0.3
        )


    send_summary(
        results,
        state,
        errors,
        force=MANUAL_RUN
    )


    save_state(
        state
    )


    counts = {}


    for r in results:

        status = r.get(
            "status",
            "UNKNOWN"
        )

        counts[status] = (
            counts.get(
                status,
                0
            )
            + 1
        )


    print(
        "\nSTATUS COUNTS:",
        counts
    )

    print(
        "SYMBOL COUNT:",
        len(SYMBOLS)
    )

    print(
        "ERROR COUNT:",
        len(errors)
    )

    print(
        "STATE FILE:",
        STATE_FILE
    )


if __name__ == "__main__":
    main()
