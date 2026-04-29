#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Polymarket BTC Up/Down 5m — 多参数组合并行影子盘评测器
=====================================================

用途：
    1. 复用你当前 polymarket_sniper_live.py 里的市场发现、WS盘口、REST盘口兜底；
    2. 不调用 LiveTrader，不需要私钥，不会真实下单；
    3. 同一份实时盘口下，让所有参数组合同时跑影子交易；
    4. 每个参数组合拥有独立权益、持仓、胜率、PNL、最大回撤；
    5. 跑一天后看 grid_summary.csv，找盈利最大、胜率最高、综合表现最稳的参数。

使用：
    把本文件放到 polymarket_sniper_live.py 同目录，然后运行：
        python polymarket_param_grid_shadow.py

输出目录：
    grid_shadow_data/
        grid_trades.csv       每个参数组合的买入/止盈/结算明细
        grid_summary.csv      所有参数组合的实时汇总排名
        grid_state.json       程序中断后可恢复的状态
        errors.log            错误日志

强安全限制：
    本程序强制 PAPER_MODE=True、LIVE_TRADING_ENABLED=False，且完全不初始化 LiveTrader。
"""

from __future__ import annotations

import asyncio
import csv
import itertools
import json
import os
import signal
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

try:
    import polymarket_sniper_live as base
except ImportError as e:
    print("❌ 找不到 polymarket_sniper_live.py。请把本文件放到 polymarket_sniper_live.py 同目录。")
    raise


# ─────────────────────────────────────────────────────────────
# 强制安全模式：永远不实盘
# ─────────────────────────────────────────────────────────────

base.CFG.PAPER_MODE = True
base.CFG.LIVE_TRADING_ENABLED = False
base.CFG.DEBUG_WS_EVENT_TYPES = False


# ─────────────────────────────────────────────────────────────
# 评测器配置
# ─────────────────────────────────────────────────────────────

@dataclass
class GridRunnerConfig:
    PROGRAM_NAME: str = "Polymarket BTC 5m 多参数组合并行影子盘评测器"
    DATA_DIR: str = "grid_shadow_data"

    # 主循环
    EVAL_INTERVAL_SEC: float = 0.30
    MARKET_REFRESH_SEC: int = 8
    BOOK_STALE_SEC: float = 5.0
    REST_BOOK_FALLBACK_ENABLED: bool = True
    REST_BOOK_FALLBACK_INTERVAL_SEC: float = 1.0
    REST_BOOK_TIMEOUT: float = 3.0
    SETTLEMENT_TIMEOUT_SEC: int = 45

    # 账户/风控：每个参数组合独立一套账户
    INIT_EQUITY: float = 19.0
    MIN_TRADE_BUDGET_U: float = 2.0
    ORDER_BUDGET_U: float = 18.0
    DAILY_MAX_LOSS_U: float = 15.0
    MAX_CONSECUTIVE_LOSSES: int = 5
    MAX_TRADES_PER_DAY: int = 288
    ONLY_ONE_TRADE_PER_WINDOW: bool = True

    # 输出
    PRINT_TOP_INTERVAL_SEC: float = 15.0
    SAVE_INTERVAL_SEC: float = 10.0
    TOP_N: int = 15
    MIN_TRADES_FOR_WINRATE_RANK: int = 5

    # 是否从 grid_state.json 恢复
    RESUME_STATE: bool = True


RCFG = GridRunnerConfig()


# ─────────────────────────────────────────────────────────────
# 参数网格：你主要调这里
# ─────────────────────────────────────────────────────────────

# 注意：下面不是“单个参数”，而是会做笛卡尔积组合。
# 参数组合数量 = 下方所有列表长度的笛卡尔积。默认只展开核心交易参数，手续费/止损/风控可按需加值。
SNIPE_WINDOWS: List[Tuple[int, int]] = [
    (10, 50),
    (15, 50),
    (20, 50),
    (30, 60),
]

# 监控/入场 ask 价格范围：这就是你说的“监控的价格范围”。
# 每个组合只会在 ask 落入对应区间时触发影子买入判断。
MONITOR_ASK_RANGES: List[Tuple[float, float]] = [
    (0.82, 0.93),
    (0.84, 0.93),
    (0.85, 0.93),
    (0.86, 0.93),
    (0.88, 0.93),
    (0.86, 0.92),
    (0.88, 0.92),
    (0.90, 0.94),
]

MAX_SPREADS: List[float] = [0.03, 0.05, 0.08]
MIN_BID_DEPTHS: List[float] = [5.0, 10.0, 20.0]
MIN_CROWD_RATIOS: List[float] = [1.10, 1.30, 1.50, 2.00]
MIN_NET_PROFITS_U: List[float] = [0.00, 0.01, 0.02]
TAKE_PROFIT_BIDS: List[float] = [0.97, 0.98, 0.99]

# 预算建议先固定，避免“谁下得多谁PNL大”的假象。真正比较时看 ROI / 每笔均值 / 回撤。
ORDER_BUDGETS_U: List[float] = [18.0]
MIN_TRADE_BUDGETS_U: List[float] = [2.0]

# 手续费估算参数。默认与原程序一致；需要评估不同手续费假设时，在列表里加值。
FEE_THETAS: List[float] = [0.05]
TAKER_REBATE_RATES: List[float] = [0.50]

# 中途止损参数。默认关闭；如果想测试止损，把 True 加进列表，例如 [False, True]。
ENABLE_MID_EXIT_STOP_LOSSES: List[bool] = [False]
STOP_LOSS_US: List[float] = [3.0]

# 每局是否只交易一次。默认与原程序一致。
ONLY_ONE_TRADE_PER_WINDOWS: List[bool] = [True]

# 到期后没有 market_resolved 事件时，用 bid 接近 1/0 作为兜底结算阈值。
WIN_SETTLE_BIDS: List[float] = [0.95]
LOSE_SETTLE_BIDS: List[float] = [0.05]

# 每个组合独立风控参数。默认与原程序一致；要测试更激进/保守时，在列表中加值。
DAILY_MAX_LOSS_US: List[float] = [15.0]
MAX_CONSECUTIVE_LOSSES_LIST: List[int] = [5]
MAX_TRADES_PER_DAYS: List[int] = [288]


# ─────────────────────────────────────────────────────────────
# 基础工具
# ─────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def ensure_dir() -> Path:
    p = Path(RCFG.DATA_DIR)
    p.mkdir(parents=True, exist_ok=True)
    return p


def log(msg: str, color: str = base.WHITE) -> None:
    base.log(msg, color)


class GridJournal:
    TRADE_FIELDS = [
        "time", "variant_id", "event", "market_slug", "side", "remain",
        "entry_ask", "exit_price", "shares", "cost", "fee", "expected_net",
        "pnl", "equity", "daily_pnl", "total_pnl", "wins", "losses", "win_rate",
        "snipe_min", "snipe_max", "min_ask", "max_ask", "max_spread",
        "min_bid_depth", "min_crowd_ratio", "min_net_profit_u", "take_profit_bid",
        "order_budget_u", "min_trade_budget_u", "fee_theta", "taker_rebate_rate",
        "enable_mid_stop_loss", "stop_loss_u", "only_one_trade_per_window",
        "win_settle_bid", "lose_settle_bid", "daily_max_loss_u",
        "max_consecutive_losses", "max_trades_per_day", "reason",
    ]

    SUMMARY_FIELDS = [
        "rank_pnl", "rank_score", "variant_id", "equity", "total_pnl", "daily_pnl",
        "roi_pct", "trades", "trades_today", "wins", "losses", "expired", "take_profits",
        "win_rate_pct", "avg_pnl", "max_drawdown_pct", "score",
        "snipe_min", "snipe_max", "min_ask", "max_ask", "max_spread",
        "min_bid_depth", "min_crowd_ratio", "min_net_profit_u", "take_profit_bid",
        "order_budget_u", "min_trade_budget_u", "fee_theta", "taker_rebate_rate",
        "enable_mid_stop_loss", "stop_loss_u", "only_one_trade_per_window",
        "win_settle_bid", "lose_settle_bid", "daily_max_loss_u",
        "max_consecutive_losses", "max_trades_per_day", "open_position",
    ]

    def __init__(self) -> None:
        self.dir = ensure_dir()
        self.trades_path = self.dir / "grid_trades.csv"
        self.summary_path = self.dir / "grid_summary.csv"
        self.error_path = self.dir / "errors.log"
        self.state_path = self.dir / "grid_state.json"

        if not self.trades_path.exists():
            with self.trades_path.open("w", newline="", encoding="utf-8-sig") as f:
                csv.DictWriter(f, fieldnames=self.TRADE_FIELDS, extrasaction="ignore").writeheader()

    def trade(self, row: Dict[str, Any]) -> None:
        with self.trades_path.open("a", newline="", encoding="utf-8-sig") as f:
            csv.DictWriter(f, fieldnames=self.TRADE_FIELDS, extrasaction="ignore").writerow(row)

    def error(self, msg: str) -> None:
        with self.error_path.open("a", encoding="utf-8") as f:
            f.write(f"[{now_iso()}] {msg}\n")

    def write_summary(self, rows: List[Dict[str, Any]]) -> None:
        tmp = self.summary_path.with_name(f"{self.summary_path.stem}.{os.getpid()}.tmp")
        with tmp.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=self.SUMMARY_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        tmp.replace(self.summary_path)

    def write_state(self, data: Dict[str, Any]) -> None:
        tmp = self.state_path.with_name(f"{self.state_path.stem}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)

    def load_state(self) -> Optional[Dict[str, Any]]:
        if not self.state_path.exists():
            return None
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return None


# ─────────────────────────────────────────────────────────────
# 多参数组合数据结构
# ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ParamSet:
    variant_id: str
    snipe_min: int
    snipe_max: int
    min_ask: float
    max_ask: float
    max_spread: float
    min_bid_depth: float
    min_crowd_ratio: float
    min_net_profit_u: float
    take_profit_bid: float
    order_budget_u: float
    min_trade_budget_u: float
    fee_theta: float
    taker_rebate_rate: float
    enable_mid_stop_loss: bool
    stop_loss_u: float
    only_one_trade_per_window: bool
    win_settle_bid: float
    lose_settle_bid: float
    daily_max_loss_u: float
    max_consecutive_losses: int
    max_trades_per_day: int


@dataclass
class ShadowPosition:
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
    market_id: str
    up_token_id: str
    down_token_id: str


@dataclass
class VariantState:
    params: ParamSet
    equity: float = RCFG.INIT_EQUITY
    peak_equity: float = RCFG.INIT_EQUITY
    daily_pnl: float = 0.0
    total_pnl: float = 0.0
    trades: int = 0
    trades_today: int = 0
    wins: int = 0
    losses: int = 0
    expired: int = 0
    take_profits: int = 0
    consecutive_losses: int = 0
    max_drawdown_pct: float = 0.0
    day: str = field(default_factory=lambda: date.today().isoformat())
    pos: Optional[ShadowPosition] = None
    traded_windows: Set[str] = field(default_factory=set)

    def reset_daily_if_needed(self) -> None:
        today = date.today().isoformat()
        if self.day != today:
            self.day = today
            self.daily_pnl = 0.0
            self.trades_today = 0
            self.consecutive_losses = 0
            self.traded_windows.clear()

    @property
    def win_rate_pct(self) -> float:
        return self.wins / self.trades * 100.0 if self.trades else 0.0

    @property
    def avg_pnl(self) -> float:
        return self.total_pnl / self.trades if self.trades else 0.0

    @property
    def roi_pct(self) -> float:
        return self.total_pnl / RCFG.INIT_EQUITY * 100.0 if RCFG.INIT_EQUITY else 0.0

    @property
    def score(self) -> float:
        """
        综合分：盈利优先，惩罚回撤，交易太少降权。
        你真正筛最终参数时，建议同时看 total_pnl、win_rate_pct、max_drawdown_pct、trades。
        """
        trade_factor = min(1.0, self.trades / max(1, RCFG.MIN_TRADES_FOR_WINRATE_RANK))
        return (self.total_pnl * 100.0 + self.win_rate_pct - self.max_drawdown_pct * 2.0) * trade_factor


def build_param_grid() -> List[ParamSet]:
    variants: List[ParamSet] = []
    i = 1
    for (
        (smin, smax),
        (amin, amax),
        spread,
        depth,
        crowd,
        min_profit,
        tp,
        budget,
        min_trade_budget,
        fee_theta,
        rebate_rate,
        enable_stop,
        stop_loss_u,
        only_one,
        win_settle_bid,
        lose_settle_bid,
        daily_max_loss_u,
        max_consecutive_losses,
        max_trades_per_day,
    ) in itertools.product(
        SNIPE_WINDOWS,
        MONITOR_ASK_RANGES,
        MAX_SPREADS,
        MIN_BID_DEPTHS,
        MIN_CROWD_RATIOS,
        MIN_NET_PROFITS_U,
        TAKE_PROFIT_BIDS,
        ORDER_BUDGETS_U,
        MIN_TRADE_BUDGETS_U,
        FEE_THETAS,
        TAKER_REBATE_RATES,
        ENABLE_MID_EXIT_STOP_LOSSES,
        STOP_LOSS_US,
        ONLY_ONE_TRADE_PER_WINDOWS,
        WIN_SETTLE_BIDS,
        LOSE_SETTLE_BIDS,
        DAILY_MAX_LOSS_US,
        MAX_CONSECUTIVE_LOSSES_LIST,
        MAX_TRADES_PER_DAYS,
    ):
        # 防止无效窗口
        if smin >= smax:
            continue
        # 防止无效价格区间
        if amin > amax:
            continue
        variants.append(
            ParamSet(
                variant_id=f"V{i:05d}",
                snipe_min=int(smin),
                snipe_max=int(smax),
                min_ask=float(amin),
                max_ask=float(amax),
                max_spread=float(spread),
                min_bid_depth=float(depth),
                min_crowd_ratio=float(crowd),
                min_net_profit_u=float(min_profit),
                take_profit_bid=float(tp),
                order_budget_u=float(budget),
                min_trade_budget_u=float(min_trade_budget),
                fee_theta=float(fee_theta),
                taker_rebate_rate=float(rebate_rate),
                enable_mid_stop_loss=bool(enable_stop),
                stop_loss_u=float(stop_loss_u),
                only_one_trade_per_window=bool(only_one),
                win_settle_bid=float(win_settle_bid),
                lose_settle_bid=float(lose_settle_bid),
                daily_max_loss_u=float(daily_max_loss_u),
                max_consecutive_losses=int(max_consecutive_losses),
                max_trades_per_day=int(max_trades_per_day),
            )
        )
        i += 1
    return variants


# ─────────────────────────────────────────────────────────────
# 主评测引擎
# ─────────────────────────────────────────────────────────────

class GridShadowEngine:
    def __init__(self) -> None:
        self.journal = GridJournal()
        self.finder = base.MarketFinder()
        self.cache = base.BookCache()
        self.ws = base.WsManager(self.cache, self.journal)

        self.market: Optional[base.MarketWindow] = None
        self._last_refresh = 0.0
        self._last_rest_book_fallback = 0.0
        self._last_print_top = 0.0
        self._last_save = 0.0
        self._running = True

        self.variants: List[VariantState] = [VariantState(params=p) for p in build_param_grid()]
        self.variant_by_id: Dict[str, VariantState] = {v.params.variant_id: v for v in self.variants}

        if RCFG.RESUME_STATE:
            self._load_state()

    def _load_state(self) -> None:
        state = self.journal.load_state()
        if not state:
            return

        restored = 0
        for raw in state.get("variants", []):
            vid = raw.get("params", {}).get("variant_id") or raw.get("variant_id")
            if not vid or vid not in self.variant_by_id:
                continue
            v = self.variant_by_id[vid]
            # 参数以当前脚本为准，只恢复统计和持仓
            for key in [
                "equity", "peak_equity", "daily_pnl", "total_pnl", "trades", "trades_today", "wins", "losses",
                "expired", "take_profits", "consecutive_losses", "max_drawdown_pct", "day",
            ]:
                if key in raw:
                    setattr(v, key, raw[key])
            v.traded_windows = set(raw.get("traded_windows", []))
            pos = raw.get("pos")
            if isinstance(pos, dict):
                try:
                    v.pos = ShadowPosition(**pos)
                except Exception:
                    v.pos = None
            restored += 1

        if restored:
            log(f"[恢复] 已从 grid_state.json 恢复 {restored} 个参数组合状态", base.YELLOW)

    def _serialize_state(self) -> Dict[str, Any]:
        rows = []
        for v in self.variants:
            d = asdict(v)
            d["traded_windows"] = sorted(list(v.traded_windows))[-500:]
            rows.append(d)
        return {
            "updated_at": now_iso(),
            "market_slug": self.market.slug if self.market else "",
            "variants": rows,
        }

    def _books_ready(self, up_book: base.Book, down_book: base.Book) -> Tuple[bool, str]:
        now = time.time()
        for book in (up_book, down_book):
            if book.updated_at <= 0:
                return False, f"[{book.outcome}] 盘口尚未初始化"
            stale = now - book.updated_at
            if stale > RCFG.BOOK_STALE_SEC:
                return False, f"[{book.outcome}] 盘口过期 {stale:.1f}s"
            if book.best_bid is None or book.best_ask is None:
                return False, f"[{book.outcome}] bid/ask 不完整 bid={book.best_bid} ask={book.best_ask}"
        return True, "OK"

    async def _refresh_market(self) -> Optional[base.MarketWindow]:
        now = time.time()
        if (
            self.market
            and now - self._last_refresh < RCFG.MARKET_REFRESH_SEC
            and self.market.end_ts > base.utc_now_ts() + 5
        ):
            return self.market

        loop = asyncio.get_event_loop()
        market = await loop.run_in_executor(None, self.finder.find)
        self._last_refresh = time.time()

        if market and (not self.market or market.slug != self.market.slug):
            self.market = market
            log(f"[市场] 切换到 {market.slug} remain={market.end_ts - base.utc_now_ts()}s", base.CYAN)
            log(f"       UP={market.up_token_id[:14]}... DOWN={market.down_token_id[:14]}...", base.WHITE)

            token_map = {
                market.up_token_id: "UP",
                market.down_token_id: "DOWN",
            }
            for token_id, outcome in token_map.items():
                self.cache.reset(token_id, outcome)
            self.ws.set_tokens(token_map)
            await self.ws.force_reconnect()

        return self.market

    def _fetch_book_rest_once(self, token_id: str) -> Optional[Dict[str, Any]]:
        urls = [
            f"{base.CFG.CLOB_BASE_URL}/book",
            f"{base.CFG.CLOB_BASE_URL}/orderbook",
        ]
        for url in urls:
            try:
                r = base.requests.get(
                    url,
                    params={"token_id": token_id},
                    timeout=RCFG.REST_BOOK_TIMEOUT,
                    headers={"User-Agent": "polymarket-grid-shadow/1.0"},
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

    def _try_rest_book_fallback(self, market: base.MarketWindow, force: bool = False) -> None:
        if not RCFG.REST_BOOK_FALLBACK_ENABLED:
            return

        now = time.time()
        if not force and now - self._last_rest_book_fallback < RCFG.REST_BOOK_FALLBACK_INTERVAL_SEC:
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

    @staticmethod
    def _calc_taker_fee(p: ParamSet, shares: float, price: float) -> float:
        gross = p.fee_theta * shares * price * (1.0 - price)
        net = gross * (1.0 - p.taker_rebate_rate)
        return max(0.0, net)

    @classmethod
    def _expected_net_profit(cls, p: ParamSet, ask: float, shares: float, win_prob: float) -> float:
        fee_buy = cls._calc_taker_fee(p, shares, ask)
        profit_win = shares * (1.0 - ask) - fee_buy
        profit_lose = -shares * ask - fee_buy
        return win_prob * profit_win + (1.0 - win_prob) * profit_lose

    def _variant_blocked(self, v: VariantState) -> Optional[str]:
        p = v.params
        v.reset_daily_if_needed()
        if v.daily_pnl <= -abs(p.daily_max_loss_u):
            return "DAILY_MAX_LOSS"
        if v.consecutive_losses >= p.max_consecutive_losses:
            return "MAX_CONSECUTIVE_LOSSES"
        if v.trades_today >= p.max_trades_per_day:
            return "MAX_TRADES_PER_DAY"
        if v.equity < p.min_trade_budget_u:
            return "LOW_EQUITY"
        return None

    @staticmethod
    def _choose_book(
        p: ParamSet,
        up_book: base.Book,
        down_book: base.Book,
    ) -> Tuple[Optional[base.Book], Optional[base.Book], str]:
        def price_in_range(book: base.Book) -> bool:
            ask = book.best_ask
            return ask is not None and p.min_ask <= ask <= p.max_ask

        up_ok = price_in_range(up_book)
        down_ok = price_in_range(down_book)
        up_depth = up_book.bid_depth_shares
        down_depth = down_book.bid_depth_shares

        if up_ok and not down_ok:
            return up_book, down_book, "UP_ONLY_PRICE_OK"
        if down_ok and not up_ok:
            return down_book, up_book, "DOWN_ONLY_PRICE_OK"
        if up_ok and down_ok:
            if up_depth >= down_depth * p.min_crowd_ratio:
                return up_book, down_book, "BOTH_OK_UP_DEPTH_STRONGER"
            if down_depth >= up_depth * p.min_crowd_ratio:
                return down_book, up_book, "BOTH_OK_DOWN_DEPTH_STRONGER"
            return None, None, "BOTH_OK_BUT_CROWD_NOT_ENOUGH"
        return None, None, "NO_PRICE_RANGE_MATCH"

    def _try_open_for_variant(
        self,
        v: VariantState,
        market: base.MarketWindow,
        up_book: base.Book,
        down_book: base.Book,
        remain: int,
    ) -> None:
        p = v.params

        if v.pos is not None:
            return
        if remain > p.snipe_max or remain < p.snipe_min:
            return
        if p.only_one_trade_per_window and market.slug in v.traded_windows:
            return
        if self._variant_blocked(v):
            return

        chosen, other, reason = self._choose_book(p, up_book, down_book)
        if chosen is None or other is None:
            return

        ask = chosen.best_ask
        spread = chosen.spread
        if ask is None:
            return
        if ask < p.min_ask or ask > p.max_ask:
            return
        if spread is None or spread > p.max_spread:
            return
        if chosen.bid_depth_shares < p.min_bid_depth:
            return

        budget = min(p.order_budget_u, v.equity)
        if budget < p.min_trade_budget_u:
            return

        shares = budget / ask
        fee = self._calc_taker_fee(p, shares, ask)
        crowd_ratio = chosen.bid_depth_shares / max(other.bid_depth_shares, 1e-9)
        win_prob = base.estimate_win_prob(ask, crowd_ratio, remain)
        exp_net = self._expected_net_profit(p, ask, shares, win_prob=win_prob)

        if exp_net < p.min_net_profit_u:
            return

        v.pos = ShadowPosition(
            market_slug=market.slug,
            side=chosen.outcome,
            token_id=chosen.token_id,
            entry_ask=ask,
            shares=shares,
            cost=shares * ask,
            fee_paid=fee,
            expected_net_profit=exp_net,
            entry_ts=base.utc_now_ts(),
            market_end_ts=market.end_ts,
            market_id=market.market_id,
            up_token_id=market.up_token_id,
            down_token_id=market.down_token_id,
        )

        self._write_trade_event(
            v=v,
            event="BUY",
            market_slug=market.slug,
            side=chosen.outcome,
            remain=remain,
            entry_ask=ask,
            exit_price="",
            shares=shares,
            cost=shares * ask,
            fee=fee,
            expected_net=exp_net,
            pnl=0.0,
            reason=f"{reason} crowd_ratio={crowd_ratio:.2f} win_prob≈{win_prob * 100:.1f}%",
        )

    def _check_position_for_variant(self, v: VariantState) -> None:
        pos = v.pos
        if pos is None:
            return

        p = v.params
        remain = pos.market_end_ts - base.utc_now_ts()
        book = self.cache.get_or_create(pos.token_id, pos.side)
        bid = book.best_bid

        # 未到期：先看止盈，再按参数决定是否启用中途止损
        if remain > 0:
            if bid is not None and bid >= p.take_profit_bid:
                exit_value = pos.shares * bid
                pnl = exit_value - pos.cost - pos.fee_paid
                v.take_profits += 1
                self._finalize(v, pnl=pnl, event="TAKE_PROFIT", exit_price=bid, reason=f"bid>={p.take_profit_bid:.2f}")
                return

            if p.enable_mid_stop_loss and bid is not None:
                exit_value = pos.shares * bid
                floating_pnl = exit_value - pos.cost - pos.fee_paid
                if floating_pnl <= -abs(p.stop_loss_u):
                    self._finalize(
                        v,
                        pnl=floating_pnl,
                        event="STOP_LOSS",
                        exit_price=bid,
                        reason=f"floating_pnl<={-abs(p.stop_loss_u):.2f}U",
                    )
            return

        # 到期后：优先使用 market_resolved
        winner = self.cache.get_winner_for_market(
            market_id=pos.market_id,
            up_token_id=pos.up_token_id,
            down_token_id=pos.down_token_id,
        )
        if winner:
            win = winner == pos.token_id
            if win:
                settle_price = 1.0
                pnl = pos.shares * (settle_price - pos.entry_ask) - pos.fee_paid
                self._finalize(v, pnl=pnl, event="WIN", exit_price=settle_price, reason="market_resolved")
            else:
                settle_price = 0.0
                pnl = -pos.cost - pos.fee_paid
                self._finalize(v, pnl=pnl, event="LOSE", exit_price=settle_price, reason="market_resolved")
            return

        # 无 resolved 事件时，用盘口接近 1/0 兜底
        if bid is not None and bid >= p.win_settle_bid:
            settle_price = bid
            pnl = pos.shares * (settle_price - pos.entry_ask) - pos.fee_paid
            self._finalize(v, pnl=pnl, event="WIN", exit_price=settle_price, reason="bid_to_1")
            return

        if bid is None or bid <= p.lose_settle_bid:
            settle_price = bid or 0.0
            pnl = -pos.cost - pos.fee_paid
            self._finalize(v, pnl=pnl, event="LOSE", exit_price=settle_price, reason="bid_to_0")
            return

        # 超时仍无法判定，不计盈亏，但记 expired
        if base.utc_now_ts() > pos.market_end_ts + RCFG.SETTLEMENT_TIMEOUT_SEC:
            v.expired += 1
            self._finalize(v, pnl=0.0, event="EXPIRED", exit_price=bid if bid is not None else "", reason="settlement_timeout")

    def _finalize(self, v: VariantState, pnl: float, event: str, exit_price: Any, reason: str) -> None:
        pos = v.pos
        if pos is None:
            return

        v.equity += pnl
        v.peak_equity = max(v.peak_equity, v.equity)
        v.daily_pnl += pnl
        v.total_pnl += pnl
        v.trades += 1
        v.trades_today += 1

        dd = (v.peak_equity - v.equity) / v.peak_equity * 100.0 if v.peak_equity else 0.0
        v.max_drawdown_pct = max(v.max_drawdown_pct, dd)

        if pnl >= 0:
            if event != "EXPIRED":
                v.wins += 1
            v.consecutive_losses = 0
        else:
            v.losses += 1
            v.consecutive_losses += 1

        v.traded_windows.add(pos.market_slug)

        self._write_trade_event(
            v=v,
            event=event,
            market_slug=pos.market_slug,
            side=pos.side,
            remain=pos.market_end_ts - base.utc_now_ts(),
            entry_ask=pos.entry_ask,
            exit_price=exit_price,
            shares=pos.shares,
            cost=pos.cost,
            fee=pos.fee_paid,
            expected_net=pos.expected_net_profit,
            pnl=pnl,
            reason=reason,
        )

        v.pos = None

    def _write_trade_event(
        self,
        v: VariantState,
        event: str,
        market_slug: str,
        side: str,
        remain: int,
        entry_ask: Any,
        exit_price: Any,
        shares: float,
        cost: float,
        fee: float,
        expected_net: float,
        pnl: float,
        reason: str,
    ) -> None:
        p = v.params
        self.journal.trade(
            {
                "time": now_iso(),
                "variant_id": p.variant_id,
                "event": event,
                "market_slug": market_slug,
                "side": side,
                "remain": remain,
                "entry_ask": entry_ask,
                "exit_price": exit_price,
                "shares": shares,
                "cost": cost,
                "fee": fee,
                "expected_net": expected_net,
                "pnl": pnl,
                "equity": v.equity,
                "daily_pnl": v.daily_pnl,
                "total_pnl": v.total_pnl,
                "wins": v.wins,
                "losses": v.losses,
                "win_rate": v.win_rate_pct,
                **asdict(p),
                "reason": reason,
            }
        )

    def _summary_rows(self) -> List[Dict[str, Any]]:
        by_pnl = sorted(self.variants, key=lambda v: v.total_pnl, reverse=True)
        pnl_rank: Dict[str, int] = {v.params.variant_id: i + 1 for i, v in enumerate(by_pnl)}
        by_score = sorted(self.variants, key=lambda v: v.score, reverse=True)
        score_rank: Dict[str, int] = {v.params.variant_id: i + 1 for i, v in enumerate(by_score)}

        rows: List[Dict[str, Any]] = []
        for v in by_pnl:
            p = v.params
            rows.append(
                {
                    "rank_pnl": pnl_rank[p.variant_id],
                    "rank_score": score_rank[p.variant_id],
                    "variant_id": p.variant_id,
                    "equity": round(v.equity, 6),
                    "total_pnl": round(v.total_pnl, 6),
                    "daily_pnl": round(v.daily_pnl, 6),
                    "roi_pct": round(v.roi_pct, 4),
                    "trades": v.trades,
                    "wins": v.wins,
                    "losses": v.losses,
                    "expired": v.expired,
                    "take_profits": v.take_profits,
                    "win_rate_pct": round(v.win_rate_pct, 4),
                    "avg_pnl": round(v.avg_pnl, 6),
                    "max_drawdown_pct": round(v.max_drawdown_pct, 4),
                    "score": round(v.score, 6),
                    "snipe_min": p.snipe_min,
                    "snipe_max": p.snipe_max,
                    "min_ask": p.min_ask,
                    "max_ask": p.max_ask,
                    "max_spread": p.max_spread,
                    "min_bid_depth": p.min_bid_depth,
                    "min_crowd_ratio": p.min_crowd_ratio,
                    "min_net_profit_u": p.min_net_profit_u,
                    "take_profit_bid": p.take_profit_bid,
                    "order_budget_u": p.order_budget_u,
                    "min_trade_budget_u": p.min_trade_budget_u,
                    "fee_theta": p.fee_theta,
                    "taker_rebate_rate": p.taker_rebate_rate,
                    "enable_mid_stop_loss": p.enable_mid_stop_loss,
                    "stop_loss_u": p.stop_loss_u,
                    "only_one_trade_per_window": p.only_one_trade_per_window,
                    "win_settle_bid": p.win_settle_bid,
                    "lose_settle_bid": p.lose_settle_bid,
                    "daily_max_loss_u": p.daily_max_loss_u,
                    "max_consecutive_losses": p.max_consecutive_losses,
                    "max_trades_per_day": p.max_trades_per_day,
                    "open_position": "YES" if v.pos else "NO",
                }
            )
        return rows

    def _save_all(self) -> None:
        rows = self._summary_rows()
        self.journal.write_summary(rows)
        self.journal.write_state(self._serialize_state())

    def _print_top(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_print_top < RCFG.PRINT_TOP_INTERVAL_SEC:
            return
        self._last_print_top = now

        active_positions = sum(1 for v in self.variants if v.pos is not None)
        rows = self._summary_rows()
        top_pnl = rows[: RCFG.TOP_N]
        top_winrate = sorted(
            [r for r in rows if int(r["trades"]) >= RCFG.MIN_TRADES_FOR_WINRATE_RANK],
            key=lambda r: (float(r["win_rate_pct"]), float(r["total_pnl"]), -float(r["max_drawdown_pct"])),
            reverse=True,
        )[: RCFG.TOP_N]

        market_info = "暂无市场"
        if self.market:
            market_info = f"{self.market.slug} remain={self.market.end_ts - base.utc_now_ts()}s"

        print()
        log(
            f"[排行榜] variants={len(self.variants)} open={active_positions} market={market_info} "
            f"summary={self.journal.summary_path.resolve()}",
            base.CYAN,
        )
        print("按累计PNL Top:")
        print("rank variant pnl winrate trades avg_pnl maxDD params")
        for r in top_pnl[: min(8, len(top_pnl))]:
            print(
                f"#{r['rank_pnl']:<4} {r['variant_id']} "
                f"pnl={float(r['total_pnl']):+8.4f}U "
                f"wr={float(r['win_rate_pct']):5.1f}% "
                f"n={int(r['trades']):3d} avg={float(r['avg_pnl']):+7.4f} "
                f"dd={float(r['max_drawdown_pct']):5.2f}% "
                f"win={r['snipe_min']}-{r['snipe_max']}s ask={r['min_ask']}-{r['max_ask']} "
                f"sp={r['max_spread']} depth={r['min_bid_depth']} crowd={r['min_crowd_ratio']} "
                f"tp={r['take_profit_bid']} minNet={r['min_net_profit_u']}"
            )

        if top_winrate:
            print("按胜率 Top，已过滤交易次数太少的组合:")
            for i, r in enumerate(top_winrate[: min(5, len(top_winrate))], start=1):
                print(
                    f"#{i:<2} {r['variant_id']} "
                    f"wr={float(r['win_rate_pct']):5.1f}% "
                    f"pnl={float(r['total_pnl']):+8.4f}U "
                    f"n={int(r['trades']):3d} dd={float(r['max_drawdown_pct']):5.2f}% "
                    f"win={r['snipe_min']}-{r['snipe_max']}s ask={r['min_ask']}-{r['max_ask']} "
                    f"sp={r['max_spread']} depth={r['min_bid_depth']} crowd={r['min_crowd_ratio']} "
                    f"tp={r['take_profit_bid']} minNet={r['min_net_profit_u']}"
                )
        print()

    async def _loop(self) -> None:
        log("[策略] 等待 WS 连接...", base.YELLOW)
        await self.ws.connected.wait()
        log("[策略] 多参数影子评测开始", base.GREEN)

        while self._running:
            try:
                market = await self._refresh_market()
                if not market:
                    log("未找到市场，等待...", base.YELLOW)
                    await asyncio.sleep(3)
                    continue

                up_book = self.cache.get_or_create(market.up_token_id, "UP")
                down_book = self.cache.get_or_create(market.down_token_id, "DOWN")
                ready, reason = self._books_ready(up_book, down_book)
                if not ready:
                    self._try_rest_book_fallback(market)
                    ready, reason = self._books_ready(up_book, down_book)

                if ready:
                    remain = market.end_ts - base.utc_now_ts()

                    # 先处理所有已开仓组合的止盈/结算
                    for v in self.variants:
                        v.reset_daily_if_needed()
                        if v.pos is not None:
                            self._check_position_for_variant(v)

                    # 再让所有空仓组合同时判断是否开仓
                    for v in self.variants:
                        if v.pos is None:
                            self._try_open_for_variant(v, market, up_book, down_book, remain)
                else:
                    # 降低噪音，只偶尔输出
                    if time.time() - self._last_print_top > RCFG.PRINT_TOP_INTERVAL_SEC:
                        log(f"[盘口] {reason}，等待", base.DIM)

                now = time.time()
                if now - self._last_save >= RCFG.SAVE_INTERVAL_SEC:
                    self._save_all()
                    self._last_save = now

                self._print_top()

            except Exception as e:
                err = f"主循环异常: {e}\n{traceback.format_exc()}"
                log(err, base.RED)
                self.journal.error(err)

            await asyncio.sleep(RCFG.EVAL_INTERVAL_SEC)

    async def run(self) -> None:
        self.banner()

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
            log(f"[初始化] {market.slug}", base.CYAN)
            log(f"         UP={market.up_token_id[:14]}... DOWN={market.down_token_id[:14]}...", base.WHITE)
        else:
            log("[初始化] 暂未找到市场，WS 启动后持续重试", base.YELLOW)

        def _handle_stop(*_: Any) -> None:
            self._running = False
            self.ws.stop()

        try:
            signal.signal(signal.SIGINT, _handle_stop)
            signal.signal(signal.SIGTERM, _handle_stop)
        except Exception:
            pass

        try:
            await asyncio.gather(self.ws.run(), self._loop())
        except asyncio.CancelledError:
            pass
        finally:
            self.ws.stop()
            self._save_all()
            self._print_top(force=True)
            log(f"[退出] 已保存：{self.journal.summary_path.resolve()}", base.GREEN)

    def banner(self) -> None:
        variants = len(self.variants)
        print(
            f"""
{base.BOLD}{base.CYAN}╔════════════════════════════════════════════════════════════════════╗
║  {RCFG.PROGRAM_NAME:<66s}║
╠════════════════════════════════════════════════════════════════════╣
║  模式        : 影子盘 / 不下实盘 / 不需要私钥                      ║
║  参数组合数  : {variants:<54d}║
║  初始权益    : {RCFG.INIT_EQUITY:<54.2f}║
║  固定预算    : {ORDER_BUDGETS_U[0]:<54.2f}║
║  输出目录    : {str(Path(RCFG.DATA_DIR).resolve()):<54s}║
║  看结果      : grid_summary.csv / grid_trades.csv                  ║
╚════════════════════════════════════════════════════════════════════╝{base.RESET}
"""
        )


def main() -> None:
    engine = GridShadowEngine()
    asyncio.run(engine.run())


if __name__ == "__main__":
    main()
