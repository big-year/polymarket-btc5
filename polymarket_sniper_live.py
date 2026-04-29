#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Polymarket BTC Up/Down 5m — CLOB V2 修复版
================================================

修复重点：
1. 适配 2026-04-28 Polymarket CLOB V2 升级；
2. 使用 py_clob_client_v2，不再使用旧 py_clob_client；
3. 实盘买入/卖出使用 create_and_post_market_order()；
4. FAK/FOK 按 V2 market order 处理；
5. 修复 order_version_mismatch；
6. 保留原策略：最后 20~60 秒，只买高概率 ask 区间方向；
7. 保留 0.99 bid 止盈；
8. 保留 WS 盘口缓存、REST 盘口兜底、持仓结算逻辑。

安装：
    pip uninstall py-clob-client -y
    pip install -U py-clob-client-v2 python-dotenv requests websockets

.env 示例：
    POLY_PRIVATE_KEY=你的私钥
    POLY_FUNDER=你的 proxy/funder 钱包地址
    POLY_SIGNATURE_TYPE=1
    POLY_CLOB_HOST=https://clob.polymarket.com

如果你已有 CLOB API Key，也可以添加：
    CLOB_API_KEY=xxx
    CLOB_SECRET=xxx
    CLOB_PASS_PHRASE=xxx

实盘确认：
    在当前目录创建 live_trading_confirm.txt
    内容必须精确为：
    ENABLE_LIVE_TRADING
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import sys
import time
import traceback
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from dataclasses import asdict, dataclass, field
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

try:
    from py_clob_client_v2 import (
        ApiCreds,
        ClobClient,
        OrderType,
        PartialCreateOrderOptions,
        Side,
        MarketOrderArgs,
        BalanceAllowanceParams,
        AssetType,
    )
except ImportError:
    ApiCreds = None
    ClobClient = None
    OrderType = None
    PartialCreateOrderOptions = None
    Side = None
    MarketOrderArgs = None
    BalanceAllowanceParams = None
    AssetType = None

try:
    import websockets
    from websockets.exceptions import ConnectionClosed, WebSocketException
except ImportError:
    print("请先安装依赖：pip install websockets")
    sys.exit(1)


# ─────────────────────────────────────────────────────────────
# 颜色与日志
# ─────────────────────────────────────────────────────────────

RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
WHITE = "\033[97m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


def c(text: str, color: str) -> str:
    return f"{color}{text}{RESET}"


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def now_iso() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def utc_now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def log(msg: str, color: str = WHITE) -> None:
    print(f"{c(ts(), CYAN)} {c(msg, color)}", flush=True)


# ─────────────────────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────────────────────

@dataclass
class Config:
    PROGRAM_NAME: str = "Polymarket BTC 5m Favorite Side Sniper CLOB V2 修复版"
    PAPER_MODE: bool = False

    # 实盘
    LIVE_TRADING_ENABLED: bool = True
    LIVE_CONFIRM_FILE: str = "live_trading_confirm.txt"
    LIVE_CONFIRM_TEXT: str = "ENABLE_LIVE_TRADING"
    LIVE_CHAIN_ID: int = 137
    LIVE_SIGNATURE_TYPE: int = 1
    LIVE_ORDER_TYPE: str = "FAK"
    LIVE_BUY_SLIPPAGE: float = 0.03
    LIVE_SELL_SLIPPAGE: float = 0.01
    LIVE_DEFAULT_TICK_SIZE: float = 0.01

    # 市场
    GAMMA_BASE_URL: str = "https://gamma-api.polymarket.com"
    CLOB_BASE_URL: str = "https://clob.polymarket.com"
    WINDOW_SECONDS: int = 300
    MARKET_SLUG_PREFIX: str = "btc-updown-5m"
    MARKET_SEARCH_OFFSETS: Tuple[int, ...] = (0, 300, -300, 600, -600)
    MARKET_REFRESH_SEC: int = 8
    HTTP_TIMEOUT: int = 10
    HTTP_RETRIES: int = 3
    HTTP_RETRY_SLEEP: float = 0.5

    # WS
    WS_URL: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    WS_PING_INTERVAL: float = 20.0
    WS_PING_TIMEOUT: float = 10.0
    WS_RECONNECT_BASE: float = 1.0
    WS_RECONNECT_MAX: float = 30.0
    WS_RECONNECT_FACTOR: float = 2.0

    # 策略
    SNIPE_WINDOW_MAX: int = 50
    SNIPE_WINDOW_MIN: int = 10

    # 手续费估算
    FEE_THETA: float = 0.05
    TAKER_REBATE_RATE: float = 0.50
    MIN_NET_PROFIT_U: float = 0.02

    # 账户
    INIT_EQUITY: float = 19.0
    ORDER_BUDGET_U: float = 18.0
    MIN_TRADE_BUDGET_U: float = 2.0

    # 盘口过滤
    MAX_SPREAD: float = 0.08
    MIN_ENTRY_ASK: float = 0.86
    MAX_ENTRY_ASK: float = 0.93
    MIN_BID_DEPTH_SHARES: float = 10.0
    MIN_CROWD_RATIO: float = 1.30

    # 风控
    ENABLE_MID_EXIT_STOP_LOSS: bool = False
    STOP_LOSS_U: float = 3.0
    DAILY_MAX_LOSS_U: float = 15.0
    MAX_CONSECUTIVE_LOSSES: int = 5
    MAX_TRADES_PER_DAY: int = 288
    ONLY_ONE_TRADE_PER_WINDOW: bool = True
    COOLDOWN_AFTER_TRADE_SEC: int = 5

    # 0.98 止盈
    ENABLE_TAKE_PROFIT_AT_099: bool = True
    TAKE_PROFIT_BID_PRICE: float = 0.98

    # 结算
    SETTLEMENT_TIMEOUT_SEC: int = 45

    # 文件
    DATA_DIR: str = "sniper_data"
    TRADES_FILE: str = "trades.csv"
    STATE_FILE: str = "state.json"
    ERROR_FILE: str = "errors.log"

    # 主循环
    EVAL_INTERVAL_SEC: float = 0.3
    BOOK_STALE_SEC: float = 5.0

    # REST 盘口兜底
    REST_BOOK_FALLBACK_ENABLED: bool = True
    REST_BOOK_FALLBACK_INTERVAL_SEC: float = 1.0
    REST_BOOK_TIMEOUT: float = 3.0

    DEBUG_WS_EVENT_TYPES: bool = True


CFG = Config()


# ─────────────────────────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────────────────────────

@dataclass
class Level:
    price: float
    size: float


def to_float_or_none(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip()
        if not v or v.lower() in ("none", "null", "nan"):
            return None
    try:
        x = float(v)
    except Exception:
        return None
    if x <= 0:
        return None
    return x


def _parse_levels(raw: Any, reverse: bool) -> List[Level]:
    out: List[Level] = []
    if not isinstance(raw, list):
        return out

    for x in raw:
        try:
            p = None
            sz = None
            if isinstance(x, dict):
                p = x.get("price") or x.get("p") or x.get("px")
                sz = x.get("size") or x.get("s") or x.get("qty") or x.get("quantity")
            elif isinstance(x, (list, tuple)) and len(x) >= 2:
                p = x[0]
                sz = x[1]

            p_f = to_float_or_none(p)
            s_f = to_float_or_none(sz)
            if p_f is not None and s_f is not None:
                out.append(Level(price=p_f, size=s_f))
        except Exception:
            continue

    out.sort(key=lambda z: z.price, reverse=reverse)
    return out


@dataclass
class Book:
    token_id: str
    outcome: str
    bids: List[Level] = field(default_factory=list)
    asks: List[Level] = field(default_factory=list)
    last_trade_price: Optional[float] = None
    updated_at: float = 0.0

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    @property
    def bid_depth_shares(self) -> float:
        return sum(lv.size for lv in self.bids[:3])

    def apply_snapshot(self, data: Dict[str, Any]) -> None:
        bids_raw = data.get("bids")
        if bids_raw is None:
            bids_raw = data.get("buys") or data.get("BUY") or []

        asks_raw = data.get("asks")
        if asks_raw is None:
            asks_raw = data.get("sells") or data.get("SELL") or []

        self.bids = _parse_levels(bids_raw, reverse=True)
        self.asks = _parse_levels(asks_raw, reverse=False)

        last_price = to_float_or_none(data.get("last_trade_price") or data.get("lastTradePrice"))
        if last_price is not None:
            self.last_trade_price = last_price

        self.updated_at = time.time()

    def apply_best_bid_ask(self, data: Dict[str, Any]) -> None:
        changed = False

        bid = data.get("best_bid") or data.get("bid") or data.get("bestBid")
        ask = data.get("best_ask") or data.get("ask") or data.get("bestAsk")

        bid_f = to_float_or_none(bid)
        ask_f = to_float_or_none(ask)

        if bid_f is not None:
            old_size = self.bids[0].size if self.bids else 1.0
            self.bids = [Level(price=bid_f, size=old_size)]
            changed = True

        if ask_f is not None:
            old_size = self.asks[0].size if self.asks else 1.0
            self.asks = [Level(price=ask_f, size=old_size)]
            changed = True

        if changed:
            self.updated_at = time.time()

    def apply_delta(self, data: Dict[str, Any]) -> None:
        changes = data.get("price_changes") or data.get("changes") or []
        changed = False

        for ch in changes:
            side = str(ch.get("side", "")).upper()
            price_f = to_float_or_none(ch.get("price") or ch.get("p") or ch.get("px"))

            try:
                raw_size = ch.get("size", ch.get("s", ch.get("qty", 0)))
                size = float(raw_size) if raw_size not in (None, "") else 0.0
            except Exception:
                size = 0.0

            if price_f is None:
                continue

            if side == "BUY":
                levels = self.bids
                reverse = True
            elif side == "SELL":
                levels = self.asks
                reverse = False
            else:
                continue

            levels[:] = [lv for lv in levels if abs(lv.price - price_f) > 1e-9]

            if size > 1e-9:
                levels.append(Level(price=price_f, size=size))

            levels.sort(key=lambda z: z.price, reverse=reverse)
            changed = True

        if changed:
            self.updated_at = time.time()


@dataclass
class MarketWindow:
    slug: str
    question: str
    url: str
    start_ts: int
    end_ts: int
    up_token_id: str
    down_token_id: str
    market_id: str = ""


@dataclass
class Position:
    market_slug: str
    side: str
    token_id: str
    entry_ask: float
    shares: float
    cost: float
    fee_paid: float
    expected_net_profit: float
    entry_ts: int
    market_end_ts: int
    market_id: str = ""
    up_token_id: str = ""
    down_token_id: str = ""
    status: str = "OPEN"
    realized_pnl: float = 0.0
    live_order_id: str = ""
    live_sell_order_id: str = ""
    live_status: str = ""
    live_avg_entry_price: float = 0.0
    live_filled_shares: float = 0.0


@dataclass
class AccountState:
    equity: float
    peak_equity: float
    day: str
    daily_pnl: float = 0.0
    trades_today: int = 0
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    consecutive_losses: int = 0
    total_pnl: float = 0.0

    @staticmethod
    def fresh() -> "AccountState":
        return AccountState(
            equity=CFG.INIT_EQUITY,
            peak_equity=CFG.INIT_EQUITY,
            day=date.today().isoformat(),
        )

    def reset_daily(self) -> None:
        today = date.today().isoformat()
        if self.day != today:
            self.day = today
            self.daily_pnl = 0.0
            self.trades_today = 0
            self.consecutive_losses = 0


# ─────────────────────────────────────────────────────────────
# 手续费与期望收益
# ─────────────────────────────────────────────────────────────

def calc_taker_fee(shares: float, price: float) -> float:
    gross = CFG.FEE_THETA * shares * price * (1.0 - price)
    net = gross * (1.0 - CFG.TAKER_REBATE_RATE)
    return max(0.0, net)


def expected_net_profit(ask: float, shares: float, win_prob: float) -> float:
    fee_buy = calc_taker_fee(shares, ask)
    profit_win = shares * (1.0 - ask) - fee_buy
    profit_lose = -shares * ask - fee_buy
    return win_prob * profit_win + (1.0 - win_prob) * profit_lose


def break_even_win_rate(ask: float) -> float:
    fee_per_share = calc_taker_fee(1.0, ask)
    return ask + fee_per_share


def estimate_win_prob(ask: float, crowd_ratio: float, remain: int) -> float:
    p = ask
    if crowd_ratio >= 3.0:
        p += 0.05
    elif crowd_ratio >= 2.0:
        p += 0.03
    elif crowd_ratio >= 1.5:
        p += 0.015

    if remain <= 12:
        p += 0.025
    elif remain <= 20:
        p += 0.015
    elif remain <= 30:
        p += 0.008

    return max(0.01, min(0.97, p))


def fee_summary(ask: float, shares: float) -> str:
    fee = calc_taker_fee(shares, ask)
    gross = CFG.FEE_THETA * shares * ask * (1.0 - ask)
    return (
        f"手续费 gross={gross:.4f}U → 打折后={fee:.4f}U "
        f"(Θ={CFG.FEE_THETA} rebate={CFG.TAKER_REBATE_RATE * 100:.0f}%)"
    )


# ─────────────────────────────────────────────────────────────
# 文件存储
# ─────────────────────────────────────────────────────────────

def ensure_dir() -> Path:
    p = Path(CFG.DATA_DIR)
    p.mkdir(parents=True, exist_ok=True)
    return p


class Journal:
    TRADE_FIELDS = [
        "time", "mode", "market_slug", "event", "side", "ask", "shares", "cost",
        "fee", "expected_net", "pnl", "equity", "live_usdc_balance",
        "live_order_id", "live_status", "live_avg_price", "live_filled_shares", "reason",
    ]

    def __init__(self) -> None:
        self.dir = ensure_dir()
        self.trades_path = self.dir / CFG.TRADES_FILE
        self.error_path = self.dir / CFG.ERROR_FILE

        if not self.trades_path.exists():
            with self.trades_path.open("w", newline="", encoding="utf-8-sig") as f:
                csv.DictWriter(f, fieldnames=self.TRADE_FIELDS, extrasaction="ignore").writeheader()

    def trade(self, row: Dict[str, Any]) -> None:
        row = dict(row)
        row.setdefault("mode", "PAPER" if CFG.PAPER_MODE else "LIVE")
        with self.trades_path.open("a", newline="", encoding="utf-8-sig") as f:
            csv.DictWriter(f, fieldnames=self.TRADE_FIELDS, extrasaction="ignore").writerow(row)

    def error(self, msg: str) -> None:
        with self.error_path.open("a", encoding="utf-8") as f:
            f.write(f"[{now_iso()}] {msg}\n")


class StateStore:
    def __init__(self) -> None:
        self.path = ensure_dir() / CFG.STATE_FILE

    def load(self) -> Tuple[AccountState, Optional[Position], List[str]]:
        if not self.path.exists():
            return AccountState.fresh(), None, []

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            acc = AccountState(**data["account"])
            pos = Position(**data["position"]) if data.get("position") else None
            traded_windows = data.get("traded_windows", [])
            acc.reset_daily()
            return acc, pos, traded_windows
        except Exception:
            return AccountState.fresh(), None, []

    def save(self, acc: AccountState, pos: Optional[Position], traded_windows: List[str]) -> None:
        data = {
            "account": asdict(acc),
            "position": asdict(pos) if pos else None,
            "traded_windows": traded_windows[-500:],
            "updated_at": now_iso(),
        }

        tmp = self.path.with_name(f"{self.path.stem}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        for _ in range(10):
            try:
                tmp.replace(self.path)
                return
            except PermissionError:
                time.sleep(0.1)

        tmp.replace(self.path)


# ─────────────────────────────────────────────────────────────
# HTTP 市场发现
# ─────────────────────────────────────────────────────────────

class Http:
    def __init__(self) -> None:
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "polymarket-sniper-clob-v2/1.0"

    def get(self, url: str, params: Optional[Dict[str, Any]] = None) -> Any:
        last: Optional[Exception] = None

        for i in range(CFG.HTTP_RETRIES):
            try:
                r = self.s.get(url, params=params or {}, timeout=CFG.HTTP_TIMEOUT)
                r.raise_for_status()
                return r.json()
            except Exception as e:
                last = e
                time.sleep(CFG.HTTP_RETRY_SLEEP * (i + 1))

        raise RuntimeError(f"GET failed {url}: {last}")


class MarketFinder:
    def __init__(self) -> None:
        self.http = Http()

    def _window_start(self, t: int) -> int:
        return (t // CFG.WINDOW_SECONDS) * CFG.WINDOW_SECONDS

    def _slug(self, start: int) -> str:
        return f"{CFG.MARKET_SLUG_PREFIX}-{start}"

    @staticmethod
    def _parse_ids(market: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        up_id = None
        down_id = None

        tokens = market.get("tokens")
        if isinstance(tokens, list):
            for t in tokens:
                outcome = str(t.get("outcome", "")).lower()
                token_id = t.get("token_id") or t.get("tokenId") or t.get("id")
                if not token_id:
                    continue
                if outcome == "up":
                    up_id = str(token_id)
                elif outcome == "down":
                    down_id = str(token_id)

        if up_id and down_id:
            return up_id, down_id

        try:
            outcomes = json.loads(market["outcomes"]) if isinstance(market.get("outcomes"), str) else market.get("outcomes", [])
            token_ids = json.loads(market["clobTokenIds"]) if isinstance(market.get("clobTokenIds"), str) else market.get("clobTokenIds", [])
        except Exception:
            return None, None

        for outcome, token_id in zip(outcomes, token_ids):
            outcome_s = str(outcome).lower()
            if outcome_s == "up":
                up_id = str(token_id)
            elif outcome_s == "down":
                down_id = str(token_id)

        return up_id, down_id

    def find(self) -> Optional[MarketWindow]:
        now = utc_now_ts()
        base = self._window_start(now)

        for offset in CFG.MARKET_SEARCH_OFFSETS:
            start = base + offset
            end = start + CFG.WINDOW_SECONDS

            if end <= now - 3:
                continue

            slug = self._slug(start)
            ev: Optional[Dict[str, Any]] = None

            try:
                ev = self.http.get(f"{CFG.GAMMA_BASE_URL}/events/slug/{slug}")
            except Exception:
                try:
                    data = self.http.get(f"{CFG.GAMMA_BASE_URL}/events", {"slug": slug, "limit": 1})
                    if isinstance(data, list) and data:
                        ev = data[0]
                    elif isinstance(data, dict):
                        events = data.get("events") or []
                        ev = events[0] if events else None
                except Exception:
                    ev = None

            if not ev:
                continue

            markets = ev.get("markets") or ([ev["market"]] if ev.get("market") else [])
            if not markets:
                continue

            market = markets[0]
            up_id, down_id = self._parse_ids(market)
            if not up_id or not down_id:
                continue

            market_id = str(
                market.get("conditionId")
                or market.get("condition_id")
                or market.get("id")
                or ev.get("condition_id")
                or ev.get("conditionId")
                or ""
            )

            return MarketWindow(
                slug=slug,
                question=ev.get("title") or market.get("question") or slug,
                url=f"https://polymarket.com/event/{slug}",
                start_ts=start,
                end_ts=end,
                up_token_id=up_id,
                down_token_id=down_id,
                market_id=market_id,
            )

        return None


# ─────────────────────────────────────────────────────────────
# WebSocket 盘口缓存
# ─────────────────────────────────────────────────────────────

class BookCache:
    def __init__(self) -> None:
        self._books: Dict[str, Book] = {}
        self._seen_events: Dict[str, bool] = {}
        self.resolved_by_market: Dict[str, str] = {}
        self.resolved_by_token_set: Dict[str, str] = {}

    def get_or_create(self, token_id: str, outcome: str) -> Book:
        if token_id not in self._books:
            self._books[token_id] = Book(token_id=token_id, outcome=outcome)
        return self._books[token_id]

    def reset(self, token_id: str, outcome: str) -> None:
        self._books[token_id] = Book(token_id=token_id, outcome=outcome)

    def _debug_event(self, event_type: str, token_id: str, outcome: Optional[str], msg: Dict[str, Any]) -> None:
        if not CFG.DEBUG_WS_EVENT_TYPES:
            return
        if event_type and not self._seen_events.get(event_type):
            self._seen_events[event_type] = True
            print(
                f"[WS调试] 首次收到 event_type={event_type!r} "
                f"token_id={token_id[:14]!r} outcome={outcome!r} keys={list(msg.keys())}",
                flush=True,
            )

    def handle_msg(self, raw: str, token_outcome_map: Dict[str, str]) -> None:
        try:
            msgs = json.loads(raw)
        except Exception:
            return

        if isinstance(msgs, dict):
            msgs = [msgs]

        for msg in msgs:
            if not isinstance(msg, dict):
                continue

            event_type = msg.get("event_type") or msg.get("type") or ""

            if event_type == "market_resolved":
                self._handle_market_resolved(msg)
                self._debug_event(event_type, "", None, msg)
                continue

            if event_type == "price_change":
                self._handle_price_change(msg, token_outcome_map)
                self._debug_event(event_type, "", None, msg)
                continue

            token_id = str(msg.get("asset_id") or msg.get("token_id") or "")
            outcome = token_outcome_map.get(token_id)

            self._debug_event(event_type, token_id, outcome, msg)

            if not outcome:
                continue

            book = self.get_or_create(token_id, outcome)

            if event_type == "book":
                book.apply_snapshot(msg)
            elif event_type == "best_bid_ask":
                book.apply_best_bid_ask(msg)
            elif event_type == "last_trade_price":
                try:
                    book.last_trade_price = float(msg.get("price", 0))
                    book.updated_at = time.time()
                except Exception:
                    pass

    def _handle_price_change(self, msg: Dict[str, Any], token_outcome_map: Dict[str, str]) -> None:
        changes = msg.get("price_changes") or msg.get("changes") or []
        if not isinstance(changes, list):
            return

        grouped: Dict[str, List[Dict[str, Any]]] = {}

        for ch in changes:
            if not isinstance(ch, dict):
                continue

            token_id = str(ch.get("asset_id") or ch.get("token_id") or ch.get("asset") or "")
            if not token_id:
                continue
            if token_id not in token_outcome_map:
                continue

            grouped.setdefault(token_id, []).append(ch)

        for token_id, token_changes in grouped.items():
            outcome = token_outcome_map[token_id]
            book = self.get_or_create(token_id, outcome)
            book.apply_delta({"price_changes": token_changes})

    def _handle_market_resolved(self, msg: Dict[str, Any]) -> None:
        winning_asset_id = str(msg.get("winning_asset_id") or msg.get("winningAssetId") or "")
        market_id = str(msg.get("market") or msg.get("id") or msg.get("condition_id") or msg.get("conditionId") or "")

        if market_id and winning_asset_id:
            self.resolved_by_market[market_id] = winning_asset_id

        assets_ids = msg.get("assets_ids") or msg.get("asset_ids") or []
        if isinstance(assets_ids, str):
            try:
                assets_ids = json.loads(assets_ids)
            except Exception:
                assets_ids = []

        if isinstance(assets_ids, list) and winning_asset_id:
            key = "|".join(sorted(str(x) for x in assets_ids))
            self.resolved_by_token_set[key] = winning_asset_id

    def get_winner_for_market(self, market_id: str, up_token_id: str, down_token_id: str) -> Optional[str]:
        if market_id and market_id in self.resolved_by_market:
            return self.resolved_by_market[market_id]

        key = "|".join(sorted([str(up_token_id), str(down_token_id)]))
        return self.resolved_by_token_set.get(key)


class WsManager:
    def __init__(self, cache: BookCache, journal: Journal) -> None:
        self.cache = cache
        self.journal = journal
        self._token_map: Dict[str, str] = {}
        self._ws: Any = None
        self._running = False
        self._delay = CFG.WS_RECONNECT_BASE
        self.connected = asyncio.Event()

    def set_tokens(self, token_map: Dict[str, str]) -> None:
        self._token_map = token_map

    async def _subscribe(self, ws: Any) -> None:
        if not self._token_map:
            return

        msg = {
            "assets_ids": list(self._token_map.keys()),
            "type": "market",
            "custom_feature_enabled": True,
        }

        await ws.send(json.dumps(msg))
        short_ids = [x[:14] + "..." for x in self._token_map.keys()]
        log(f"[WS] 已订阅 {len(self._token_map)} 个 token: {short_ids}", CYAN)

    async def force_reconnect(self) -> None:
        try:
            if self._ws:
                await self._ws.close()
        except Exception:
            pass

    async def run(self) -> None:
        self._running = True

        while self._running:
            try:
                async with websockets.connect(
                    CFG.WS_URL,
                    ping_interval=CFG.WS_PING_INTERVAL,
                    ping_timeout=CFG.WS_PING_TIMEOUT,
                    open_timeout=15,
                ) as ws:
                    self._ws = ws
                    self._delay = CFG.WS_RECONNECT_BASE
                    await self._subscribe(ws)
                    self.connected.set()
                    log("[WS] 连接成功", GREEN)

                    async for raw in ws:
                        self.cache.handle_msg(raw, self._token_map)

            except (ConnectionClosed, WebSocketException) as e:
                log(f"[WS] 断线: {e}", YELLOW)

            except Exception as e:
                err = f"[WS] 异常: {e}"
                log(err, RED)
                self.journal.error(err)

            finally:
                self._ws = None
                self.connected.clear()

            if not self._running:
                break

            log(f"[WS] {self._delay:.1f}s 后重连", YELLOW)
            await asyncio.sleep(self._delay)
            self._delay = min(self._delay * CFG.WS_RECONNECT_FACTOR, CFG.WS_RECONNECT_MAX)

    def stop(self) -> None:
        self._running = False


# ─────────────────────────────────────────────────────────────
# CLOB V2 实盘交易
# ─────────────────────────────────────────────────────────────

@dataclass
class LiveOrderResult:
    ok: bool
    order_id: str = ""
    status: str = ""
    raw: Any = None
    error: str = ""
    filled_shares: float = 0.0
    avg_price: float = 0.0
    spent_u: float = 0.0
    success: bool = False


class LiveTrader:
    def __init__(self, journal: Journal) -> None:
        self.journal = journal
        self.client: Any = None
        self.enabled = False

    def _confirm_file_ok(self) -> bool:
        path = Path(CFG.LIVE_CONFIRM_FILE)
        try:
            return path.exists() and path.read_text(encoding="utf-8").strip() == CFG.LIVE_CONFIRM_TEXT
        except Exception:
            return False

    def _make_client(self, host: str, chain_id: int, private_key: str, signature_type: int, funder: str, creds: Any = None) -> Any:
        """
        V2 SDK 不同小版本的 ClobClient 参数可能略有差异。
        这里用逐级降级方式，避免因为 use_server_time / retry_on_error / signature_type 参数名变化而直接启动失败。
        """
        attempts: List[Dict[str, Any]] = []

        base = {
            "host": host,
            "chain_id": chain_id,
            "key": private_key,
        }

        full = dict(base)
        full["signature_type"] = signature_type
        if funder:
            full["funder"] = funder
        full["use_server_time"] = True
        full["retry_on_error"] = True
        if creds is not None:
            full["creds"] = creds
        attempts.append(full)

        mid = dict(base)
        mid["signature_type"] = signature_type
        if funder:
            mid["funder"] = funder
        if creds is not None:
            mid["creds"] = creds
        attempts.append(mid)

        simple = dict(base)
        if creds is not None:
            simple["creds"] = creds
        attempts.append(simple)

        last_exc: Optional[Exception] = None
        for kwargs in attempts:
            try:
                return ClobClient(**kwargs)
            except TypeError as e:
                last_exc = e
                continue

        raise RuntimeError(f"ClobClient init failed: {repr(last_exc)}")

    def init_if_needed(self) -> None:
        if self.client is not None:
            return

        if CFG.PAPER_MODE or not CFG.LIVE_TRADING_ENABLED:
            self.enabled = False
            return

        if not self._confirm_file_ok():
            raise RuntimeError(
                f"实盘确认失败：请在当前目录创建 {CFG.LIVE_CONFIRM_FILE}，"
                f"内容必须精确为 {CFG.LIVE_CONFIRM_TEXT}"
            )

        if ClobClient is None or MarketOrderArgs is None or OrderType is None:
            raise RuntimeError(
                "缺少 CLOB V2 SDK。请执行：\n"
                "pip uninstall py-clob-client -y\n"
                "pip install -U py-clob-client-v2 python-dotenv"
            )

        if load_dotenv:
            load_dotenv()

        private_key = (
            os.getenv("POLY_PRIVATE_KEY", "").strip()
            or os.getenv("PK", "").strip()
            or os.getenv("PRIVATE_KEY", "").strip()
        )
        funder = os.getenv("POLY_FUNDER", "").strip() or os.getenv("FUNDER", "").strip()
        host = os.getenv("POLY_CLOB_HOST", CFG.CLOB_BASE_URL).strip() or CFG.CLOB_BASE_URL
        signature_type = int(os.getenv("POLY_SIGNATURE_TYPE", str(CFG.LIVE_SIGNATURE_TYPE)))

        if not private_key:
            raise RuntimeError("缺少环境变量 POLY_PRIVATE_KEY，也可以用 PK 或 PRIVATE_KEY")

        if signature_type in (1, 2) and not funder:
            raise RuntimeError("proxy 钱包模式缺少 POLY_FUNDER")

        api_key = os.getenv("POLY_CLOB_API_KEY", "").strip() or os.getenv("CLOB_API_KEY", "").strip()
        api_secret = os.getenv("POLY_CLOB_SECRET", "").strip() or os.getenv("CLOB_SECRET", "").strip()
        api_passphrase = (
            os.getenv("POLY_CLOB_PASS_PHRASE", "").strip()
            or os.getenv("CLOB_PASS_PHRASE", "").strip()
            or os.getenv("CLOB_API_PASSPHRASE", "").strip()
        )

        if api_key and api_secret and api_passphrase and ApiCreds is not None:
            creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)
            client = self._make_client(host, CFG.LIVE_CHAIN_ID, private_key, signature_type, funder, creds=creds)
            log("[LIVE] 使用已有 CLOB V2 API credentials", CYAN)
        else:
            temp = self._make_client(host, CFG.LIVE_CHAIN_ID, private_key, signature_type, funder, creds=None)
            creds = temp.create_or_derive_api_key()
            client = self._make_client(host, CFG.LIVE_CHAIN_ID, private_key, signature_type, funder, creds=creds)
            log("[LIVE] 已 create_or_derive_api_key；建议将 API Key 写入 .env", YELLOW)

        self.client = client
        self.enabled = True

        try:
            r = requests.get(f"{host}/version", timeout=5)
            version = r.text[:200]
        except Exception:
            version = "UNKNOWN"

        log(
            f"[LIVE] CLOB V2 初始化完成 host={host} version={version} "
            f"signature_type={signature_type} funder={'已设置' if funder else 'EOA'}",
            GREEN,
        )

    @staticmethod
    def _num(v: Any, default: float = 0.0) -> float:
        try:
            if v is None or v == "":
                return default
            return float(v)
        except Exception:
            return default

    @staticmethod
    def _floor(value: float, decimals: int) -> float:
        q = Decimal("1").scaleb(-decimals)
        return float(Decimal(str(value)).quantize(q, rounding=ROUND_DOWN))

    @staticmethod
    def _round_price(price: float, tick: float, side_name: str) -> float:
        tick = tick or CFG.LIVE_DEFAULT_TICK_SIZE
        d_price = Decimal(str(price))
        d_tick = Decimal(str(tick))
        if side_name.upper() == "BUY":
            n = (d_price / d_tick).to_integral_value(rounding=ROUND_UP)
        else:
            n = (d_price / d_tick).to_integral_value(rounding=ROUND_DOWN)
        px = max(d_tick, n * d_tick)
        return float(px.quantize(Decimal("0.0001"), rounding=ROUND_DOWN))

    @staticmethod
    def _parse_order_id(resp: Any) -> str:
        if not isinstance(resp, dict):
            return ""
        return str(resp.get("orderID") or resp.get("orderId") or resp.get("id") or resp.get("order_id") or "")

    @staticmethod
    def _parse_status(resp: Any) -> str:
        if not isinstance(resp, dict):
            return ""
        return str(resp.get("status") or resp.get("success") or "")

    @staticmethod
    def _parse_success(resp: Any) -> bool:
        if not isinstance(resp, dict):
            return False
        if resp.get("success") is True:
            return True
        if resp.get("error") or resp.get("errorMsg"):
            return False
        status = str(resp.get("status") or "").lower()
        return status in {"matched", "filled", "success", "delayed", "live", "unmatched"} or bool(resp.get("orderID"))

    def _order_type(self) -> Any:
        name = str(CFG.LIVE_ORDER_TYPE).upper()
        return getattr(OrderType, name, OrderType.FAK)

    def get_collateral_balance_usdc(self) -> Optional[float]:
        self.init_if_needed()
        if not self.enabled or not self.client:
            return None

        try:
            if BalanceAllowanceParams is None or AssetType is None:
                return None
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            res = self.client.get_balance_allowance(params)
            raw = self._num(res.get("balance") if isinstance(res, dict) else None, 0.0)
            return raw / 1_000_000.0 if raw > 10_000 else raw
        except Exception as e:
            self.journal.error(f"LIVE balance check failed: {repr(e)}")
            return None

    def get_conditional_balance_shares(self, token_id: str) -> Optional[float]:
        self.init_if_needed()
        if not self.enabled or not self.client:
            return None

        try:
            if BalanceAllowanceParams is None or AssetType is None:
                return None
            params = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=str(token_id))
            res = self.client.get_balance_allowance(params)
            raw = self._num(res.get("balance") if isinstance(res, dict) else None, 0.0)
            return raw / 1_000_000.0 if raw > 10_000 else raw
        except Exception as e:
            self.journal.error(f"LIVE conditional balance check failed token={token_id}: {repr(e)}")
            return None

    def get_tick_size(self, token_id: str) -> float:
        try:
            return float(self.client.get_tick_size(str(token_id)))
        except Exception:
            return CFG.LIVE_DEFAULT_TICK_SIZE

    def get_neg_risk(self, token_id: str) -> bool:
        try:
            return bool(self.client.get_neg_risk(str(token_id)))
        except Exception:
            return False

    def _market_order_args(self, token_id: str, side: Any, amount: float, price: float, order_type: Any) -> Any:
        """
        兼容 V2 SDK 不同小版本：
        - 新文档：MarketOrderArgs(token_id, side, amount, price)
        - README 示例：MarketOrderArgs(token_id, amount, side, order_type)
        """
        variants = [
            {"token_id": str(token_id), "side": side, "amount": float(amount), "price": float(price), "order_type": order_type},
            {"token_id": str(token_id), "side": side, "amount": float(amount), "price": float(price)},
            {"token_id": str(token_id), "amount": float(amount), "side": side, "order_type": order_type},
            {"token_id": str(token_id), "amount": float(amount), "side": side},
        ]
        last_exc: Optional[Exception] = None
        for kwargs in variants:
            try:
                return MarketOrderArgs(**kwargs)
            except TypeError as e:
                last_exc = e
                continue
        raise RuntimeError(f"MarketOrderArgs init failed: {repr(last_exc)}")

    def _post_market_order_v2(self, token_id: str, side: Any, amount: float, price: float, tick: float, neg_risk: bool) -> Any:
        order_type = self._order_type()
        args = self._market_order_args(token_id=token_id, side=side, amount=amount, price=price, order_type=order_type)
        options = PartialCreateOrderOptions(tick_size=str(tick), neg_risk=bool(neg_risk))

        # 官方 V2 Python SDK 方法名
        try:
            return self.client.create_and_post_market_order(
                order_args=args,
                options=options,
                order_type=order_type,
            )
        except TypeError:
            # 兼容位置参数
            return self.client.create_and_post_market_order(args, options, order_type)

    @staticmethod
    def _amount_for_size_price(size: float, price: float) -> float:
        amount = Decimal(str(size)) * Decimal(str(price))
        return float(amount.quantize(Decimal("0.01"), rounding=ROUND_DOWN))

    def buy_limit(self, token_id: str, ask: float, shares: float) -> LiveOrderResult:
        """
        名称保留 buy_limit，实际用 V2 market order + FAK。
        BUY market order 的 amount 是要花的美元金额。
        price 是 worst-price limit。
        """
        self.init_if_needed()
        if not self.enabled:
            return LiveOrderResult(ok=False, error="LIVE_NOT_ENABLED")

        amount_u = self._floor(float(shares) * float(ask), 2)
        if amount_u <= 0:
            return LiveOrderResult(ok=False, error="BUY_AMOUNT_ZERO")

        tick = self.get_tick_size(token_id)
        neg_risk = self.get_neg_risk(token_id)
        worst_price = self._round_price(min(0.99, float(ask) + CFG.LIVE_BUY_SLIPPAGE), tick, "BUY")

        approx_shares = self._floor(amount_u / max(worst_price, 1e-9), 4)

        log(
            f"[LIVE BUY V2 准备] token={token_id[:14]}... ask={ask:.4f} worst={worst_price:.4f} "
            f"amount={amount_u:.2f}U approx_shares≈{approx_shares:.4f} type={CFG.LIVE_ORDER_TYPE} tick={tick} neg_risk={neg_risk}",
            YELLOW,
        )

        try:
            resp = self._post_market_order_v2(
                token_id=token_id,
                side=Side.BUY,
                amount=amount_u,
                price=worst_price,
                tick=tick,
                neg_risk=neg_risk,
            )

            oid = self._parse_order_id(resp)
            status = self._parse_status(resp)
            ok = self._parse_success(resp)

            if not ok:
                return LiveOrderResult(ok=False, order_id=oid, status=status, raw=resp, error=f"BUY_NOT_MATCHED resp={resp}")

            return LiveOrderResult(
                ok=True,
                order_id=oid,
                status=status,
                raw=resp,
                success=True,
                filled_shares=approx_shares,
                avg_price=worst_price,
                spent_u=amount_u,
            )

        except Exception as e:
            err = repr(e)
            self.journal.error(f"LIVE BUY V2 failed: {err}")

            if "order_version_mismatch" in err:
                try:
                    log("[LIVE BUY V2] order_version_mismatch，重建 client 后重试一次", YELLOW)
                    self.client = None
                    self.enabled = False
                    self.init_if_needed()
                    resp = self._post_market_order_v2(
                        token_id=token_id,
                        side=Side.BUY,
                        amount=amount_u,
                        price=worst_price,
                        tick=tick,
                        neg_risk=neg_risk,
                    )
                    oid = self._parse_order_id(resp)
                    status = self._parse_status(resp)
                    ok = self._parse_success(resp)
                    if ok:
                        return LiveOrderResult(
                            ok=True, order_id=oid, status=status, raw=resp,
                            success=True, filled_shares=approx_shares,
                            avg_price=worst_price, spent_u=amount_u,
                        )
                    return LiveOrderResult(ok=False, order_id=oid, status=status, raw=resp, error=f"BUY_NOT_MATCHED_AFTER_RETRY resp={resp}")
                except Exception as e2:
                    self.journal.error(f"LIVE BUY V2 retry failed: {repr(e2)}")
                    return LiveOrderResult(ok=False, error=f"ORDER_VERSION_MISMATCH_RETRY_FAILED: {repr(e2)}")

            return LiveOrderResult(ok=False, error=err)

    def sell_limit(self, token_id: str, bid: float, shares: float) -> LiveOrderResult:
        """
        名称保留 sell_limit，实际用 V2 market order + FAK。
        SELL market order 的 amount 是卖出的 shares 数量。
        price 是 worst-price limit。
        """
        self.init_if_needed()
        if not self.enabled:
            return LiveOrderResult(ok=False, error="LIVE_NOT_ENABLED")

        real_balance = self.get_conditional_balance_shares(token_id)
        size_base = min(float(shares), real_balance) if real_balance is not None else float(shares)
        size = self._floor(size_base, 4)

        if size <= 0:
            return LiveOrderResult(ok=False, error="SELL_SIZE_ZERO")

        tick = self.get_tick_size(token_id)
        neg_risk = self.get_neg_risk(token_id)
        worst_price = self._round_price(max(0.01, float(bid) - CFG.LIVE_SELL_SLIPPAGE), tick, "SELL")
        expected_receive = self._amount_for_size_price(size, worst_price)

        log(
            f"[LIVE SELL V2 准备] token={token_id[:14]}... bid={bid:.4f} worst={worst_price:.4f} "
            f"shares={size:.4f} receive≈{expected_receive:.2f}U "
            f"real_balance={'UNKNOWN' if real_balance is None else f'{real_balance:.4f}'} type={CFG.LIVE_ORDER_TYPE}",
            YELLOW,
        )

        try:
            resp = self._post_market_order_v2(
                token_id=token_id,
                side=Side.SELL,
                amount=size,
                price=worst_price,
                tick=tick,
                neg_risk=neg_risk,
            )

            oid = self._parse_order_id(resp)
            status = self._parse_status(resp)
            ok = self._parse_success(resp)

            if not ok:
                return LiveOrderResult(ok=False, order_id=oid, status=status, raw=resp, error=f"SELL_NOT_MATCHED resp={resp}")

            return LiveOrderResult(
                ok=True,
                order_id=oid,
                status=status,
                raw=resp,
                success=True,
                filled_shares=size,
                avg_price=worst_price,
                spent_u=expected_receive,
            )

        except Exception as e:
            err = repr(e)
            self.journal.error(f"LIVE SELL V2 failed: {err}")

            if "order_version_mismatch" in err:
                try:
                    log("[LIVE SELL V2] order_version_mismatch，重建 client 后重试一次", YELLOW)
                    self.client = None
                    self.enabled = False
                    self.init_if_needed()
                    resp = self._post_market_order_v2(
                        token_id=token_id,
                        side=Side.SELL,
                        amount=size,
                        price=worst_price,
                        tick=tick,
                        neg_risk=neg_risk,
                    )
                    oid = self._parse_order_id(resp)
                    status = self._parse_status(resp)
                    ok = self._parse_success(resp)
                    if ok:
                        return LiveOrderResult(
                            ok=True, order_id=oid, status=status, raw=resp,
                            success=True, filled_shares=size,
                            avg_price=worst_price, spent_u=expected_receive,
                        )
                    return LiveOrderResult(ok=False, order_id=oid, status=status, raw=resp, error=f"SELL_NOT_MATCHED_AFTER_RETRY resp={resp}")
                except Exception as e2:
                    self.journal.error(f"LIVE SELL V2 retry failed: {repr(e2)}")
                    return LiveOrderResult(ok=False, error=f"ORDER_VERSION_MISMATCH_RETRY_FAILED: {repr(e2)}")

            return LiveOrderResult(ok=False, error=err)


# ─────────────────────────────────────────────────────────────
# 风控
# ─────────────────────────────────────────────────────────────

class RiskManager:
    def __init__(self, acc: AccountState) -> None:
        self.acc = acc

    def block(self) -> Optional[str]:
        self.acc.reset_daily()

        if self.acc.daily_pnl <= -abs(CFG.DAILY_MAX_LOSS_U):
            return f"日亏损触顶 {self.acc.daily_pnl:.2f}U"

        if self.acc.consecutive_losses >= CFG.MAX_CONSECUTIVE_LOSSES:
            return f"连续亏损 {self.acc.consecutive_losses} 次"

        if self.acc.trades_today >= CFG.MAX_TRADES_PER_DAY:
            return f"每日交易上限 {self.acc.trades_today}"

        if self.acc.equity < CFG.MIN_TRADE_BUDGET_U:
            return f"权益过低 {self.acc.equity:.2f}U，低于最低交易预算 {CFG.MIN_TRADE_BUDGET_U:.2f}U"

        return None


# ─────────────────────────────────────────────────────────────
# 主引擎
# ─────────────────────────────────────────────────────────────

class SniperEngine:
    def __init__(self) -> None:
        self.journal = Journal()
        self.store = StateStore()
        self.acc, self.pos, self.traded = self.store.load()
        self.risk = RiskManager(self.acc)
        self.finder = MarketFinder()
        self.cache = BookCache()
        self.ws = WsManager(self.cache, self.journal)
        self.live = LiveTrader(self.journal)
        self.live_balance_usdc: Optional[float] = None

        self.market: Optional[MarketWindow] = None
        self._last_refresh = 0.0
        self._cooldown_end = 0.0
        self._last_rest_book_fallback = 0.0

    def _refresh_live_balance(self, reason: str = "") -> Optional[float]:
        if CFG.PAPER_MODE:
            return None
        try:
            bal = self.live.get_collateral_balance_usdc()
            if bal is not None:
                self.live_balance_usdc = bal
                self.acc.equity = bal
                log(f"[LIVE余额] pUSD/USDC={bal:.4f}U" + (f" reason={reason}" if reason else ""), CYAN)
            return bal
        except Exception as e:
            self.journal.error(f"LIVE balance refresh failed: {repr(e)}")
            log(f"[LIVE余额] 查询失败: {e}", RED)
            return None

    def banner(self) -> None:
        mode = "PAPER/影子盘" if CFG.PAPER_MODE else "LIVE/实盘"
        print(
            f"""
{BOLD}{CYAN}╔══════════════════════════════════════════════════════════════════╗
║  {CFG.PROGRAM_NAME:<64s} ║
╠══════════════════════════════════════════════════════════════════╣
║  模式  : {mode:<57s} ║
║  策略  : 最后 {CFG.SNIPE_WINDOW_MIN}~{CFG.SNIPE_WINDOW_MAX}s，买高概率 ask 区间方向，默认不中途止损        ║
║  价格  : ask 区间 [{CFG.MIN_ENTRY_ASK:.2f}, {CFG.MAX_ENTRY_ASK:.2f}]，点差 ≤ {CFG.MAX_SPREAD:.2f}                             ║
║  预算  : {CFG.ORDER_BUDGET_U:.2f}U/笔，最低 {CFG.MIN_TRADE_BUDGET_U:.2f}U，日亏上限 {CFG.DAILY_MAX_LOSS_U:.2f}U                 ║
║  止盈  : bid ≥ {CFG.TAKE_PROFIT_BID_PRICE:.2f} 自动全部卖出                                  ║
║  当前  : {self.acc.equity:.2f}U                                              ║
╚══════════════════════════════════════════════════════════════════╝{RESET}
"""
        )

    async def _refresh_market(self) -> Optional[MarketWindow]:
        now = time.time()

        if (
            self.market
            and now - self._last_refresh < CFG.MARKET_REFRESH_SEC
            and self.market.end_ts > utc_now_ts() + 5
        ):
            return self.market

        loop = asyncio.get_event_loop()
        market = await loop.run_in_executor(None, self.finder.find)
        self._last_refresh = time.time()

        if market and (not self.market or market.slug != self.market.slug):
            log(f"[市场] 切换到: {market.slug} end={market.end_ts} remain={market.end_ts - utc_now_ts()}s", CYAN)
            log(f"       问题: {market.question}", WHITE)
            log(f"       URL:  {market.url}", WHITE)
            log(f"       UP={market.up_token_id[:14]}... DOWN={market.down_token_id[:14]}...", WHITE)

            token_map = {
                market.up_token_id: "UP",
                market.down_token_id: "DOWN",
            }

            for token_id, outcome in token_map.items():
                self.cache.reset(token_id, outcome)

            self.ws.set_tokens(token_map)
            await self.ws.force_reconnect()
            self.market = market

        return self.market

    def _fetch_book_rest_once(self, token_id: str) -> Optional[Dict[str, Any]]:
        urls = [
            f"{CFG.CLOB_BASE_URL}/book",
            f"{CFG.CLOB_BASE_URL}/orderbook",
        ]

        for url in urls:
            try:
                r = requests.get(
                    url,
                    params={"token_id": token_id},
                    timeout=CFG.REST_BOOK_TIMEOUT,
                    headers={"User-Agent": "polymarket-sniper-book-fallback/1.0"},
                )
                if r.status_code == 404:
                    continue
                r.raise_for_status()
                data = r.json()
                if isinstance(data, dict):
                    return data
            except Exception:
                continue

        return None

    def _try_rest_book_fallback(self, market: MarketWindow, force: bool = False) -> None:
        if not CFG.REST_BOOK_FALLBACK_ENABLED:
            return

        now = time.time()
        if not force and now - self._last_rest_book_fallback < CFG.REST_BOOK_FALLBACK_INTERVAL_SEC:
            return

        self._last_rest_book_fallback = now

        for token_id, outcome in ((market.up_token_id, "UP"), (market.down_token_id, "DOWN")):
            data = self._fetch_book_rest_once(token_id)
            if not data:
                continue

            if isinstance(data.get("book"), dict):
                data = data["book"]
            elif isinstance(data.get("data"), dict):
                data = data["data"]
            elif isinstance(data.get("orderbook"), dict):
                data = data["orderbook"]

            book = self.cache.get_or_create(token_id, outcome)
            book.apply_snapshot(data)

            log(
                f"[REST盘口兜底] {outcome} bid={book.best_bid} ask={book.best_ask} "
                f"bids={len(book.bids)} asks={len(book.asks)}",
                DIM,
            )

    def _books_ready(self, up_book: Book, down_book: Book) -> Tuple[bool, str]:
        now = time.time()

        for book in (up_book, down_book):
            if book.updated_at <= 0:
                return False, f"[{book.outcome}] 盘口尚未初始化"

            stale = now - book.updated_at
            if stale > CFG.BOOK_STALE_SEC:
                return False, f"[{book.outcome}] 盘口过期 {stale:.1f}s"

            if book.best_bid is None or book.best_ask is None:
                return (
                    False,
                    f"[{book.outcome}] best_bid/best_ask 不完整 "
                    f"bid={book.best_bid} ask={book.best_ask} "
                    f"bids_len={len(book.bids)} asks_len={len(book.asks)} "
                    f"updated_at={book.updated_at:.0f}"
                )

        return True, "OK"

    def _snipe(self, market: MarketWindow) -> None:
        remain = market.end_ts - utc_now_ts()

        if remain > CFG.SNIPE_WINDOW_MAX or remain < CFG.SNIPE_WINDOW_MIN:
            return

        if time.time() < self._cooldown_end:
            return

        block = self.risk.block()
        if block:
            log(f"风控: {block}", RED)
            return

        if CFG.ONLY_ONE_TRADE_PER_WINDOW and market.slug in self.traded:
            return

        up_book = self.cache.get_or_create(market.up_token_id, "UP")
        down_book = self.cache.get_or_create(market.down_token_id, "DOWN")

        ready, reason = self._books_ready(up_book, down_book)
        if not ready:
            self._try_rest_book_fallback(market)
            ready, reason = self._books_ready(up_book, down_book)

        if not ready:
            log(reason + "，跳过", DIM)
            return

        up_depth = up_book.bid_depth_shares
        down_depth = down_book.bid_depth_shares

        log(
            f"[狙击] remain={remain}s "
            f"UP depth={up_depth:.1f} bid={up_book.best_bid} ask={up_book.best_ask} "
            f"DOWN depth={down_depth:.1f} bid={down_book.best_bid} ask={down_book.best_ask}",
            YELLOW,
        )

        def price_in_favorite_range(book: Book) -> bool:
            ask0 = book.best_ask
            return ask0 is not None and CFG.MIN_ENTRY_ASK <= ask0 <= CFG.MAX_ENTRY_ASK

        up_price_ok = price_in_favorite_range(up_book)
        down_price_ok = price_in_favorite_range(down_book)

        if up_price_ok and not down_price_ok:
            chosen_book = up_book
            other_book = down_book
            log("  UP 符合高概率 ask 区间，选择 UP", GREEN)
        elif down_price_ok and not up_price_ok:
            chosen_book = down_book
            other_book = up_book
            log("  DOWN 符合高概率 ask 区间，选择 DOWN", GREEN)
        elif up_price_ok and down_price_ok:
            if up_depth >= down_depth * CFG.MIN_CROWD_RATIO:
                chosen_book = up_book
                other_book = down_book
                log("  两边 ask 都符合，UP 深度更强，选择 UP", GREEN)
            elif down_depth >= up_depth * CFG.MIN_CROWD_RATIO:
                chosen_book = down_book
                other_book = up_book
                log("  两边 ask 都符合，DOWN 深度更强，选择 DOWN", GREEN)
            else:
                log(f"  两边 ask 都符合，但深度差距不足：UP {up_depth:.1f} vs DOWN {down_depth:.1f}，不买", DIM)
                return
        else:
            log(
                f"  两边 ask 都不在高概率区间：UP ask={up_book.best_ask} DOWN ask={down_book.best_ask} "
                f"要求 [{CFG.MIN_ENTRY_ASK:.2f}, {CFG.MAX_ENTRY_ASK:.2f}]，不买",
                DIM,
            )
            return

        side = chosen_book.outcome
        ask = chosen_book.best_ask

        if ask is None:
            log(f"  [{side}] ask 不存在", DIM)
            return

        if ask < CFG.MIN_ENTRY_ASK or ask > CFG.MAX_ENTRY_ASK:
            log(f"  [{side}] ask={ask:.3f} 超出范围 [{CFG.MIN_ENTRY_ASK:.2f}, {CFG.MAX_ENTRY_ASK:.2f}]", DIM)
            return

        spread = chosen_book.spread
        if spread is None or spread > CFG.MAX_SPREAD:
            log(f"  [{side}] 点差过大 spread={spread}", DIM)
            return

        if chosen_book.bid_depth_shares < CFG.MIN_BID_DEPTH_SHARES:
            log(f"  [{side}] bid_depth 不足 {chosen_book.bid_depth_shares:.1f}", DIM)
            return

        budget = min(CFG.ORDER_BUDGET_U, self.acc.equity)
        if budget < CFG.MIN_TRADE_BUDGET_U:
            log(f"  可用预算 {budget:.2f}U 低于最低交易预算，不买", RED)
            return

        shares = budget / ask
        fee = calc_taker_fee(shares, ask)
        crowd_ratio = chosen_book.bid_depth_shares / max(other_book.bid_depth_shares, 1e-9)
        win_prob = estimate_win_prob(ask, crowd_ratio, remain)
        exp_net = expected_net_profit(ask, shares, win_prob=win_prob)
        be_rate = break_even_win_rate(ask)

        log(
            f"  [{side}] ask={ask:.3f} shares={shares:.2f} budget={budget:.2f}U "
            f"crowd_ratio={crowd_ratio:.2f}x win_prob≈{win_prob * 100:.1f}%\n"
            f"           {fee_summary(ask, shares)}\n"
            f"           期望净利润={exp_net:.4f}U 阈值={CFG.MIN_NET_PROFIT_U:.4f}U "
            f"盈亏平衡胜率≈{be_rate * 100:.1f}%",
            WHITE,
        )

        if exp_net < CFG.MIN_NET_PROFIT_U:
            log(f"  ❌ 期望净利润 {exp_net:.4f}U < 阈值 {CFG.MIN_NET_PROFIT_U:.4f}U，不买", RED)
            return

        self._execute_buy(market=market, side=side, book=chosen_book, shares=shares, fee=fee, exp_net=exp_net)

    def _execute_buy(self, market: MarketWindow, side: str, book: Book, shares: float, fee: float, exp_net: float) -> None:
        ask = book.best_ask
        if ask is None:
            return

        cost = shares * ask
        live_order_id = ""
        live_status = ""
        live_avg_price = ask
        live_filled_shares = shares

        if CFG.PAPER_MODE:
            log(
                f"🟢 影子买入 {side} ask={ask:.4f} shares={shares:.3f} "
                f"cost={cost:.4f}U fee={fee:.4f}U 期望净利={exp_net:.4f}U",
                GREEN,
            )
        else:
            self._refresh_live_balance("before_buy")
            res = self.live.buy_limit(book.token_id, ask=ask, shares=shares)
            if not res.ok:
                log(f"❌ 实盘买入失败 {side}: {res.error}", RED)
                self.journal.trade(
                    {
                        "time": now_iso(),
                        "mode": "LIVE",
                        "market_slug": market.slug,
                        "event": "BUY_FAIL",
                        "side": side,
                        "ask": ask,
                        "shares": shares,
                        "cost": cost,
                        "fee": fee,
                        "expected_net": exp_net,
                        "pnl": 0.0,
                        "equity": self.acc.equity,
                        "live_usdc_balance": self.live_balance_usdc if self.live_balance_usdc is not None else "",
                        "live_order_id": res.order_id,
                        "live_status": res.status,
                        "live_avg_price": res.avg_price,
                        "live_filled_shares": res.filled_shares,
                        "reason": res.error,
                    }
                )
                return

            live_order_id = res.order_id
            live_status = res.status
            live_avg_price = res.avg_price or ask
            live_filled_shares = res.filled_shares or shares
            shares = live_filled_shares
            cost = res.spent_u or (shares * live_avg_price)
            ask = live_avg_price
            fee = calc_taker_fee(shares, ask)

            log(
                f"🟢 实盘买入成功 {side} avg≈{ask:.4f} shares≈{shares:.4f} "
                f"cost≈{cost:.4f}U order_id={live_order_id} status={live_status}",
                GREEN,
            )
            self._refresh_live_balance("after_buy")

        self.pos = Position(
            market_slug=market.slug,
            side=side,
            token_id=book.token_id,
            entry_ask=ask,
            shares=shares,
            cost=cost,
            fee_paid=fee,
            expected_net_profit=exp_net,
            entry_ts=utc_now_ts(),
            market_end_ts=market.end_ts,
            market_id=market.market_id,
            up_token_id=market.up_token_id,
            down_token_id=market.down_token_id,
            live_order_id=live_order_id,
            live_status=live_status,
            live_avg_entry_price=live_avg_price,
            live_filled_shares=live_filled_shares,
        )

        self.journal.trade(
            {
                "time": now_iso(),
                "mode": "PAPER" if CFG.PAPER_MODE else "LIVE",
                "market_slug": market.slug,
                "event": "BUY",
                "side": side,
                "ask": ask,
                "shares": shares,
                "cost": cost,
                "fee": fee,
                "expected_net": exp_net,
                "pnl": 0.0,
                "equity": self.acc.equity,
                "live_usdc_balance": self.live_balance_usdc if self.live_balance_usdc is not None else "",
                "live_order_id": live_order_id,
                "live_status": live_status,
                "live_avg_price": live_avg_price,
                "live_filled_shares": live_filled_shares,
                "reason": f"crowd_snipe remain={market.end_ts - utc_now_ts()}s",
            }
        )

        self.store.save(self.acc, self.pos, self.traded)

    async def _wait_settlement(self) -> None:
        if not self.pos or self.pos.status != "OPEN":
            return

        pos = self.pos
        remain = pos.market_end_ts - utc_now_ts()
        log(f"[结算等待] {pos.market_slug} {pos.side} 进场价={pos.entry_ask:.4f} 剩余={remain}s", CYAN)

        deadline = pos.market_end_ts + CFG.SETTLEMENT_TIMEOUT_SEC

        while utc_now_ts() < deadline:
            await asyncio.sleep(1)

            if not self.pos or self.pos.status != "OPEN":
                return

            pos = self.pos

            up_token_id = pos.up_token_id
            down_token_id = pos.down_token_id
            if (not up_token_id or not down_token_id) and self.market and self.market.slug == pos.market_slug:
                up_token_id = self.market.up_token_id
                down_token_id = self.market.down_token_id

            winner = self.cache.get_winner_for_market(
                market_id=pos.market_id,
                up_token_id=up_token_id,
                down_token_id=down_token_id,
            )

            if winner:
                win = winner == pos.token_id
                self._settle(win=win, settle_price=1.0 if win else 0.0, reason="market_resolved")
                return

            remain = pos.market_end_ts - utc_now_ts()

            if remain > 0:
                log(f"  ⏱ {pos.market_slug} 剩余 {remain}s ...", DIM)
                continue

            book = self.cache.get_or_create(pos.token_id, pos.side)
            bid = book.best_bid

            if bid is not None and bid >= 0.95:
                self._settle(win=True, settle_price=bid, reason="bid_to_1")
                return

            if bid is None or bid <= 0.05:
                self._settle(win=False, settle_price=bid or 0.0, reason="bid_to_0")
                return

            log(f"  结算中... {pos.market_slug} bid={bid}", DIM)

        log("[结算] 等待超时，标记 EXPIRED，本次不计盈亏", YELLOW)

        if self.pos:
            self.pos.status = "EXPIRED"
            self._finalize_position(pnl=0.0, note="EXPIRED")

    def _close_position_at_bid(self, bid: float, reason: str) -> None:
        assert self.pos is not None

        exit_value = self.pos.shares * bid
        pnl = exit_value - self.pos.cost - self.pos.fee_paid

        if CFG.PAPER_MODE:
            log(
                f"🔻 影子平仓 {self.pos.side} bid={bid:.4f} exit_value={exit_value:.4f}U "
                f"pnl={pnl:+.4f}U reason={reason}",
                RED if pnl < 0 else GREEN,
            )
        else:
            res = self.live.sell_limit(self.pos.token_id, bid=bid, shares=self.pos.shares)
            if not res.ok:
                log(f"❌ 实盘卖出失败 {self.pos.side}: {res.error}", RED)
                self.journal.trade(
                    {
                        "time": now_iso(),
                        "mode": "LIVE",
                        "market_slug": self.pos.market_slug,
                        "event": "SELL_FAIL",
                        "side": self.pos.side,
                        "ask": self.pos.entry_ask,
                        "shares": self.pos.shares,
                        "cost": self.pos.cost,
                        "fee": self.pos.fee_paid,
                        "expected_net": self.pos.expected_net_profit,
                        "pnl": pnl,
                        "equity": self.acc.equity,
                        "live_usdc_balance": self.live_balance_usdc if self.live_balance_usdc is not None else "",
                        "live_order_id": res.order_id,
                        "live_status": res.status,
                        "live_avg_price": res.avg_price,
                        "live_filled_shares": res.filled_shares,
                        "reason": res.error,
                    }
                )
                return

            self.pos.live_sell_order_id = res.order_id
            exit_value = res.spent_u or exit_value
            pnl = exit_value - self.pos.cost - self.pos.fee_paid

            log(
                f"🔻 实盘卖出成功 {self.pos.side} bid={bid:.4f} avg≈{res.avg_price:.4f} "
                f"shares≈{res.filled_shares:.4f} pnl≈{pnl:+.4f}U order_id={res.order_id}",
                RED if pnl < 0 else GREEN,
            )
            self._refresh_live_balance("after_sell")

        self.pos.status = "CLOSED"
        self._finalize_position(pnl=pnl, note=reason)

    def _settle(self, win: bool, settle_price: float, reason: str = "") -> None:
        assert self.pos is not None

        if win:
            gross_pnl = self.pos.shares * (settle_price - self.pos.entry_ask)
            net_pnl = gross_pnl - self.pos.fee_paid
            self.pos.status = "WIN"
        else:
            net_pnl = -self.pos.cost - self.pos.fee_paid
            self.pos.status = "LOSE"

        color = GREEN if net_pnl >= 0 else RED

        log(
            f"{'🏆' if win else '💀'} 结算 {self.pos.side} {'赢' if win else '亏'} "
            f"settle_price={settle_price:.4f} net_pnl={net_pnl:+.4f}U reason={reason}",
            color,
        )

        self._finalize_position(pnl=net_pnl, note="WIN" if win else "LOSE")

    def _finalize_position(self, pnl: float, note: str) -> None:
        assert self.pos is not None

        self.pos.realized_pnl = pnl

        self.acc.equity += pnl
        self.acc.peak_equity = max(self.acc.peak_equity, self.acc.equity)
        self.acc.daily_pnl += pnl
        self.acc.total_pnl += pnl
        self.acc.total_trades += 1
        self.acc.trades_today += 1

        if pnl >= 0:
            self.acc.wins += 1
            self.acc.consecutive_losses = 0
        else:
            self.acc.losses += 1
            self.acc.consecutive_losses += 1

        if not CFG.PAPER_MODE:
            self._refresh_live_balance(f"finalize_{note}")

        self.journal.trade(
            {
                "time": now_iso(),
                "mode": "PAPER" if CFG.PAPER_MODE else "LIVE",
                "market_slug": self.pos.market_slug,
                "event": note,
                "side": self.pos.side,
                "ask": self.pos.entry_ask,
                "shares": self.pos.shares,
                "cost": self.pos.cost,
                "fee": self.pos.fee_paid,
                "expected_net": self.pos.expected_net_profit,
                "pnl": pnl,
                "equity": self.acc.equity,
                "live_usdc_balance": self.live_balance_usdc if self.live_balance_usdc is not None else "",
                "live_order_id": self.pos.live_sell_order_id or self.pos.live_order_id,
                "live_status": self.pos.live_status,
                "live_avg_price": self.pos.live_avg_entry_price,
                "live_filled_shares": self.pos.live_filled_shares,
                "reason": note,
            }
        )

        self.traded.append(self.pos.market_slug)
        self.pos = None
        self._cooldown_end = time.time() + CFG.COOLDOWN_AFTER_TRADE_SEC
        self.store.save(self.acc, None, self.traded)

    async def _loop(self) -> None:
        log("[策略] 等待 WS 连接...", YELLOW)
        await self.ws.connected.wait()
        log("[策略] 开始", GREEN)

        while True:
            try:
                self.acc.reset_daily()

                if self.pos and self.pos.status == "OPEN":
                    pos_remain = self.pos.market_end_ts - utc_now_ts()

                    if pos_remain <= 0:
                        await self._wait_settlement()
                    else:
                        book = self.cache.get_or_create(self.pos.token_id, self.pos.side)

                        if book.best_bid is not None:
                            floating_pnl = book.best_bid * self.pos.shares - self.pos.cost - self.pos.fee_paid

                            log(
                                f"[持仓观察] {self.pos.market_slug} {self.pos.side} "
                                f"remain={pos_remain}s bid={book.best_bid:.4f} 浮动PNL={floating_pnl:+.4f}U",
                                DIM,
                            )

                            if CFG.ENABLE_TAKE_PROFIT_AT_099 and book.best_bid >= CFG.TAKE_PROFIT_BID_PRICE:
                                log(
                                    f"[0.99止盈触发] {self.pos.side} bid={book.best_bid:.4f} "
                                    f">= {CFG.TAKE_PROFIT_BID_PRICE:.4f}，执行全部卖出",
                                    GREEN,
                                )
                                self._close_position_at_bid(book.best_bid, reason="TAKE_PROFIT_BID_0.99")
                                self.store.save(self.acc, self.pos, self.traded)
                                await asyncio.sleep(CFG.EVAL_INTERVAL_SEC)
                                continue

                            if CFG.ENABLE_MID_EXIT_STOP_LOSS and floating_pnl <= -CFG.STOP_LOSS_U:
                                log(f"[中途止损已开启] 浮亏={floating_pnl:.4f}U，按当前 bid 平仓", RED)
                                self._close_position_at_bid(book.best_bid, reason="STOP_LOSS")
                        else:
                            log(f"[持仓] {self.pos.market_slug} {self.pos.side} remain={pos_remain}s，暂无 bid，等待盘口", DIM)

                    self.store.save(self.acc, self.pos, self.traded)
                    await asyncio.sleep(CFG.EVAL_INTERVAL_SEC)
                    continue

                market = await self._refresh_market()

                if not market:
                    log("未找到市场，等待...", YELLOW)
                    await asyncio.sleep(3)
                    continue

                self._snipe(market)
                self.store.save(self.acc, self.pos, self.traded)

            except Exception as e:
                err = f"主循环异常: {e}\n{traceback.format_exc()}"
                log(err, RED)
                self.journal.error(err)

            await asyncio.sleep(CFG.EVAL_INTERVAL_SEC)

    async def run(self) -> None:
        self.banner()

        if not CFG.PAPER_MODE:
            self.live.init_if_needed()
            self._refresh_live_balance("startup")

        loop = asyncio.get_event_loop()
        market = await loop.run_in_executor(None, self.finder.find)

        if market:
            self.market = market
            self._last_refresh = time.time()

            token_map = {
                market.up_token_id: "UP",
                market.down_token_id: "DOWN",
            }

            for token_id, outcome in token_map.items():
                self.cache.reset(token_id, outcome)

            self.ws.set_tokens(token_map)

            log(f"[初始化] {market.slug}", CYAN)
            log(f"         UP={market.up_token_id[:14]}... DOWN={market.down_token_id[:14]}...", WHITE)
        else:
            log("[初始化] 暂未找到市场，WS 启动后持续重试", YELLOW)

        try:
            await asyncio.gather(self.ws.run(), self._loop())
        except asyncio.CancelledError:
            pass
        finally:
            self.ws.stop()
            self.store.save(self.acc, self.pos, self.traded)
            self._summary()

    def _summary(self) -> None:
        total = self.acc.total_trades
        win_rate = self.acc.wins / total * 100 if total else 0.0
        drawdown = ((self.acc.peak_equity - self.acc.equity) / self.acc.peak_equity * 100 if self.acc.peak_equity else 0.0)

        print(
            f"""
{BOLD}{CYAN}╔── 统计 ──────────────────────────────────────────────────────────╗{RESET}
  当前权益  : {self.acc.equity:.4f} U
  今日 PNL  : {self.acc.daily_pnl:+.4f} U
  累计 PNL  : {self.acc.total_pnl:+.4f} U
  交易次数  : {total}
  胜率      : {win_rate:.1f}%
  当前回撤  : {drawdown:.2f}%
  连续亏损  : {self.acc.consecutive_losses}
  日志目录  : {Path(CFG.DATA_DIR).resolve()}
{BOLD}{CYAN}╚──────────────────────────────────────────────────────────────────╝{RESET}
"""
        )


def main() -> None:
    engine = SniperEngine()

    try:
        asyncio.run(engine.run())
    except KeyboardInterrupt:
        print()
        log("用户中断", YELLOW)


if __name__ == "__main__":
    main()
