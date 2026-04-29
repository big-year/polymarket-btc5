#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Polymarket 多参数影子盘排行榜交互式 CLI
========================================

读取 grid_shadow_data/grid_summary.csv / grid_trades.csv，查看多参数影子盘结果。

重点：支持多字段优先级排序。
例如：
    winrate,pnl      先按胜率，再按累计盈利
    pnl,winrate      先按累计盈利，再按胜率
    score,pnl,trades 先按综合评分，再按累计盈利，再按交易次数

交互运行：
    python grid_rank_viewer.py

一次性查看：
    python grid_rank_viewer.py --once --sort winrate,pnl --min-trades 20 --top 30
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


DEFAULT_DATA_DIR = "grid_shadow_data"

SORT_OPTIONS = ["pnl", "score", "winrate", "avg", "drawdown", "trades", "roi"]
SORT_LABELS = {
    "pnl": "累计盈利 total_pnl 最大",
    "score": "综合评分 score 最大",
    "winrate": "胜率 win_rate_pct 最大",
    "avg": "单笔均值 avg_pnl 最大",
    "drawdown": "最大回撤 max_drawdown_pct 最小",
    "trades": "交易次数 trades 最多",
    "roi": "ROI roi_pct 最大",
}

# 每种排序对应的 CSV 字段与方向。True = 越大越好；False = 越小越好。
SORT_FIELDS: Dict[str, Tuple[str, bool]] = {
    "pnl": ("total_pnl", True),
    "score": ("score", True),
    "winrate": ("win_rate_pct", True),
    "avg": ("avg_pnl", True),
    "drawdown": ("max_drawdown_pct", False),
    "trades": ("trades", True),
    "roi": ("roi_pct", True),
}

SORT_ALIASES = {
    "1": "pnl",
    "2": "score",
    "3": "winrate",
    "4": "avg",
    "5": "drawdown",
    "6": "trades",
    "7": "roi",
    "profit": "pnl",
    "profits": "pnl",
    "total_pnl": "pnl",
    "pnl": "pnl",
    "score": "score",
    "wr": "winrate",
    "win": "winrate",
    "winrate": "winrate",
    "win_rate": "winrate",
    "win_rate_pct": "winrate",
    "avg": "avg",
    "avg_pnl": "avg",
    "dd": "drawdown",
    "drawdown": "drawdown",
    "maxdd": "drawdown",
    "max_drawdown_pct": "drawdown",
    "n": "trades",
    "trade": "trades",
    "trades": "trades",
    "roi": "roi",
    "roi_pct": "roi",
}


# ─────────────────────────────────────────────────────────────
# 基础工具
# ─────────────────────────────────────────────────────────────


def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def pause(msg: str = "按回车继续...") -> None:
    try:
        input(msg)
    except (EOFError, KeyboardInterrupt):
        pass


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        s = str(v).strip()
        if s == "":
            return default
        return float(s)
    except Exception:
        return default


def safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None:
            return default
        s = str(v).strip()
        if s == "":
            return default
        return int(float(s))
    except Exception:
        return default


def yes_no(v: Any) -> bool:
    return str(v).strip().upper() in {"YES", "Y", "TRUE", "1", "OPEN"}


def fmt_num(v: Any, width: int = 10, digits: int = 4, signed: bool = False) -> str:
    try:
        x = float(v)
        if signed:
            return f"{x:+{width}.{digits}f}"
        return f"{x:{width}.{digits}f}"
    except Exception:
        return "-".rjust(width)


def fmt_pct(v: Any, width: int = 7, digits: int = 2) -> str:
    return fmt_num(v, width=width, digits=digits, signed=False)


def format_time_from_path(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "UNKNOWN"


def short_bool(v: Any) -> str:
    return "Y" if yes_no(v) else "N"


def normalize_sort_token(token: str) -> Optional[str]:
    t = str(token).strip().lower().replace(" ", "")
    if not t:
        return None
    return SORT_ALIASES.get(t)


def parse_sort_keys(raw: str, default: Optional[List[str]] = None) -> List[str]:
    """
    支持：
        3,1
        winrate,pnl
        3 1
        winrate pnl
        3>1>7
    """
    if default is None:
        default = ["score"]
    if raw is None:
        return default[:]

    cleaned = str(raw).strip()
    if not cleaned:
        return default[:]

    for sep in [">", "，", ";", "|", "/"]:
        cleaned = cleaned.replace(sep, ",")
    cleaned = cleaned.replace(" ", ",")

    out: List[str] = []
    for part in cleaned.split(","):
        key = normalize_sort_token(part)
        if key and key not in out:
            out.append(key)
    return out or default[:]


def sort_keys_text(sort_keys: Sequence[str]) -> str:
    return ",".join(sort_keys)


def sort_keys_label(sort_keys: Sequence[str]) -> str:
    return " > ".join(f"{k}({SORT_LABELS.get(k, '')})" for k in sort_keys)


# ─────────────────────────────────────────────────────────────
# 数据读取 / 排序 / 过滤
# ─────────────────────────────────────────────────────────────


def load_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"找不到文件：{path}")
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def sort_value(row: Dict[str, str], sort_key: str) -> float:
    field, _desc = SORT_FIELDS[sort_key]
    if field == "trades":
        return float(safe_int(row.get(field)))
    return safe_float(row.get(field))


def sort_rows(rows: List[Dict[str, str]], sort_keys: Sequence[str]) -> List[Dict[str, str]]:
    """
    多字段优先级排序。
    使用稳定排序：从最后一个优先级开始排，最终第一个优先级权重最高。
    """
    keys = [k for k in sort_keys if k in SORT_FIELDS] or ["score"]
    out = list(rows)

    # 固定兜底：同等条件下按 variant_id 稳定排序，避免每次显示跳动。
    out.sort(key=lambda r: str(r.get("variant_id", "")))

    for key in reversed(keys):
        _field, desc = SORT_FIELDS[key]
        out.sort(key=lambda r, k=key: sort_value(r, k), reverse=desc)
    return out


@dataclass
class ViewConfig:
    data_dir: Path
    summary_path: Path
    trades_path: Path
    sort_keys: List[str]
    top: int = 20
    min_trades: int = 0
    only_open: bool = False
    profitable_only: bool = False
    ask_filter: str = ""
    snipe_filter: str = ""
    variant_filter: str = ""


def build_paths(data_dir: str, summary: str = "", trades: str = "") -> Tuple[Path, Path, Path]:
    data_dir_path = Path(data_dir)
    summary_path = Path(summary) if summary else data_dir_path / "grid_summary.csv"
    trades_path = Path(trades) if trades else data_dir_path / "grid_trades.csv"
    return data_dir_path, summary_path, trades_path


def apply_filters(rows: List[Dict[str, str]], cfg: ViewConfig) -> List[Dict[str, str]]:
    out = rows
    if cfg.min_trades > 0:
        out = [r for r in out if safe_int(r.get("trades")) >= cfg.min_trades]
    if cfg.only_open:
        out = [r for r in out if yes_no(r.get("open_position"))]
    if cfg.profitable_only:
        out = [r for r in out if safe_float(r.get("total_pnl")) > 0]
    if cfg.ask_filter:
        f = cfg.ask_filter.strip()
        if "-" in f:
            lo, hi = [x.strip() for x in f.split("-", 1)]
            out = [r for r in out if str(r.get("min_ask", "")) == lo and str(r.get("max_ask", "")) == hi]
        else:
            out = [r for r in out if str(r.get("min_ask", "")) == f or str(r.get("max_ask", "")) == f]
    if cfg.snipe_filter:
        f = cfg.snipe_filter.strip().lower().replace("s", "")
        if "-" in f:
            lo, hi = [x.strip() for x in f.split("-", 1)]
            out = [r for r in out if str(r.get("snipe_min", "")) == lo and str(r.get("snipe_max", "")) == hi]
        else:
            out = [r for r in out if str(r.get("snipe_min", "")) == f or str(r.get("snipe_max", "")) == f]
    if cfg.variant_filter:
        v = cfg.variant_filter.strip().upper()
        out = [r for r in out if v in str(r.get("variant_id", "")).upper()]
    return out


def get_current_rows(cfg: ViewConfig) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    rows = load_csv(cfg.summary_path)
    filtered = apply_filters(rows, cfg)
    sorted_rows = sort_rows(filtered, cfg.sort_keys)
    return rows, sorted_rows


# ─────────────────────────────────────────────────────────────
# 打印视图
# ─────────────────────────────────────────────────────────────


def print_header(cfg: ViewConfig, total_rows: int, shown_rows: int) -> None:
    print("=" * 126)
    print("Polymarket 多参数影子盘排行榜 CLI")
    print("=" * 126)
    print(f"时间          : {now_str()}")
    print(f"summary       : {cfg.summary_path.resolve()}")
    print(f"summary更新时间: {format_time_from_path(cfg.summary_path)}")
    print(f"trades        : {cfg.trades_path.resolve()}")
    print(
        f"排序优先级    : {sort_keys_text(cfg.sort_keys)} | {sort_keys_label(cfg.sort_keys)}\n"
        f"显示参数      : Top={cfg.top} | 最少交易={cfg.min_trades} | 显示={shown_rows}/{total_rows}"
    )
    filters = []
    if cfg.only_open:
        filters.append("只看持仓中")
    if cfg.profitable_only:
        filters.append("只看盈利")
    if cfg.ask_filter:
        filters.append(f"ask={cfg.ask_filter}")
    if cfg.snipe_filter:
        filters.append(f"time={cfg.snipe_filter}")
    if cfg.variant_filter:
        filters.append(f"variant包含={cfg.variant_filter}")
    print("过滤          : " + (" | ".join(filters) if filters else "无"))
    print("-" * 126)


def print_table(rows: List[Dict[str, str]], cfg: ViewConfig, clear: bool = False) -> None:
    if clear:
        clear_screen()

    all_rows = load_csv(cfg.summary_path)
    print_header(cfg, total_rows=len(all_rows), shown_rows=len(rows))

    if not rows:
        print("暂无符合条件的记录。可能是还没成交，或者过滤条件太严格。")
        return

    limit = max(1, int(cfg.top))
    rows = rows[:limit]

    header = (
        f"{'#':>3} {'variant':<7} {'pnl':>10} {'roi%':>8} {'wr%':>7} {'n':>5} "
        f"{'avg':>9} {'maxDD%':>8} {'score':>10} {'tp':>4} {'open':>4} "
        f"{'win':>7} {'ask':>11} {'sp':>5} {'depth':>6} {'crowd':>6} {'take':>5} {'minNet':>7}"
    )
    print(header)
    print("-" * len(header))

    for i, r in enumerate(rows, start=1):
        win = f"{r.get('snipe_min','')}-{r.get('snipe_max','')}"
        ask = f"{r.get('min_ask','')}-{r.get('max_ask','')}"
        line = (
            f"{i:>3} {r.get('variant_id',''):<7} "
            f"{fmt_num(r.get('total_pnl'), 10, 4, signed=True)} "
            f"{fmt_pct(r.get('roi_pct'), 8, 2)} "
            f"{fmt_pct(r.get('win_rate_pct'), 7, 2)} "
            f"{safe_int(r.get('trades')):>5} "
            f"{fmt_num(r.get('avg_pnl'), 9, 4, signed=True)} "
            f"{fmt_pct(r.get('max_drawdown_pct'), 8, 2)} "
            f"{fmt_num(r.get('score'), 10, 2)} "
            f"{safe_int(r.get('take_profit_count')):>4} "
            f"{short_bool(r.get('open_position')):>4} "
            f"{win:>7} {ask:>11} "
            f"{r.get('max_spread',''):>5} {r.get('min_bid_depth',''):>6} "
            f"{r.get('min_crowd_ratio',''):>6} {r.get('take_profit_bid',''):>5} {r.get('min_net_profit_u',''):>7}"
        )
        print(line)

    print()
    print("说明：排序支持多字段优先级，例如 winrate,pnl = 先胜率，再累计盈利；pnl,winrate = 先盈利，再胜率。")


def print_stats(rows: List[Dict[str, str]], cfg: ViewConfig) -> None:
    total = len(rows)
    if total <= 0:
        print("暂无数据。")
        return

    traded = [r for r in rows if safe_int(r.get("trades")) > 0]
    profitable = [r for r in rows if safe_float(r.get("total_pnl")) > 0]
    open_rows = [r for r in rows if yes_no(r.get("open_position"))]

    best_pnl = max(rows, key=lambda r: safe_float(r.get("total_pnl")))
    best_score = max(rows, key=lambda r: safe_float(r.get("score")))
    best_wr_candidates = [r for r in rows if safe_int(r.get("trades")) >= max(1, cfg.min_trades)]
    best_wr = max(best_wr_candidates, key=lambda r: safe_float(r.get("win_rate_pct"))) if best_wr_candidates else None

    print("整体统计")
    print("-" * 80)
    print(f"总组合数        : {total}")
    print(f"已有交易组合    : {len(traded)}")
    print(f"盈利组合数      : {len(profitable)}")
    print(f"当前持仓组合    : {len(open_rows)}")
    print(f"最高累计盈利    : {best_pnl.get('variant_id')} pnl={safe_float(best_pnl.get('total_pnl')):+.4f}U wr={safe_float(best_pnl.get('win_rate_pct')):.2f}% n={safe_int(best_pnl.get('trades'))}")
    print(f"最高综合评分    : {best_score.get('variant_id')} score={safe_float(best_score.get('score')):.2f} pnl={safe_float(best_score.get('total_pnl')):+.4f}U n={safe_int(best_score.get('trades'))}")
    if best_wr:
        print(f"最高胜率        : {best_wr.get('variant_id')} wr={safe_float(best_wr.get('win_rate_pct')):.2f}% pnl={safe_float(best_wr.get('total_pnl')):+.4f}U n={safe_int(best_wr.get('trades'))}")


def print_variant_detail(cfg: ViewConfig, variant_id: str) -> None:
    rows = load_csv(cfg.summary_path)
    v = variant_id.strip().upper()
    matches = [r for r in rows if str(r.get("variant_id", "")).upper() == v]
    if not matches:
        matches = [r for r in rows if v in str(r.get("variant_id", "")).upper()]
    if not matches:
        print(f"找不到参数组合：{variant_id}")
        return

    r = matches[0]
    print("参数组合详情")
    print("-" * 80)
    preferred = [
        "variant_id", "total_pnl", "roi_pct", "win_rate_pct", "trades", "wins", "losses",
        "avg_pnl", "max_drawdown_pct", "score", "take_profit_count", "expired_count",
        "open_position", "snipe_min", "snipe_max", "min_ask", "max_ask", "max_spread",
        "min_bid_depth", "min_crowd_ratio", "take_profit_bid", "min_net_profit_u",
        "order_budget_u", "min_trade_budget_u", "fee_theta", "taker_rebate_rate",
        "enable_mid_exit_stop_loss", "stop_loss_u", "only_one_trade_per_window",
        "win_settle_bid", "lose_settle_bid", "daily_max_loss_u", "max_consecutive_losses",
        "max_trades_per_day",
    ]
    printed = set()
    for k in preferred:
        if k in r:
            print(f"{k:<30}: {r.get(k, '')}")
            printed.add(k)
    for k in sorted(r.keys()):
        if k not in printed:
            print(f"{k:<30}: {r.get(k, '')}")

    if cfg.trades_path.exists():
        trades = load_csv(cfg.trades_path)
        vt = [t for t in trades if str(t.get("variant_id", "")).upper() == str(r.get("variant_id", "")).upper()]
        print("-" * 80)
        print(f"该组合交易明细数量: {len(vt)}")
        for t in vt[-10:]:
            print(
                f"{t.get('time','')} {t.get('market_slug','')} {t.get('event','')} "
                f"{t.get('side','')} pnl={t.get('pnl','')} equity={t.get('equity','')} reason={t.get('reason','')}"
            )


def print_recent_trades(cfg: ViewConfig, limit: int = 30) -> None:
    if not cfg.trades_path.exists():
        print(f"找不到交易明细：{cfg.trades_path}")
        return
    trades = load_csv(cfg.trades_path)
    if cfg.variant_filter:
        v = cfg.variant_filter.strip().upper()
        trades = [t for t in trades if v in str(t.get("variant_id", "")).upper()]
    trades = trades[-max(1, limit):]
    print(f"最近 {len(trades)} 条交易明细")
    print("-" * 120)
    for t in trades:
        print(
            f"{t.get('time','')} {t.get('variant_id',''):<7} {t.get('market_slug','')} "
            f"{t.get('event',''):<14} {t.get('side',''):<4} "
            f"ask={t.get('ask','')} shares={t.get('shares','')} pnl={t.get('pnl','')} "
            f"eq={t.get('equity','')} reason={t.get('reason','')}"
        )


def export_top(cfg: ViewConfig, rows: List[Dict[str, str]]) -> Path:
    out_path = cfg.data_dir / f"rank_export_{sort_keys_text(cfg.sort_keys).replace(',', '_')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    selected = rows[: max(1, cfg.top)]
    if not selected:
        raise RuntimeError("没有可导出的记录。")
    fields = list(selected[0].keys())
    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(selected)
    return out_path


# ─────────────────────────────────────────────────────────────
# 交互菜单
# ─────────────────────────────────────────────────────────────


def menu_sort(cfg: ViewConfig) -> None:
    print("选择排序方式：支持多选，优先级按照输入顺序。")
    print("例如：3,1 表示先按胜率，再按累计盈利；1,3 表示先按累计盈利，再按胜率。")
    print()
    for i, key in enumerate(SORT_OPTIONS, start=1):
        mark = "*" if key in cfg.sort_keys else " "
        priority = cfg.sort_keys.index(key) + 1 if key in cfg.sort_keys else ""
        print(f"  {i}. [{mark}] {key:<8} {SORT_LABELS[key]} {f'优先级={priority}' if priority else ''}")
    print()
    raw = input(f"输入序号或名称，多个用逗号分隔 [当前 {sort_keys_text(cfg.sort_keys)}]：").strip()
    if not raw:
        return
    keys = parse_sort_keys(raw, default=cfg.sort_keys)
    cfg.sort_keys = keys
    print(f"已设置排序优先级：{sort_keys_text(cfg.sort_keys)}")
    pause()


def menu_top(cfg: ViewConfig) -> None:
    raw = input(f"Top 数量 [当前 {cfg.top}]：").strip()
    if not raw:
        return
    try:
        cfg.top = max(1, int(raw))
    except Exception:
        print("输入无效。")
        pause()


def menu_filters(cfg: ViewConfig) -> None:
    while True:
        clear_screen()
        print("过滤条件")
        print("-" * 80)
        print(f"1. 最少交易数       : {cfg.min_trades}")
        print(f"2. 只看当前持仓     : {cfg.only_open}")
        print(f"3. 只看盈利组合     : {cfg.profitable_only}")
        print(f"4. ask 范围过滤     : {cfg.ask_filter or '无'}  例：0.86-0.93")
        print(f"5. 时间窗口过滤     : {cfg.snipe_filter or '无'}  例：10-50")
        print(f"6. variant 过滤     : {cfg.variant_filter or '无'}  例：V00001")
        print("7. 清空过滤")
        print("0. 返回")
        choice = input("选择：").strip()
        if choice == "0":
            return
        if choice == "1":
            raw = input("最少交易数：").strip()
            if raw:
                try:
                    cfg.min_trades = max(0, int(raw))
                except Exception:
                    pass
        elif choice == "2":
            cfg.only_open = not cfg.only_open
        elif choice == "3":
            cfg.profitable_only = not cfg.profitable_only
        elif choice == "4":
            cfg.ask_filter = input("ask 范围，例如 0.86-0.93；留空取消：").strip()
        elif choice == "5":
            cfg.snipe_filter = input("时间窗口，例如 10-50；留空取消：").strip()
        elif choice == "6":
            cfg.variant_filter = input("variant，例如 V00001；留空取消：").strip()
        elif choice == "7":
            cfg.min_trades = 0
            cfg.only_open = False
            cfg.profitable_only = False
            cfg.ask_filter = ""
            cfg.snipe_filter = ""
            cfg.variant_filter = ""


def menu_watch(cfg: ViewConfig) -> None:
    raw = input("自动刷新间隔秒数 [默认 5]：").strip()
    try:
        interval = float(raw) if raw else 5.0
    except Exception:
        interval = 5.0
    print("自动刷新中，按 Ctrl+C 返回菜单。")
    time.sleep(0.6)
    try:
        while True:
            _all, rows = get_current_rows(cfg)
            print_table(rows, cfg, clear=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n已停止自动刷新。")
        pause()


def interactive_loop(cfg: ViewConfig) -> None:
    while True:
        try:
            all_rows, rows = get_current_rows(cfg)
        except Exception as e:
            clear_screen()
            print(f"❌ {e}")
            print("确认静默评测程序已经运行过，并生成 grid_shadow_data/grid_summary.csv。")
            pause()
            continue

        clear_screen()
        print_table(rows, cfg)
        print()
        print("菜单")
        print("-" * 80)
        print("1. 刷新排行榜")
        print("2. 设置排序方式 / 多字段优先级")
        print("3. 修改 Top 数量")
        print("4. 设置过滤条件")
        print("5. 查看整体统计")
        print("6. 查看某个参数组合详情")
        print("7. 查看最近交易明细")
        print("8. 自动刷新排行榜")
        print("9. 导出当前过滤后的 Top 到 CSV")
        print("0. 退出")
        choice = input("选择：").strip().lower()

        if choice in {"0", "q", "quit", "exit"}:
            return
        if choice in {"", "1", "r", "refresh"}:
            continue
        if choice == "2":
            clear_screen()
            menu_sort(cfg)
        elif choice == "3":
            menu_top(cfg)
        elif choice == "4":
            menu_filters(cfg)
        elif choice == "5":
            clear_screen()
            print_stats(all_rows, cfg)
            pause()
        elif choice == "6":
            v = input("输入 variant_id，例如 V00001：").strip()
            if v:
                clear_screen()
                print_variant_detail(cfg, v)
                pause()
        elif choice == "7":
            raw = input("显示最近多少条 [默认 30]：").strip()
            try:
                n = int(raw) if raw else 30
            except Exception:
                n = 30
            clear_screen()
            print_recent_trades(cfg, n)
            pause()
        elif choice == "8":
            menu_watch(cfg)
        elif choice == "9":
            try:
                out = export_top(cfg, rows)
                print(f"已导出：{out.resolve()}")
            except Exception as e:
                print(f"导出失败：{e}")
            pause()


# ─────────────────────────────────────────────────────────────
# CLI 参数
# ─────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="查看 Polymarket 多参数影子盘排行榜")
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="数据目录，默认 grid_shadow_data")
    parser.add_argument("--summary", default="", help="直接指定 grid_summary.csv 路径，优先级高于 --data-dir")
    parser.add_argument("--trades", default="", help="直接指定 grid_trades.csv 路径，优先级高于 --data-dir")
    parser.add_argument("--sort", default="score", help="排序方式，支持多字段，如 winrate,pnl 或 3,1")
    parser.add_argument("--top", type=int, default=20, help="显示前 N 名")
    parser.add_argument("--min-trades", type=int, default=0, help="至少完成多少笔交易才显示")
    parser.add_argument("--only-open", action="store_true", help="只显示当前有持仓的组合")
    parser.add_argument("--profitable-only", action="store_true", help="只显示 total_pnl > 0 的组合")
    parser.add_argument("--ask", default="", help="过滤 ask 范围，例如 0.86-0.93")
    parser.add_argument("--snipe", default="", help="过滤时间窗口，例如 10-50")
    parser.add_argument("--variant", default="", help="过滤 variant_id，例如 V00001")
    parser.add_argument("--once", action="store_true", help="只打印一次，不进入交互菜单")
    parser.add_argument("--watch", type=float, default=0.0, help="非交互模式每隔 N 秒自动刷新；需要配合 --once 使用")
    parser.add_argument("--clear", action="store_true", help="非交互刷新时清屏")
    return parser.parse_args()


def make_config(args: argparse.Namespace) -> ViewConfig:
    data_dir, summary_path, trades_path = build_paths(args.data_dir, args.summary, args.trades)
    return ViewConfig(
        data_dir=data_dir,
        summary_path=summary_path,
        trades_path=trades_path,
        sort_keys=parse_sort_keys(args.sort, default=["score"]),
        top=max(1, int(args.top)),
        min_trades=max(0, int(args.min_trades)),
        only_open=bool(args.only_open),
        profitable_only=bool(args.profitable_only),
        ask_filter=str(args.ask or "").strip(),
        snipe_filter=str(args.snipe or "").strip(),
        variant_filter=str(args.variant or "").strip(),
    )


def run_once(cfg: ViewConfig, watch: float = 0.0, clear: bool = False) -> None:
    while True:
        try:
            _all, rows = get_current_rows(cfg)
            print_table(rows, cfg, clear=clear)
        except Exception as e:
            print(f"❌ {e}", file=sys.stderr)
            print("确认静默评测程序已经运行过，并生成了 grid_shadow_data/grid_summary.csv。", file=sys.stderr)

        if watch <= 0:
            return
        time.sleep(float(watch))


def main() -> None:
    args = parse_args()
    cfg = make_config(args)

    if args.once:
        run_once(cfg, watch=float(args.watch), clear=bool(args.clear))
        return

    interactive_loop(cfg)


if __name__ == "__main__":
    main()
