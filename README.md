# Polymarket BTC Up/Down 5m Sniper · CLOB V2 实盘 / 影子盘 / 多参数评测系统

> 一个面向 Polymarket `Bitcoin Up or Down 5m` 市场的自动化交易与参数评测项目。  
> 支持 CLOB V2、实时盘口 WebSocket、REST 盘口兜底、影子盘模拟、实盘下单、0.99/0.98 Bid 止盈、到期结算、多参数组合并行评测、排行榜筛选与导出。

---

## 联系方式

如需交流、定制开发、部署协助或策略二次开发：

- TG：`@whoisme123s`

---

## 重要声明

本项目仅用于技术研究、策略验证、自动化交易系统学习与风险评估，不构成任何投资建议。  
Polymarket 相关交易存在本金亏损、滑点、成交失败、API 变更、盘口延迟、网络中断、规则变化、地区合规限制等风险。  
请先使用影子盘和小资金充分测试，确认逻辑、资金权限、API 权限和风控都正确后，再考虑实盘。

---

## 项目简介

本项目主要围绕 Polymarket BTC 5 分钟涨跌市场构建，核心思想是：

1. 自动发现当前或即将开始的 `btc-updown-5m-*` 市场；
2. 订阅 UP / DOWN 两个 outcome token 的实时盘口；
3. 在市场最后若干秒内，寻找 ask 价格落在设定高概率区间的一侧；
4. 结合点差、买盘深度、双方深度比、期望净利润等条件判断是否入场；
5. 入场后持续观察持仓 bid；
6. 若 bid 达到止盈阈值，立即尝试全部卖出；
7. 若未提前止盈，则等待市场到期，通过 `market_resolved` 或 bid 接近 1/0 进行兜底结算；
8. 通过多参数影子盘评测器同时测试大量参数组合，筛选更稳的参数；
9. 通过排行榜 CLI 查看 PNL、胜率、ROI、最大回撤、综合评分等结果。

---


## 核心功能

### 1. CLOB V2 适配

`polymarket_sniper_live.py` 已适配 Polymarket CLOB V2：

- 使用 `py_clob_client_v2`
- 使用 `create_and_post_market_order()`
- BUY / SELL 都通过 V2 market order 方式提交
- 支持 `FAK` / `FOK` 类型
- 自动处理 tick size
- 自动读取 negative risk 配置
- 对 `order_version_mismatch` 做一次重建 client 后重试
- 不再使用旧版 `py_clob_client`

---

### 2. 自动发现 BTC 5m 市场

程序会根据当前 UTC 时间自动拼接并搜索市场 slug：

```text
btc-updown-5m-{timestamp}
```

默认搜索当前窗口、前后窗口和更远一个窗口：

```python
MARKET_SEARCH_OFFSETS = (0, 300, -300, 600, -600)
```

也就是说，程序会尝试发现：

- 当前 5 分钟市场
- 下一个 5 分钟市场
- 上一个 5 分钟市场
- 再往后一个 5 分钟市场
- 再往前一个 5 分钟市场

---

### 3. WebSocket 实时盘口

程序订阅 Polymarket CLOB WebSocket：

```python
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
```

支持处理：

- `book`：盘口快照
- `price_change`：盘口增量变化
- `best_bid_ask`：最优买一卖一兜底更新
- `last_trade_price`：最新成交价
- `market_resolved`：市场结算事件

---

### 4. REST 盘口兜底

如果 WebSocket 盘口未初始化、盘口过期、bid/ask 不完整，程序会尝试从 CLOB REST 接口拉取盘口：

```python
REST_BOOK_FALLBACK_ENABLED = True
REST_BOOK_FALLBACK_INTERVAL_SEC = 1.0
REST_BOOK_TIMEOUT = 3.0
```

这样可以降低 WS 刚连接、断线重连或切换市场时没有盘口导致错过交易的概率。

---

### 5. 影子盘模式

影子盘不会真实下单，只会基于实时盘口模拟买入、止盈、结算和盈亏。

用途：

- 验证策略逻辑
- 观察入场时机
- 评估参数表现
- 检查盘口数据是否稳定
- 避免直接实盘亏损

主策略中通过：

```python
PAPER_MODE = True
```

启用影子盘。

多参数评测器会强制：

```python
base.CFG.PAPER_MODE = True
base.CFG.LIVE_TRADING_ENABLED = False
```

所以多参数评测器永远不会真实下单。

---

### 6. 实盘交易模式

实盘模式会调用 Polymarket CLOB V2 SDK 下单。  
启动实盘需要同时满足：

1. `PAPER_MODE = False`
2. `LIVE_TRADING_ENABLED = True`
3. 正确配置 `.env`
4. 当前目录存在 `live_trading_confirm.txt`
5. `live_trading_confirm.txt` 内容必须精确为：

```text
ENABLE_LIVE_TRADING
```

这是为了避免误操作导致真实资金下单。

---

### 7. 入场策略

主策略默认逻辑：

- 只在市场最后 `SNIPE_WINDOW_MIN ~ SNIPE_WINDOW_MAX` 秒内考虑入场
- 默认是最后 `10 ~ 50` 秒
- 只买 ask 落在指定高概率区间的一侧
- 默认 ask 区间为 `0.86 ~ 0.93`
- 要求点差不能过大
- 要求买盘深度不能太低
- 如果 UP / DOWN 两边都符合 ask 区间，则选择买盘深度更强的一边
- 计算 crowd ratio、估算胜率、估算期望净利润
- 期望净利润低于阈值则不买

---

### 8. 0.98 / 0.99 Bid 止盈

程序配置名称是：

```python
ENABLE_TAKE_PROFIT_AT_099 = True
TAKE_PROFIT_BID_PRICE = 0.98
```

虽然变量名保留了 `099`，但当前默认止盈 bid 是 `0.98`。

当持仓中的 token 当前 best bid 达到或超过 `TAKE_PROFIT_BID_PRICE` 时：

- 影子盘：按当前 bid 模拟卖出
- 实盘：调用 CLOB V2 SELL market order，使用 worst price 控制滑点

---

### 9. 中途止损

默认关闭：

```python
ENABLE_MID_EXIT_STOP_LOSS = False
STOP_LOSS_U = 3.0
```

开启后，当浮动亏损小于等于 `-STOP_LOSS_U` 时，程序会尝试按当前 bid 平仓。

注意：在 5 分钟二元市场里，中途止损可能会因为盘口极端变化、深度不足、滑点、无法成交等原因表现不稳定，开启前必须影子盘充分验证。

---

### 10. 到期结算

到期后程序优先使用 WebSocket 的 `market_resolved` 事件识别赢家。

如果没有收到 `market_resolved`，则使用 bid 兜底：

- bid >= `0.95`：认为接近归 1，按胜利结算
- bid <= `0.05` 或 bid 不存在：认为接近归 0，按失败结算
- 超过 `SETTLEMENT_TIMEOUT_SEC` 仍无法判断：标记为 `EXPIRED`，本次不计盈亏

---

### 11. 多参数组合并行影子评测

`polymarket_param_grid_shadow_v2.py` 用同一份实时盘口，让多个参数组合同时跑影子交易。

特点：

- 不需要私钥
- 不会实盘下单
- 每个参数组合有独立权益
- 每个参数组合有独立持仓
- 每个参数组合独立统计 PNL、胜率、ROI、最大回撤
- 输出 `grid_summary.csv`
- 输出 `grid_trades.csv`
- 支持中断后从 `grid_state.json` 恢复

适合用于筛选：

- 哪个入场时间窗口更好
- 哪个 ask 区间更好
- 哪个点差限制更好
- 哪个深度要求更好
- 哪个 crowd ratio 更好
- 哪个止盈 bid 更好

---

### 12. 排行榜 CLI

`grid_rank_viewer.py` 用来读取多参数评测结果：

- 查看排行榜
- 多字段排序
- 按交易次数过滤
- 按是否盈利过滤
- 按 ask 区间过滤
- 按时间窗口过滤
- 查看某个 `variant_id` 参数详情
- 查看最近交易明细
- 自动刷新排行榜
- 导出当前过滤后的 Top 到 CSV

---

## 安装环境

### 1. Python 版本

建议：

```text
Python >= 3.10
```

### 2. 安装依赖

```bash
pip uninstall py-clob-client -y
pip install -U py-clob-client-v2 python-dotenv requests websockets
```

如果你只跑影子盘和多参数评测，也建议安装完整依赖，避免导入时报错。

---

## 环境变量配置

在项目根目录创建 `.env`：

```env
# Polymarket 钱包私钥
POLY_PRIVATE_KEY=你的私钥

# Polymarket proxy/funder 钱包地址
POLY_FUNDER=你的_proxy_or_funder_钱包地址

# 签名类型，通常为 1
POLY_SIGNATURE_TYPE=1

# CLOB Host
POLY_CLOB_HOST=https://clob.polymarket.com

# 如果你已有 CLOB API Key，可以填以下三项
CLOB_API_KEY=
CLOB_SECRET=
CLOB_PASS_PHRASE=
```

---

## 运行方式

### 1. 运行主策略影子盘

先确认 `polymarket_sniper_live.py` 中：

```python
PAPER_MODE = True
LIVE_TRADING_ENABLED = False
```

然后运行：

```bash
python polymarket_sniper_live.py
```

影子盘不会真实下单，适合先观察策略表现。

---

### 2. 运行主策略实盘

确认你真的要实盘后，修改：

```python
PAPER_MODE = False
LIVE_TRADING_ENABLED = True
```

创建实盘确认文件：

Linux / macOS：

```bash
echo ENABLE_LIVE_TRADING > live_trading_confirm.txt
```

Windows CMD：

```cmd
echo ENABLE_LIVE_TRADING> live_trading_confirm.txt
```

然后运行：

```bash
python polymarket_sniper_live.py
```

---

### 3. 运行多参数影子盘评测

确保 `polymarket_param_grid_shadow_v2.py` 和 `polymarket_sniper_live.py` 在同一目录：

```bash
python polymarket_param_grid_shadow_v2.py
```

输出目录：

```text
grid_shadow_data/
├── grid_trades.csv
├── grid_summary.csv
├── grid_state.json
└── errors.log
```

---

### 4. 查看多参数排行榜

交互模式：

```bash
python grid_rank_viewer.py
```

只看一次：

```bash
python grid_rank_viewer.py --once
```

按胜率优先，再按盈利排序：

```bash
python grid_rank_viewer.py --once --sort winrate,pnl --min-trades 20 --top 30
```

按累计盈利优先，再按胜率排序：

```bash
python grid_rank_viewer.py --once --sort pnl,winrate --min-trades 20 --top 30
```

每 5 秒刷新一次：

```bash
python grid_rank_viewer.py --once --watch 5 --clear
```

筛选 ask 区间：

```bash
python grid_rank_viewer.py --once --ask 0.86-0.93
```

筛选时间窗口：

```bash
python grid_rank_viewer.py --once --snipe 10-50
```

只看盈利组合：

```bash
python grid_rank_viewer.py --once --profitable-only
```

只看某个参数组合：

```bash
python grid_rank_viewer.py --once --variant V00001
```

---

## 主策略配置参数说明

以下参数位于 `polymarket_sniper_live.py` 的 `Config` 类中。

### 基础参数

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `PROGRAM_NAME` | `Polymarket BTC 5m Favorite Side Sniper CLOB V2 修复版` | 程序名称，只影响启动横幅显示 |
| `PAPER_MODE` | `False` | 是否影子盘。`True` 不真实下单，`False` 可实盘 |
| `LIVE_TRADING_ENABLED` | `True` | 是否允许实盘交易。实盘必须同时满足此项和确认文件 |
| `LIVE_CONFIRM_FILE` | `live_trading_confirm.txt` | 实盘确认文件名 |
| `LIVE_CONFIRM_TEXT` | `ENABLE_LIVE_TRADING` | 实盘确认文件必须包含的精确内容 |

---

### 实盘下单参数

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `LIVE_CHAIN_ID` | `137` | Polygon 链 ID |
| `LIVE_SIGNATURE_TYPE` | `1` | Polymarket 签名类型 |
| `LIVE_ORDER_TYPE` | `FAK` | 订单类型，默认 Fill-And-Kill |
| `LIVE_BUY_SLIPPAGE` | `0.03` | 买入 worst price = 当前 ask + 该滑点，上限 0.99 |
| `LIVE_SELL_SLIPPAGE` | `0.01` | 卖出 worst price = 当前 bid - 该滑点，下限 0.01 |
| `LIVE_DEFAULT_TICK_SIZE` | `0.01` | 获取 tick size 失败时使用的默认 tick |

说明：

- BUY market order 的 `amount` 是要花费的美元金额。
- SELL market order 的 `amount` 是要卖出的 shares 数量。
- 实盘卖出前会尝试查询 conditional token 余额，避免卖出数量超过实际持仓。

---

### 市场发现参数

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `GAMMA_BASE_URL` | `https://gamma-api.polymarket.com` | Polymarket Gamma API 地址 |
| `CLOB_BASE_URL` | `https://clob.polymarket.com` | Polymarket CLOB API 地址 |
| `WINDOW_SECONDS` | `300` | 市场周期，BTC Up/Down 5m 为 300 秒 |
| `MARKET_SLUG_PREFIX` | `btc-updown-5m` | 市场 slug 前缀 |
| `MARKET_SEARCH_OFFSETS` | `(0, 300, -300, 600, -600)` | 市场搜索偏移秒数 |
| `MARKET_REFRESH_SEC` | `8` | 市场刷新间隔 |
| `HTTP_TIMEOUT` | `10` | HTTP 请求超时时间 |
| `HTTP_RETRIES` | `3` | HTTP 请求失败重试次数 |
| `HTTP_RETRY_SLEEP` | `0.5` | HTTP 重试间隔基础值 |

---

### WebSocket 参数

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `WS_URL` | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | CLOB 市场 WebSocket 地址 |
| `WS_PING_INTERVAL` | `20.0` | WebSocket ping 间隔 |
| `WS_PING_TIMEOUT` | `10.0` | ping 超时时间 |
| `WS_RECONNECT_BASE` | `1.0` | 断线后初始重连等待秒数 |
| `WS_RECONNECT_MAX` | `30.0` | 最大重连等待秒数 |
| `WS_RECONNECT_FACTOR` | `2.0` | 重连等待倍增系数 |
| `DEBUG_WS_EVENT_TYPES` | `True` | 是否打印首次出现的 WS 事件类型 |

---

### 策略入场参数

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `SNIPE_WINDOW_MAX` | `50` | 距离到期大于该秒数时不入场 |
| `SNIPE_WINDOW_MIN` | `10` | 距离到期小于该秒数时不入场 |
| `MIN_ENTRY_ASK` | `0.86` | 入场 ask 下限 |
| `MAX_ENTRY_ASK` | `0.93` | 入场 ask 上限 |
| `MAX_SPREAD` | `0.08` | 最大允许点差 |
| `MIN_BID_DEPTH_SHARES` | `10.0` | 最小买盘深度，统计前三档 bid size 总和 |
| `MIN_CROWD_RATIO` | `1.30` | 两边都符合时，强势方向深度至少要达到对侧的倍数 |
| `MIN_NET_PROFIT_U` | `0.02` | 最低期望净利润，小于该值不入场 |

---

### 手续费与胜率估算参数

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `FEE_THETA` | `0.05` | 手续费估算中的 theta 参数 |
| `TAKER_REBATE_RATE` | `0.50` | taker 返佣或折扣估算比例 |
| `MIN_NET_PROFIT_U` | `0.02` | 期望净利润阈值 |

手续费估算逻辑：

```python
gross_fee = FEE_THETA * shares * price * (1 - price)
net_fee = gross_fee * (1 - TAKER_REBATE_RATE)
```

期望净利润逻辑：

```python
profit_win = shares * (1 - ask) - fee_buy
profit_lose = -shares * ask - fee_buy
expected_net = win_prob * profit_win + (1 - win_prob) * profit_lose
```

胜率估算会综合：

- 当前 ask
- crowd ratio
- 剩余时间 remain

---

### 账户与预算参数

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `INIT_EQUITY` | `19.0` | 影子盘初始权益 |
| `ORDER_BUDGET_U` | `18.0` | 每笔计划使用预算 |
| `MIN_TRADE_BUDGET_U` | `2.0` | 最低交易预算，低于则不买 |

实际下单预算逻辑：

```python
budget = min(ORDER_BUDGET_U, 当前权益)
```

也就是说，如果账户权益低于单笔预算，会自动缩小到当前权益。

---

### 风控参数

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `DAILY_MAX_LOSS_U` | `15.0` | 当日最大亏损，触及后停止入场 |
| `MAX_CONSECUTIVE_LOSSES` | `5` | 最大连续亏损次数，触及后停止入场 |
| `MAX_TRADES_PER_DAY` | `288` | 每日最大交易次数 |
| `ONLY_ONE_TRADE_PER_WINDOW` | `True` | 每个 5 分钟市场最多交易一次 |
| `COOLDOWN_AFTER_TRADE_SEC` | `5` | 平仓或结算后冷却秒数 |

---

### 止盈与止损参数

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `ENABLE_TAKE_PROFIT_AT_099` | `True` | 是否开启 bid 止盈 |
| `TAKE_PROFIT_BID_PRICE` | `0.98` | 当前持仓 bid 达到该值时全部卖出 |
| `ENABLE_MID_EXIT_STOP_LOSS` | `False` | 是否开启中途止损 |
| `STOP_LOSS_U` | `3.0` | 浮亏达到该金额时触发止损 |

---

### 结算参数

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `SETTLEMENT_TIMEOUT_SEC` | `45` | 到期后最多等待结算事件的秒数 |

结算优先级：

1. `market_resolved` 事件；
2. 持仓 token bid >= 0.95，判定赢；
3. 持仓 token bid <= 0.05 或 bid 不存在，判定输；
4. 超时仍无法判断，标记 `EXPIRED`。

---

### 文件输出参数

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `DATA_DIR` | `sniper_data` | 主策略数据目录 |
| `TRADES_FILE` | `trades.csv` | 交易记录文件 |
| `STATE_FILE` | `state.json` | 状态恢复文件 |
| `ERROR_FILE` | `errors.log` | 错误日志文件 |

主策略输出目录：

```text
sniper_data/
├── trades.csv
├── state.json
└── errors.log
```

---

### 主循环参数

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `EVAL_INTERVAL_SEC` | `0.3` | 策略循环间隔 |
| `BOOK_STALE_SEC` | `5.0` | 盘口超过该秒数未更新则视为过期 |
| `REST_BOOK_FALLBACK_ENABLED` | `True` | 是否启用 REST 盘口兜底 |
| `REST_BOOK_FALLBACK_INTERVAL_SEC` | `1.0` | REST 盘口兜底最短间隔 |
| `REST_BOOK_TIMEOUT` | `3.0` | REST 盘口请求超时时间 |

---

## 多参数评测器配置说明

以下参数位于 `polymarket_param_grid_shadow_v2.py`。

### GridRunnerConfig 参数

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `PROGRAM_NAME` | `Polymarket BTC 5m 多参数组合并行影子盘评测器` | 程序名称 |
| `DATA_DIR` | `grid_shadow_data` | 多参数评测输出目录 |
| `EVAL_INTERVAL_SEC` | `0.30` | 评测循环间隔 |
| `MARKET_REFRESH_SEC` | `8` | 市场刷新间隔 |
| `BOOK_STALE_SEC` | `5.0` | 盘口过期秒数 |
| `REST_BOOK_FALLBACK_ENABLED` | `True` | 是否启用 REST 盘口兜底 |
| `REST_BOOK_FALLBACK_INTERVAL_SEC` | `1.0` | REST 盘口兜底间隔 |
| `REST_BOOK_TIMEOUT` | `3.0` | REST 请求超时 |
| `SETTLEMENT_TIMEOUT_SEC` | `45` | 结算等待超时 |
| `INIT_EQUITY` | `19.0` | 每个参数组合初始权益 |
| `MIN_TRADE_BUDGET_U` | `2.0` | 最低交易预算 |
| `ORDER_BUDGET_U` | `18.0` | 默认交易预算 |
| `DAILY_MAX_LOSS_U` | `15.0` | 默认日亏损上限 |
| `MAX_CONSECUTIVE_LOSSES` | `5` | 默认最大连续亏损次数 |
| `MAX_TRADES_PER_DAY` | `288` | 默认每日最大交易次数 |
| `ONLY_ONE_TRADE_PER_WINDOW` | `True` | 默认每局只交易一次 |
| `PRINT_TOP_INTERVAL_SEC` | `15.0` | 控制台打印排行榜间隔 |
| `SAVE_INTERVAL_SEC` | `10.0` | 保存状态和 summary 的间隔 |
| `TOP_N` | `15` | 控制台默认展示前 N 名 |
| `MIN_TRADES_FOR_WINRATE_RANK` | `5` | 综合评分中交易次数权重的最低参考数 |
| `RESUME_STATE` | `True` | 是否从 `grid_state.json` 恢复 |

---

### 参数网格

评测器会对下列列表做笛卡尔积组合。  
组合数量 = 所有列表长度相乘。

当前默认网格：

```python
SNIPE_WINDOWS = [
    (10, 50),
    (15, 50),
    (20, 50),
    (30, 60),
]

MONITOR_ASK_RANGES = [
    (0.82, 0.93),
    (0.84, 0.93),
    (0.85, 0.93),
    (0.86, 0.93),
    (0.88, 0.93),
    (0.86, 0.92),
    (0.88, 0.92),
    (0.90, 0.94),
]

MAX_SPREADS = [0.03, 0.05, 0.08]
MIN_BID_DEPTHS = [5.0, 10.0, 20.0]
MIN_CROWD_RATIOS = [1.10, 1.30, 1.50, 2.00]
MIN_NET_PROFITS_U = [0.00, 0.01, 0.02]
TAKE_PROFIT_BIDS = [0.97, 0.98, 0.99]

ORDER_BUDGETS_U = [18.0]
MIN_TRADE_BUDGETS_U = [2.0]

FEE_THETAS = [0.05]
TAKER_REBATE_RATES = [0.50]

ENABLE_MID_EXIT_STOP_LOSSES = [False]
STOP_LOSS_US = [3.0]

ONLY_ONE_TRADE_PER_WINDOWS = [True]

WIN_SETTLE_BIDS = [0.95]
LOSE_SETTLE_BIDS = [0.05]

DAILY_MAX_LOSS_US = [15.0]
MAX_CONSECUTIVE_LOSSES_LIST = [5]
MAX_TRADES_PER_DAYS = [288]
```

默认组合数量：

```text
4 * 8 * 3 * 3 * 4 * 3 * 3 = 10368 个参数组合
```

---

### 参数网格字段解释

| 参数 | 说明 |
|---|---|
| `SNIPE_WINDOWS` | 入场时间窗口，例如 `(10, 50)` 表示剩余 10~50 秒才允许入场 |
| `MONITOR_ASK_RANGES` | 监控/入场 ask 区间，例如 `(0.86, 0.93)` |
| `MAX_SPREADS` | 最大点差过滤 |
| `MIN_BID_DEPTHS` | 最小 bid 深度过滤 |
| `MIN_CROWD_RATIOS` | 双方都符合时，强势方向深度至少是弱势方向的倍数 |
| `MIN_NET_PROFITS_U` | 最低期望净利润 |
| `TAKE_PROFIT_BIDS` | bid 止盈阈值 |
| `ORDER_BUDGETS_U` | 每笔预算 |
| `MIN_TRADE_BUDGETS_U` | 最低交易预算 |
| `FEE_THETAS` | 手续费估算 theta |
| `TAKER_REBATE_RATES` | taker 返佣/折扣估算 |
| `ENABLE_MID_EXIT_STOP_LOSSES` | 是否测试中途止损 |
| `STOP_LOSS_US` | 中途止损金额 |
| `ONLY_ONE_TRADE_PER_WINDOWS` | 每个市场是否最多交易一次 |
| `WIN_SETTLE_BIDS` | 无结算事件时，bid 大于等于该值判定赢 |
| `LOSE_SETTLE_BIDS` | 无结算事件时，bid 小于等于该值判定输 |
| `DAILY_MAX_LOSS_US` | 每个组合的日亏损上限 |
| `MAX_CONSECUTIVE_LOSSES_LIST` | 每个组合的最大连续亏损 |
| `MAX_TRADES_PER_DAYS` | 每个组合每日最大交易次数 |

---

## 多参数评测输出文件说明

### `grid_shadow_data/grid_summary.csv`

每个参数组合一行，实时汇总。

主要字段：

| 字段 | 说明 |
|---|---|
| `rank_pnl` | 按累计盈利排名 |
| `rank_score` | 按综合评分排名 |
| `variant_id` | 参数组合 ID，例如 `V00001` |
| `equity` | 当前权益 |
| `total_pnl` | 累计盈利 |
| `daily_pnl` | 当日盈利 |
| `roi_pct` | ROI 百分比 |
| `trades` | 总交易次数 |
| `trades_today` | 当日交易次数 |
| `wins` | 盈利次数 |
| `losses` | 亏损次数 |
| `expired` | 超时未结算次数 |
| `take_profits` | 止盈次数 |
| `win_rate_pct` | 胜率 |
| `avg_pnl` | 单笔平均 PNL |
| `max_drawdown_pct` | 最大回撤百分比 |
| `score` | 综合评分 |
| `snipe_min` | 入场窗口最小剩余秒数 |
| `snipe_max` | 入场窗口最大剩余秒数 |
| `min_ask` | ask 入场下限 |
| `max_ask` | ask 入场上限 |
| `max_spread` | 最大点差 |
| `min_bid_depth` | 最小 bid 深度 |
| `min_crowd_ratio` | 最小深度强弱比 |
| `min_net_profit_u` | 最低期望净利润 |
| `take_profit_bid` | bid 止盈价 |
| `order_budget_u` | 每笔预算 |
| `min_trade_budget_u` | 最低预算 |
| `fee_theta` | 手续费 theta |
| `taker_rebate_rate` | taker 返佣估算 |
| `enable_mid_stop_loss` | 是否开启中途止损 |
| `stop_loss_u` | 止损金额 |
| `only_one_trade_per_window` | 是否每局只交易一次 |
| `win_settle_bid` | 胜利兜底结算 bid |
| `lose_settle_bid` | 失败兜底结算 bid |
| `daily_max_loss_u` | 日亏损上限 |
| `max_consecutive_losses` | 连续亏损上限 |
| `max_trades_per_day` | 每日交易次数上限 |
| `open_position` | 当前是否有持仓 |

---

### `grid_shadow_data/grid_trades.csv`

记录每个参数组合的详细交易事件。

主要事件类型：

| 事件 | 说明 |
|---|---|
| `BUY` | 影子买入 |
| `TAKE_PROFIT` / `TAKE_PROFIT_BID_0.99` | 触发 bid 止盈 |
| `WIN` | 到期结算胜利 |
| `LOSE` | 到期结算失败 |
| `STOP_LOSS` | 中途止损 |
| `EXPIRED` | 结算等待超时 |

主要字段：

| 字段 | 说明 |
|---|---|
| `time` | 事件时间 |
| `variant_id` | 参数组合 ID |
| `event` | 事件类型 |
| `market_slug` | 市场 slug |
| `side` | UP 或 DOWN |
| `remain` | 入场时剩余秒数 |
| `entry_ask` | 入场 ask |
| `exit_price` | 出场价格 |
| `shares` | 份额数量 |
| `cost` | 成本 |
| `fee` | 估算手续费 |
| `expected_net` | 入场时估算期望净利润 |
| `pnl` | 本次事件实现盈亏 |
| `equity` | 当前权益 |
| `daily_pnl` | 当日 PNL |
| `total_pnl` | 累计 PNL |
| `wins` | 当前累计赢次数 |
| `losses` | 当前累计亏次数 |
| `win_rate` | 当前胜率 |
| `reason` | 触发原因 |

---

### `grid_shadow_data/grid_state.json`

用于中断恢复。  
如果 `RESUME_STATE = True`，重新启动评测器时会恢复：

- 各组合权益
- 当前持仓
- 累计 PNL
- 胜负统计
- 回撤统计
- 已交易市场记录

如果想从零开始评测，删除：

```text
grid_shadow_data/grid_state.json
grid_shadow_data/grid_summary.csv
grid_shadow_data/grid_trades.csv
```

或者将：

```python
RESUME_STATE = False
```

---

## 排行榜 CLI 参数说明

`grid_rank_viewer.py` 支持以下命令行参数：

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `--data-dir` | `grid_shadow_data` | 数据目录 |
| `--summary` | 空 | 直接指定 `grid_summary.csv` 路径，优先级高于 `--data-dir` |
| `--trades` | 空 | 直接指定 `grid_trades.csv` 路径，优先级高于 `--data-dir` |
| `--sort` | `score` | 排序方式，支持多字段 |
| `--top` | `20` | 显示前 N 名 |
| `--min-trades` | `0` | 至少完成多少笔交易才显示 |
| `--only-open` | `False` | 只显示当前有持仓的组合 |
| `--profitable-only` | `False` | 只显示 `total_pnl > 0` 的组合 |
| `--ask` | 空 | 过滤 ask 范围，例如 `0.86-0.93` |
| `--snipe` | 空 | 过滤时间窗口，例如 `10-50` |
| `--variant` | 空 | 过滤某个参数组合，例如 `V00001` |
| `--once` | `False` | 只打印一次，不进入交互菜单 |
| `--watch` | `0.0` | 非交互模式每隔 N 秒自动刷新，需配合 `--once` |
| `--clear` | `False` | 非交互刷新时清屏 |

---

## 排序字段说明

支持字段：

| 排序名 | 对应字段 | 方向 |
|---|---|---|
| `pnl` | `total_pnl` | 越大越好 |
| `score` | `score` | 越大越好 |
| `winrate` | `win_rate_pct` | 越大越好 |
| `avg` | `avg_pnl` | 越大越好 |
| `drawdown` | `max_drawdown_pct` | 越小越好 |
| `trades` | `trades` | 越大越好 |
| `roi` | `roi_pct` | 越大越好 |

支持多字段优先级排序：

```bash
python grid_rank_viewer.py --once --sort winrate,pnl
```

含义：

1. 先按胜率排序；
2. 胜率相同或接近时，再看累计盈利。

```bash
python grid_rank_viewer.py --once --sort pnl,winrate
```

含义：

1. 先按累计盈利排序；
2. 再看胜率。

---

## 如何筛选参数

### 看稳定性

建议：

```bash
python grid_rank_viewer.py --once --sort score,pnl --min-trades 20 --top 30
```

关注：

- `score`
- `total_pnl`
- `win_rate_pct`
- `max_drawdown_pct`
- `trades`

---

### 看胜率

```bash
python grid_rank_viewer.py --once --sort winrate,pnl --min-trades 20 --top 30
```

注意：只看胜率容易选出交易次数过少的组合，所以必须加 `--min-trades`。

---

### 看赚钱能力

```bash
python grid_rank_viewer.py --once --sort pnl,drawdown --min-trades 20 --top 30
```

关注：

- 累计 PNL 是否最高
- 回撤是否可接受
- 交易次数是否足够

---

### 看低回撤

```bash
python grid_rank_viewer.py --once --sort drawdown,pnl --min-trades 20 --top 30
```

适合筛选更保守的参数组合。

---

## 推荐调参顺序

建议不要一次性把所有参数都放开，否则组合数量过大、筛选噪音也会增加。

推荐顺序：

1. 固定预算：先不要比较不同预算；
2. 固定手续费：先不要加入多组手续费假设；
3. 先调入场窗口：`SNIPE_WINDOWS`
4. 再调 ask 区间：`MONITOR_ASK_RANGES`
5. 再调点差：`MAX_SPREADS`
6. 再调深度：`MIN_BID_DEPTHS`
7. 再调 crowd ratio：`MIN_CROWD_RATIOS`
8. 再调止盈：`TAKE_PROFIT_BIDS`
9. 最后再测试止损：`ENABLE_MID_EXIT_STOP_LOSSES`

---

## 常见问题

### 1. 为什么程序不下单？

常见原因：

- 不在入场时间窗口内；
- ask 不在设定范围；
- 点差超过 `MAX_SPREAD`；
- bid 深度低于 `MIN_BID_DEPTH_SHARES`；
- 两边都符合 ask，但深度差距不满足 `MIN_CROWD_RATIO`；
- 期望净利润低于 `MIN_NET_PROFIT_U`；
- 已经交易过该 market，且 `ONLY_ONE_TRADE_PER_WINDOW = True`；
- 日亏损触发风控；
- 连续亏损触发风控；
- 当日交易次数达到上限；
- 盘口未初始化或过期。

---

### 2. `no orders found to match with FAK order` 是什么意思？

意思是当前提交的 FAK 订单没有找到可立即成交的对手单。

可能原因：

- 盘口瞬间变化；
- ask/bid 已经被吃掉；
- worst price 设置太严格；
- 市场临近到期流动性不足；
- WebSocket 本地盘口比真实撮合盘口慢。

---

### 3. `not enough balance / allowance` 是什么意思？

意思是余额或授权不足。

可能原因：

- USDC/pUSD 余额不足；
- allowance 不足；
- 下单金额精度或金额略高于可用余额；
- 余额查询有延迟；
- 自动兑换奖金到账慢；
- 程序计算成本和实际 CLOB 校验金额存在微小差异。

---

### 4. `order_version_mismatch` 是什么意思？

通常和 CLOB API / SDK 的订单版本、客户端状态或 API 更新有关。  
本项目已加入一次重建 client 后重试的逻辑，但如果仍持续出现，建议：

- 更新 `py-clob-client-v2`
- 重启程序
- 确认 CLOB V2 API 当前是否有变更
- 检查本地依赖是否仍混有旧版 `py-clob-client`

---

### 5. 为什么变量叫 `ENABLE_TAKE_PROFIT_AT_099`，但默认是 0.98？

变量名是历史遗留命名。  
真实触发价格看：

```python
TAKE_PROFIT_BID_PRICE = 0.98
```

如果你想 0.99 才止盈，改成：

```python
TAKE_PROFIT_BID_PRICE = 0.99
```

---

### 6. 多参数评测器会不会真实下单？

不会。

评测器启动时强制：

```python
base.CFG.PAPER_MODE = True
base.CFG.LIVE_TRADING_ENABLED = False
```

并且完全不初始化 `LiveTrader`。

---

### 7. 实盘前必须检查什么？

实盘前至少检查：

- `.env` 是否正确；
- 私钥是否为交易专用钱包；
- 钱包余额是否足够；
- allowance 是否足够；
- `PAPER_MODE = False` 是否是你主动设置的；
- `LIVE_TRADING_ENABLED = True` 是否是你主动设置的；
- `live_trading_confirm.txt` 是否由你主动创建；
- 单笔预算是否足够小；
- 是否已经影子盘跑过足够长时间；
- 是否理解 FAK 订单可能无法成交；
- 是否理解止盈卖出也可能失败；
- 是否理解到期前盘口可能剧烈变化。

---

## 后续可扩展方向

可以继续扩展：

- Telegram Bot 控制面板；
- 多用户 API Key 隔离；
- Web 后台排行榜；
- 实盘参数热更新；
- 自动余额定时刷新；
- 自动生成每日交易报告；
- 自动对比 shadow 与 live 成交差异；
- 更多市场类型支持，例如 15m BTC、ETH Up/Down；
- 钱包跟单 / 影子跟单；
- 更严格的滑点和深度成交量模拟；
- 资金曲线可视化；
- Prometheus / Grafana 监控；
- Docker 部署；
- systemd 后台运行。

---

## License
- MIT License
