#coding:gbk
"""
Alpha#144 — 流动性冲击择时策略 v5 (市值分层版)
================================================
基于微观结构因子的中盘股择时策略，专为 QMT 实盘/仿真/回测环境设计。
在 v4 基础上，将 Alpha#144 筛选出的候选池按 **总市值** 分层，每档独立配置持仓名额。

【v4→v5 优化（市值分层）】

  背景：Alpha#144 = Σ(|跌幅| / 成交额)，本质是"下跌时流动性越差 → 反弹潜力越大"。
  该微观结构因子在小盘股上信号最强，随市值增大逐渐衰减。

  v5 分层设计（单位：亿元）：
    档位1  微型   <50亿        → 3只   （因子最强区域，倾斜配置）
    档位2  小型   50~150亿     → 3只   （因子核心战场）
    档位3  中型   150~500亿    → 3只   （平衡收益/风险）
    档位4  大中型 500~1000亿   → 3只   （v5新增）
    档位5  大型   1000~3000亿  → 3只   （v5新增）
    ─────────────────────────────────────
    合计 最多 15 只；每档独立配额，某档无候选则跳过（不跨档借名额）。

  与 v4 的区别：
    1. 持仓上限 5 → 15（分散单只踩雷风险，v3 曾出现 33% 的 1 日止损）
    2. 每档按因子值排序独立选前 3 只，避免大盘股"占坑"挤压小盘股
    3. >3000亿 超大盘股不选（流动性太深，因子失效）

【核心思想】（同 v4）
  Alpha#144 = Σ(|跌幅| / 成交额) 对下跌日求和
  因子值越大 → 下跌时流动性越差 → 后续反弹潜力越大

【策略流程】
  1. 每 10 天计算 Alpha#144 因子，取 Top 15% 候选池
  2. 计算候选池各股总市值（总股本×最新收盘价），分配到 5 个档位
  3. 每日检查候选池中突破10日新高 + 放量≥1.2x + 个股趋势(M20上升+价格>M20)
     + ATR≤8% + 动量确认(近3日2涨) + 突破强度≥0.5%
  4. 每档最多买入 3 只，次日开盘买入，等权分配
  5. 首3日-12%紧止损 / 3日后-18%止损 / 大盘转弱清仓 / 持有20天到期
  6. 中证500 < MA20×(1-3%) 空仓

作者：QMT-Expert
日期：2026-08-12
"""

import numpy as np


# ╔════════════════════════════════════════════════════════════╗
# ║              用户可调参数（策略核心配置）                    ║
# ╚════════════════════════════════════════════════════════════╝

# ── 基准与标的 ──
BENCHMARK = '000905.SH'

# ── 因子计算参数 ──
FACTOR_WINDOW    = 20
FACTOR_TOP_PCT   = 0.15
REFRESH_INTERVAL = 10

# ── 入场参数 ──
BREAKOUT_PERIOD      = 10      # ★v3保留★ 突破周期10天
BREAKOUT_STRENGTH_PCT = 0.005  # ★v4新增★ 突破最低强度：收盘 > N日最高×(1+0.5%)

# ── 量能确认参数 ──
VOL_RATIO_MIN    = 1.2         # ★v3保留★ 放量≥1.2x均量
VOL_LOOKBACK     = 5

# ── 个股趋势过滤 ──
MA_STOCK         = 20          # 均线周期
MA_SLOPE_PERIOD  = 5           # ★v4新增★ MA斜率窗口：MA20今日 > MA20[5日前]

# ── 动量确认 ──
MOMENTUM_DAYS    = 3           # ★v4新增★ 动量检查窗口（交易日）
MOMENTUM_UP_MIN  = 2           # ★v4新增★ 最少收阳天数

# ── ATR日波动过滤 ──
MAX_DAILY_RANGE_PCT = 0.08     # ★v4新增★ 最大日均振幅上限（20日EMA）：跳过日振幅>8%的高波动股
ATR_PERIOD        = 20         # ATR计算周期

# ── 出场参数 ──
MAX_HOLD_BARS     = 20
HARD_STOP_PCT     = -0.18      # 标准止损线（第4天起生效）
EARLY_STOP_PCT    = -0.12      # ★v4新增★ 前3日紧止损线
EARLY_STOP_DAYS   = 3          # ★v4新增★ 紧止损生效天数

# ── 止损冷却参数 ──
STOP_COOLDOWN     = 60         # ★v3保留★

# ── 跳空保护参数 ──
GAP_DOWN_PCT      = -0.03      # ★v3保留★

# ── 大盘过滤参数 ──
MA_MARKET         = 20
MARKET_FILTER_PCT = 0.03

# ── ★v5新增★ 市值分层参数 ──
#   每项 = (档位上界_亿元, 每档持仓数, 档位标签)
#   市值 = 总股本(TotalVolume) × 最新收盘价 / 1e8
#   超过最后一个档位上界的股票(>3000亿) 不入选
MARKET_CAP_TIERS = [
    (50,    3, '微型 <50亿'),
    (150,   3, '小型 50-150亿'),
    (500,   3, '中型 150-500亿'),
    (1000,  3, '大中型 500-1000亿'),
    (3000,  3, '大型 1000-3000亿'),
]
N_TIERS         = len(MARKET_CAP_TIERS)
TIER_LABELS     = [t[2] for t in MARKET_CAP_TIERS]
MAX_POSITIONS   = sum(t[1] for t in MARKET_CAP_TIERS)   # = 15

# ── 仓位管理参数 ──
MAX_SECTOR_COUNT  = 2
MAX_SECTOR_OTHER  = 3

# ── 行业分类参数 ──
INDUSTRY_TYPE    = 'SW'
UNKNOWN_SECTOR   = '其他'

# ── ★v4新增★ 行业黑名单（基于v2+v3双重确认）──
#   v3: 商贸零售(-24.5%,9%WR), 建筑材料(-24.5%,0%WR), 农林牧渔(-19.2%,0%WR),
#       计算机(-19.9%,22%WR), 公用事业(-17.4%,10%WR)
#   v2: 计算机(-34.7%), 商贸零售(-26.6%), 公用事业(-22.0%),
#       建筑材料(-17.8%), 农林牧渔(-16.6%)
# 两者一致的5个行业 → 启用黑名单
SECTOR_BLACKLIST = {
#     'SW1商贸零售',
#     'SW1建筑材料',
#     'SW1农林牧渔',
#     'SW1计算机',
#     'SW1公用事业',
}

# ── 涨跌停约束 ──
LIMIT_UP_PCT     = 0.098
LIMIT_DOWN_PCT   = -0.098

# ── 数据质量 ──
MIN_HISTORY_BARS = 130
MIN_DAILY_AMOUNT = 3e7


# ╔════════════════════════════════════════════════════════════╗
# ║              全局状态                                       ║
# ╚════════════════════════════════════════════════════════════╝
class State:
    stock_pool    = []
    filtered_pool = []
    positions     = {}
    cash         = 0
    total_assets = 0
    acc_id       = 'testS'
    capital      = 300000
    last_barpos     = -1
    bar_counter     = 0
    rankings        = {}
    next_refresh_bar = 0
    market_ok    = True
    pending_sells = []
    sector_map   = {}
    sector_ok    = False
    stock_names  = {}
    stop_cooldown = {}
    # ★v5新增★ 市值分层
    total_shares  = {}   # code -> 总股本(股)，init 时批量获取（静态）
    market_caps   = {}   # code -> 总市值(亿元)，因子刷新时计算
    tier_map      = {}   # code -> 档位索引(0..N_TIERS-1)，-1 表示超档不选


# ╔════════════════════════════════════════════════════════════╗
# ║   动态行业分类 + 名称                                      ║
# ╚════════════════════════════════════════════════════════════╝

def _build_sector_map_from_qmt(ContextInfo, stock_list):
    sector_map = {}
    success = 0
    for i, code in enumerate(stock_list):
        try:
            name = get_industry_name_of_stock(INDUSTRY_TYPE, code)
            if name and len(name) > 0:
                sector_map[code] = name
                success += 1
        except NameError:
            print("[行业] get_industry_name_of_stock() 不可用")
            return {}, False
        except Exception:
            pass
        if (i + 1) % 100 == 0:
            print("[行业] 进度: %d/%d (已分类=%d)" % (i+1, len(stock_list), success))
    blacklisted = sum(1 for s in set(sector_map.values()) if s in SECTOR_BLACKLIST)
    print("[行业] 完成! 已分类=%d 行业数=%d 黑名单=%d个行业" %
          (success, len(set(sector_map.values())), blacklisted))
    return sector_map, True

def _build_stock_names(ContextInfo, stock_list):
    names = {}
    for code in stock_list:
        try:
            n = ContextInfo.get_stock_name(code)
            if n and len(n) > 0: names[code] = n
        except Exception: pass
    print("[名称] 获取到 %d/%d 只股票名称" % (len(names), len(stock_list)))
    return names

def _stock_label(code):
    name = State.stock_names.get(code, '')
    sector = _get_sector(code)
    short_sec = sector.replace('SW1','').replace('CSRC1','')
    tier = State.tier_map.get(code, -1)
    cap = State.market_caps.get(code, 0)
    tier_str = TIER_LABELS[tier].split(' ')[0] if 0 <= tier < N_TIERS else '超档'
    return "%s(%s|%s|%s%.0f亿)" % (code, name, short_sec, tier_str, cap)


# ╔════════════════════════════════════════════════════════════╗
# ║  ★v5新增★ 市值获取 + 档位分配                               ║
# ╚════════════════════════════════════════════════════════════╝

def _fetch_total_shares(ContextInfo, stock_list):
    """★v5新增★ 获取总股本(股)。用 QMT 原生 ContextInfo.get_instrumentdetail 逐只取。

    注意：不能用 xtdata（MiniQMT SDK），回测公式环境无此模块。
    返回 dict 的 key 为 'TotalVolumn'（QMT 文档拼写），兼容 'TotalVolume'。
    """
    shares = {}
    for i, code in enumerate(stock_list):
        try:
            inst = ContextInfo.get_instrumentdetail(code)
        except Exception:
            inst = {}
        if not inst:
            continue
        tv = inst.get('TotalVolumn', inst.get('TotalVolume', 0))
        try:
            tv = float(tv)
        except Exception:
            tv = 0
        if tv and tv > 0:
            shares[code] = tv
        if (i + 1) % 100 == 0:
            print("[市值] 进度: %d/%d (已获取=%d)" % (i+1, len(stock_list), len(shares)))
    print("[市值] 获取总股本成功 %d/%d 只" % (len(shares), len(stock_list)))
    return shares

def _compute_market_caps(hist_close):
    """★v5新增★ 总市值(亿元) = 总股本 × 最新收盘价 / 1e8。"""
    caps = {}
    for code, shares in State.total_shares.items():
        ca = hist_close.get(code, [])
        if len(ca) > 0 and float(ca[-1]) > 0:
            caps[code] = shares * float(ca[-1]) / 1e8
    return caps

def _assign_tiers(caps):
    """★v5新增★ 按市值分配到档位，返回 {code: tier_idx}。超档返回 -1。"""
    tier_map = {}
    for code, cap in caps.items():
        for i, (max_cap, _, _) in enumerate(MARKET_CAP_TIERS):
            if cap < max_cap:
                tier_map[code] = i
                break
        else:
            tier_map[code] = -1   # 超过最大档位上界(>3000亿)，不选
    return tier_map


# ╔════════════════════════════════════════════════════════════╗
# ║  ★v4新增★ ATR / 趋势斜率 / 动量 辅助                        ║
# ╚════════════════════════════════════════════════════════════╝

def _calc_atr(high_arr, low_arr, close_arr, period=ATR_PERIOD):
    """计算 ATR (Average True Range) 的近似：日均振幅=(high-low)/close。"""
    n = min(len(high_arr), len(low_arr), len(close_arr))
    if n < period:
        return None
    h = np.array(high_arr[-period:], dtype=float)
    l = np.array(low_arr[-period:], dtype=float)
    c = np.array(close_arr[-period:], dtype=float)
    ranges = (h - l) / np.maximum(c, 1e-6)
    return float(np.mean(ranges))

def _is_ma_rising(code, hist_close):
    """★v4新增★ MA20斜率：MA[today] > MA[5日前]。"""
    if code not in hist_close or len(hist_close[code]) < MA_STOCK + MA_SLOPE_PERIOD:
        return True  # 数据不足放行
    arr = np.array(hist_close[code], dtype=float)
    ma_now = np.mean(arr[-MA_STOCK:])
    ma_past = np.mean(arr[-(MA_STOCK + MA_SLOPE_PERIOD):-MA_SLOPE_PERIOD])
    return ma_now > ma_past

def _is_price_above_ma(code, hist_close):
    """价格 > MA20。"""
    if code not in hist_close or len(hist_close[code]) < MA_STOCK + 1:
        return True
    arr = np.array(hist_close[code], dtype=float)
    current = arr[-1]
    ma = np.mean(arr[-(MA_STOCK+1):-1])
    return current > ma

def _has_momentum(code, hist_close):
    """★v4新增★ 动量确认：近 MOMENTUM_DAYS 日至少 MOMENTUM_UP_MIN 日收阳。"""
    if code not in hist_close or len(hist_close[code]) < MOMENTUM_DAYS + 1:
        return True
    arr = np.array(hist_close[code], dtype=float)
    up_count = 0
    for i in range(-MOMENTUM_DAYS, 0):
        if arr[i] > arr[i-1]:
            up_count += 1
    return up_count >= MOMENTUM_UP_MIN

def _has_low_volatility(code, hist_high, hist_low, hist_close):
    """★v4新增★ ATR日波动 < 上限。"""
    high = hist_high.get(code, [])
    low = hist_low.get(code, [])
    close = hist_close.get(code, [])
    atr = _calc_atr(high, low, close)
    if atr is None:
        return True  # 数据不足放行
    return atr <= MAX_DAILY_RANGE_PCT

def _get_effective_stop(code):
    """★v4新增★ 返回当前应使用的止损线（前EARLY_STOP_DAYS天紧止损）。"""
    pos = State.positions.get(code)
    if pos and pos.get('bars_held', 0) < EARLY_STOP_DAYS:
        return EARLY_STOP_PCT
    return HARD_STOP_PCT

def _check_gap_down(code, hist_close, hist_open):
    """跳空低开检查：今日开盘/昨日收盘 - 1 <= GAP_DOWN_PCT。"""
    if code not in hist_close or code not in hist_open:
        return False
    if len(hist_close[code]) < 2 or len(hist_open[code]) < 2:
        return False
    try:
        yc = float(hist_close[code][-2])
        to = float(hist_open[code][-1])
        if yc <= 0: return False
        return (to / yc - 1.0) <= GAP_DOWN_PCT
    except Exception:
        return False


# ╔════════════════════════════════════════════════════════════╗
# ║              init()                                         ║
# ╚════════════════════════════════════════════════════════════╝

def init(ContextInfo):
    print("[init] Alpha#144 流动性冲击择时策略 v5 (市值分层版)")
    print("[init] 分层: %s" % " | ".join(
        "%s%d只" % (t[2], t[1]) for t in MARKET_CAP_TIERS))
    print("[init] v4优化: ATR<8%|突破强度0.5%+|动量3日2阳|MA20斜率|行业黑名单5|首3日紧止损-12%")

    stocks = None
    try:
        raw = ContextInfo.get_stock_list_in_sector('中证500')
        if raw and len(raw) > 0: stocks = raw
    except Exception: pass
    if not stocks:
        try:
            raw = ContextInfo.get_sector('000905.SH')
            if raw and len(raw) > 0: stocks = raw
        except Exception: pass
    if not stocks:
        print("[init] API 获取失败，使用 fallback")
        stocks = _get_fallback_csi500()

    valid = []
    for code in stocks:
        try:
            n = ContextInfo.get_stock_name(code)
            if n and len(n) > 0 and 'ST' not in n and '*' not in n: valid.append(code)
        except Exception:
            valid.append(code)

    State.stock_pool = valid
    State.filtered_pool = valid[:]
    print("[init] 有效股票池：%d 只" % len(State.stock_pool))

    # ★v5新增★ 获取总股本（静态数据，一次性获取）
    State.total_shares = _fetch_total_shares(ContextInfo, State.stock_pool)

    State.sector_map, State.sector_ok = _build_sector_map_from_qmt(
        ContextInfo, State.stock_pool)
    State.stock_names = _build_stock_names(ContextInfo, State.stock_pool)

    universe = valid[:] + [BENCHMARK]
    ContextInfo.set_universe(list(set(universe)))

    for attr, val in [('capital', State.capital), ('benchmark', BENCHMARK),
                       ('start', '2025-01-01 09:30:00'), ('end', '2026-08-06 15:00:00')]:
        try: setattr(ContextInfo, attr, val)
        except (AttributeError, TypeError): pass

    ContextInfo.set_slippage(1, 0.001)
    ContextInfo.set_commission(0, [0.00025, 0.00025, 0.001, 0.0, 0.0, 5.0])
    ContextInfo.set_account(State.acc_id)

    bl_count = sum(1 for s in set(State.sector_map.values()) if s in SECTOR_BLACKLIST)
    print("[init] 初始化完成：")
    print("       股票池=%d只 | 总股本=%d只 | 行业=%d个 | 黑名单=%d个行业(%d只股)" % (
        len(State.stock_pool), len(State.total_shares),
        len(set(State.sector_map.values())) if State.sector_ok else 0,
        bl_count,
        sum(1 for c, s in State.sector_map.items() if s in SECTOR_BLACKLIST)))
    print("       市值分档=%d档 | 每档=%s只 | 最大持仓=%d只" % (
        N_TIERS, [t[1] for t in MARKET_CAP_TIERS], MAX_POSITIONS))
    print("       突破=%d天+%.1f%%强度 | 量能=%.1fx | MA%d斜率确认+动量%d日%d阳" % (
        BREAKOUT_PERIOD, BREAKOUT_STRENGTH_PCT*100, VOL_RATIO_MIN,
        MA_STOCK, MOMENTUM_DAYS, MOMENTUM_UP_MIN))
    print("       ATR上限=%.0f%% | 首%d天紧止损%.0f%% | 冷却=%d天 | 跳空保护=%.0f%%" % (
        MAX_DAILY_RANGE_PCT*100, EARLY_STOP_DAYS, abs(EARLY_STOP_PCT)*100,
        STOP_COOLDOWN, abs(GAP_DOWN_PCT)*100))
    print("       刷新=%d天 | 持有期=%d天 | 大盘=MA%d×%.0f%%" % (
        REFRESH_INTERVAL, MAX_HOLD_BARS, MA_MARKET,
        (1-MARKET_FILTER_PCT)*100))
    print("       初始资金=%.0f万" % (State.capital/10000))


# ╔════════════════════════════════════════════════════════════╗
# ║              handlebar()                                    ║
# ╚════════════════════════════════════════════════════════════╝

def handlebar(ContextInfo):
    bar = ContextInfo.barpos
    if bar < MIN_HISTORY_BARS: return
    if bar == State.last_barpos: return
    State.last_barpos = bar
    State.bar_counter += 1

    _update_cooldown()

    need_bars = max(FACTOR_WINDOW+30, MA_MARKET+10, MA_STOCK+MA_SLOPE_PERIOD+5,
                    BREAKOUT_PERIOD+5, VOL_LOOKBACK+5, ATR_PERIOD+5, MOMENTUM_DAYS+5)
    hist_close  = ContextInfo.get_history_data(need_bars, '1d', 'close')
    hist_amount = ContextInfo.get_history_data(need_bars, '1d', 'amount')
    hist_open   = ContextInfo.get_history_data(need_bars, '1d', 'open')
    hist_high   = ContextInfo.get_history_data(need_bars, '1d', 'high')
    hist_low    = ContextInfo.get_history_data(need_bars, '1d', 'low')

    _update_account(ContextInfo)
    State.total_assets = State.cash + _calc_total_position_value(ContextInfo, hist_close)
    _sync_positions(ContextInfo)

    State.market_ok = _check_market(hist_close)
    if not State.market_ok:
        print("[市场] %s < MA%d×%.0f%%，空仓避险" % (
            BENCHMARK, MA_MARKET, (1-MARKET_FILTER_PCT)*100))

    date_str = _log_time(ContextInfo)
    cd_cnt = sum(1 for d in State.stop_cooldown.values() if d > 0)
    stop_mode = "紧%d天" % EARLY_STOP_DAYS
    print("=" * 50)
    print("[%s] bar=%d cnt=%d 持仓=%d只 资产=%.0f万 现金=%.0f万 市场=%s 冷却=%d 止损=%s" % (
        date_str, bar, State.bar_counter, len(State.positions),
        State.total_assets/10000, State.cash/10000,
        "可交易" if State.market_ok else "防御", cd_cnt, stop_mode))

    _process_pending_sells(ContextInfo, hist_close)
    _check_exits(ContextInfo, hist_close)

    if State.bar_counter >= State.next_refresh_bar:
        # ★v5新增★ 刷新时同步更新市值与档位
        State.market_caps = _compute_market_caps(hist_close)
        State.tier_map = _assign_tiers(State.market_caps)
        State.rankings = _compute_factor_rankings(hist_close, hist_amount)
        State.next_refresh_bar = State.bar_counter + REFRESH_INTERVAL
        _log_tier_distribution(hist_close)
        print("[刷新] 因子排名完成：%d 只进入候选池" % len(State.rankings))

    if State.market_ok and len(State.positions) < MAX_POSITIONS:
        _check_entry_breakout_v5(ContextInfo, hist_close, hist_amount,
                                  hist_open, hist_high, hist_low)

    if not State.market_ok and len(State.positions) > 0:
        _liquidate_all(ContextInfo, hist_close, "大盘防御")

    for code in list(State.positions.keys()):
        State.positions[code]['bars_held'] += 1

    pos_codes = list(State.positions.keys())
    hold_days = [State.positions[c]['bars_held'] for c in pos_codes]
    pos_labels = [_stock_label(c) for c in pos_codes]
    print("[摘要] 持仓=%d只 %s | 持有=%s | 刷新还有=%d天" % (
        len(State.positions), pos_labels if pos_labels else "空仓",
        hold_days if hold_days else "-",
        State.next_refresh_bar - State.bar_counter))


# ╔════════════════════════════════════════════════════════════╗
# ║  ★v5新增★ 档位分布统计                                     ║
# ╚════════════════════════════════════════════════════════════╝

def _log_tier_distribution(hist_close):
    """打印候选池各档位分布。"""
    if not State.rankings:
        return
    counts = {i: 0 for i in range(N_TIERS)}
    for code in State.rankings:
        t = State.tier_map.get(code, -1)
        if t in counts:
            counts[t] += 1
    parts = []
    for i in range(N_TIERS):
        parts.append("%s:%d只" % (MARKET_CAP_TIERS[i][2], counts[i]))
    over = sum(1 for c in State.rankings if State.tier_map.get(c, -1) == -1)
    if over:
        parts.append("超档:%d只" % over)
    print("[分层] 候选池分布 → %s" % " | ".join(parts))


# ╔════════════════════════════════════════════════════════════╗
# ║  ★v5重写★ 入场判断（市值分层 + 6层过滤）                     ║
# ╚════════════════════════════════════════════════════════════╝

def _check_entry_breakout_v5(ContextInfo, hist_close, hist_amount,
                              hist_open, hist_high, hist_low):
    """
    ★v5重写★ 市值分层入场：
      每档独立配额（各3只），按因子值从高到低遍历候选池，
      每档最多买入其配额数；某档无候选则跳过，不跨档借名额。

    过滤链（与 v4 相同，按强度从强到弱）：
      (0) 档位配额 — 该档已满 / 超档(>3000亿) 跳过 ★新增★
      (A) 行业黑名单 — 跳过5个持续亏损行业
      (B) 止损冷却 — 60天内不买回
      (C) 跳空低开 — 开盘低开3%+不买
      (D) ATR波动上限 — 日振幅>8%的高波动股不买
      (E) 动量确认 — 近3日至少2日收阳
      (F) MA20斜率 — 均线必须上升
      (G) 趋势MA20 — 价格 > MA20
      (H) 量能 — 放量≥1.2x均量
      (I) 突破强度 — 收盘 > N日最高×1.005
    """
    if not State.rankings: return

    held = set(State.positions.keys())

    # ★v5新增★ 统计各档位当前持仓数，计算剩余名额
    tier_counts = {i: 0 for i in range(N_TIERS)}
    for code in State.positions:
        t = State.tier_map.get(code, -1)
        if t in tier_counts:
            tier_counts[t] += 1
    tier_slots = {i: MARKET_CAP_TIERS[i][1] - tier_counts[i] for i in range(N_TIERS)}
    if sum(tier_slots.values()) <= 0: return

    ranked = sorted(State.rankings.keys(), key=lambda c: State.rankings[c], reverse=True)

    signals = []
    skip_over = skip_full = 0
    skip_bl = skip_cd = skip_gap = skip_atr = skip_mom = 0
    skip_ma_slope = skip_trend = skip_vol = 0

    for code in ranked:
        if code in held: continue

        # (0) 档位配额 ★v5新增★
        tier = State.tier_map.get(code, -1)
        if tier < 0:
            skip_over += 1; continue            # 超档(>3000亿)或市值数据缺失
        if tier_slots.get(tier, 0) <= 0:
            skip_full += 1; continue            # 该档名额已满

        # (A) 行业黑名单
        if _get_sector(code) in SECTOR_BLACKLIST:
            skip_bl += 1; continue

        # (B) 止损冷却
        if State.stop_cooldown.get(code, 0) > 0:
            skip_cd += 1; continue

        # 数据检查
        close_arr = hist_close.get(code, [])
        min_len = max(BREAKOUT_PERIOD, MA_STOCK+MA_SLOPE_PERIOD, VOL_LOOKBACK, MOMENTUM_DAYS) + 2
        if len(close_arr) < min_len: continue

        arr = np.array(close_arr, dtype=float)
        cur_close = arr[-1]; prev_close = arr[-2] if len(arr) >= 2 else cur_close

        # 涨停跳过
        daily_ret = (cur_close - prev_close) / prev_close if prev_close > 0 else 0
        if daily_ret >= LIMIT_UP_PCT:
            print("  [入场跳过] %s 涨停(%.1f%%)" % (_stock_label(code), daily_ret*100))
            continue

        # (C) 跳空低开
        if _check_gap_down(code, hist_close, hist_open):
            skip_gap += 1; continue

        # (D) ATR波动上限
        if not _has_low_volatility(code, hist_high, hist_low, hist_close):
            skip_atr += 1; continue

        # (E) 动量确认
        if not _has_momentum(code, hist_close):
            skip_mom += 1; continue

        # (F) MA20斜率
        if not _is_ma_rising(code, hist_close):
            skip_ma_slope += 1; continue

        # (G) 价格 > MA20
        if not _is_price_above_ma(code, hist_close):
            skip_trend += 1; continue

        # 突破检查
        past_high = np.max(arr[-(BREAKOUT_PERIOD+1):-1])

        # (I) 突破强度
        if cur_close <= past_high * (1 + BREAKOUT_STRENGTH_PCT):
            continue  # 没有有效突破

        # (H) 量能
        amount_arr = hist_amount.get(code, [])
        vol_ok = True
        if len(amount_arr) >= VOL_LOOKBACK + 1:
            try:
                today_amt = float(amount_arr[-1])
                past_amts = [float(amount_arr[i]) for i in range(-(VOL_LOOKBACK+1), -1)
                            if amount_arr[i] is not None]
                avg_amt = np.mean(past_amts) if past_amts else 0
                vol_ok = today_amt >= avg_amt * VOL_RATIO_MIN if avg_amt > 0 else True
            except Exception: vol_ok = True
        if not vol_ok:
            skip_vol += 1; continue

        # ── 通过全部过滤 ──
        factor_val = State.rankings.get(code, 0)
        signals.append((code, cur_close, factor_val))
        tier_slots[tier] -= 1
        print("  [信号] %s 突破%d日新高! 收盘=%.2f %d日最高=%.2f alpha144=%.2e [%s]" % (
            _stock_label(code), BREAKOUT_PERIOD, cur_close,
            BREAKOUT_PERIOD, past_high, factor_val,
            MARKET_CAP_TIERS[tier][2]))

        if sum(tier_slots.values()) <= 0: break

    total = (skip_over + skip_full + skip_bl + skip_cd + skip_gap + skip_atr
             + skip_mom + skip_ma_slope + skip_trend + skip_vol)
    if total > 0:
        print("  [过滤] 超档=%d 档满=%d 黑名单=%d 冷却=%d 跳空=%d ATR=%d 动量=%d MA斜率=%d 趋势=%d 缩量=%d" % (
            skip_over, skip_full, skip_bl, skip_cd, skip_gap, skip_atr, skip_mom,
            skip_ma_slope, skip_trend, skip_vol))

    if signals:
        _buy_signals(ContextInfo, signals)


# ╔════════════════════════════════════════════════════════════╗
# ║  ★v4修改★ 首3日紧止损                                      ║
# ╚════════════════════════════════════════════════════════════╝

def _check_exits(ContextInfo, hist_close):
    """
    出场检查（★v4修改★ 前EARLY_STOP_DAYS天使用紧止损）。
    盘中止损回调在实盘中会覆盖此日线兜底。
    """
    to_sell = []
    for code, pos in State.positions.items():
        px = _get_price(ContextInfo, code, hist_close)
        if px <= 0: continue
        entry = pos['entry_price']
        pnl = (px / entry - 1.0) if entry > 0 else 0

        # ★v4修改★ 前3天用紧止损，之后用标准止损
        stop_line = _get_effective_stop(code)
        if pnl <= stop_line:
            tag = "紧" if stop_line == EARLY_STOP_PCT else ""
            print("  [止损触发] %s 浮亏%.1f%% <= %.0f%% (%s止损)" % (
                _stock_label(code), pnl*100, stop_line*100, tag or "标准"))
            to_sell.append((code, "%s止损%.0f%%(浮亏%.1f%%)" % (tag, stop_line*100, pnl*100)))
            continue

        if pos['bars_held'] >= MAX_HOLD_BARS:
            to_sell.append((code, "持有%d天到期" % MAX_HOLD_BARS))

    for code, reason in to_sell:
        _sell_position(ContextInfo, code, hist_close, reason)


# ╔════════════════════════════════════════════════════════════╗
# ║  其余函数（同v4，略作适配）                                  ║
# ╚════════════════════════════════════════════════════════════╝

def _update_cooldown():
    for code in list(State.stop_cooldown.keys()):
        State.stop_cooldown[code] -= 1
        if State.stop_cooldown[code] <= 0:
            del State.stop_cooldown[code]

def _buy_signals(ContextInfo, signals):
    total_equity = State.total_assets if State.total_assets > 0 else State.capital
    alloc = total_equity / MAX_POSITIONS
    sec_counts = {}
    for c in State.positions.keys():
        s = _get_sector(c); sec_counts[s] = sec_counts.get(s, 0) + 1

    for code, price, factor_val in signals:
        if code in State.positions: continue
        if len(State.positions) >= MAX_POSITIONS: break
        sec = _get_sector(code)
        lim = MAX_SECTOR_OTHER if sec == UNKNOWN_SECTOR else MAX_SECTOR_COUNT
        if sec_counts.get(sec, 0) >= lim:
            print("  [买入跳过] %s 已满%d只(上限%d)" % (_stock_label(code), sec_counts[sec], lim))
            continue
        shares = max(100, int(alloc / price / 100) * 100)
        need = shares * price * 1.002
        if need > State.cash:
            shares = max(100, int(State.cash * 0.98 / price / 100) * 100)
            if shares < 100:
                print("  [买入失败] %s 资金不足" % _stock_label(code)); continue
        try:
            passorder(23, 1101, State.acc_id, code, 5, -1, shares,
                      'Alpha144', 1, '', ContextInfo)
        except Exception as e:
            print("  [买入失败] %s: %s" % (_stock_label(code), str(e))); continue
        State.positions[code] = {'shares': shares, 'entry_price': price,
                                  'entry_bar': State.last_barpos, 'bars_held': 0}
        sec_counts[sec] = sec_counts.get(sec, 0) + 1
        print(">>> [买入] %s × %d股 @ %.2f | 金额%.0f | alpha144=%.2e" % (
            _stock_label(code), shares, price, shares*price, factor_val))

def _process_pending_sells(ContextInfo, hist_close):
    if not State.pending_sells: return
    retry = list(State.pending_sells)
    State.pending_sells = []
    for code in retry:
        if code not in State.positions: continue
        _sell_position(ContextInfo, code, hist_close, "补卖(昨日跌停)")

def _sell_position(ContextInfo, code, hist_close, reason):
    if code not in State.positions: return
    pos = State.positions[code]; shares = pos.get('shares', 0)
    if shares <= 0: del State.positions[code]; return
    px = _get_price(ContextInfo, code, hist_close)
    close_arr = hist_close.get(code, [])
    if len(close_arr) >= 2:
        a = np.array(close_arr, dtype=float)
        dr = (a[-1]-a[-2])/a[-2] if a[-2]>0 else 0
        if dr <= LIMIT_DOWN_PCT:
            print("  [卖出延迟] %s 跌停(%.1f%%)" % (_stock_label(code), dr*100))
            if code not in State.pending_sells: State.pending_sells.append(code)
            return
    try:
        passorder(24, 1101, State.acc_id, code, 5, -1, shares,
                  'Alpha144卖出', 1, '', ContextInfo)
    except Exception as e:
        print("  [卖出失败] %s: %s" % (_stock_label(code), str(e))); return
    entry = pos['entry_price']
    pnl = (px/entry-1)*100 if entry>0 else 0; bars = pos.get('bars_held',0)
    print("<<< [卖出] %s × %d股 @ %.2f | 盈亏%+.1f%% | 持有%d天 | %s" % (
        _stock_label(code), shares, px, pnl, bars, reason))
    if '止损' in reason:
        State.stop_cooldown[code] = STOP_COOLDOWN
        print("  [冷却] %s 加入冷却名单(%d天)" % (_stock_label(code), STOP_COOLDOWN))
    del State.positions[code]
    if code in State.pending_sells: State.pending_sells.remove(code)

def _get_sector(code):
    if State.sector_ok: return State.sector_map.get(code, UNKNOWN_SECTOR)
    return UNKNOWN_SECTOR

def _liquidate_all(ContextInfo, hist_close, reason):
    for code in list(State.positions.keys()):
        _sell_position(ContextInfo, code, hist_close, reason)


# ╔════════════════════════════════════════════════════════════╗
# ║              辅助函数                                      ║
# ╚════════════════════════════════════════════════════════════╝

def _calc_alpha144(close_arr, amount_arr):
    c = np.array(close_arr, dtype=float); a = np.array(amount_arr, dtype=float)
    n = min(len(c), len(a)); need = FACTOR_WINDOW+1
    if n < need: return None
    rc = c[-need:]; ra = a[-need:]
    alpha = 0.0; neg = 0
    for i in range(1, len(rc)):
        if rc[i-1] > 0: ret = (rc[i]-rc[i-1])/rc[i-1]
        else: ret = 0
        if ret < 0:
            ai = ra[i] if i < len(ra) else 0
            if ai > 0: alpha += abs(ret)/ai; neg += 1
    return 0.0 if neg == 0 else alpha

def _compute_factor_rankings(hist_close, hist_amount):
    raw = {}
    for code in State.filtered_pool:
        ca = hist_close.get(code, []); aa = hist_amount.get(code, [])
        if len(ca) < FACTOR_WINDOW+1 or len(aa) < FACTOR_WINDOW+1: continue
        try:
            if np.mean(np.array(aa[-FACTOR_WINDOW:], dtype=float)) < MIN_DAILY_AMOUNT: continue
        except: continue
        v = _calc_alpha144(ca, aa)
        if v is not None: raw[code] = v
    if not raw: return {}
    s = sorted(raw.keys(), key=lambda c: raw[c], reverse=True)
    return {c: raw[c] for c in s[:max(1, int(len(s)*FACTOR_TOP_PCT))]}

def _check_market(hist_close):
    if BENCHMARK not in hist_close: return True
    a = hist_close[BENCHMARK]
    if len(a) < MA_MARKET+1: return True
    ca = np.array(a, dtype=float)
    return ca[-1] >= np.mean(ca[-MA_MARKET:]) * (1.0 - MARKET_FILTER_PCT)

def _calc_total_position_value(ContextInfo, hist_close):
    t = 0.0
    for c, p in State.positions.items():
        t += p.get('shares', 0) * _get_price(ContextInfo, c, hist_close)
    return t

def _update_account(ContextInfo):
    try:
        al = get_trade_detail_data(State.acc_id, 'stock', 'account')
        if al: State.cash = al[0].m_dAvailable; State.total_assets = al[0].m_dBalance; return
    except: pass
    try: State.cash = ContextInfo.cash; State.total_assets = ContextInfo.capital
    except: pass

def _sync_positions(ContextInfo):
    try:
        pl = get_trade_detail_data(State.acc_id, 'stock', 'position')
        rp = {}
        for p in pl:
            code = p.m_strInstrumentID + '.' + p.m_strExchangeID
            if p.m_nVolume <= 0: continue
            if code in State.positions:
                State.positions[code]['shares'] = p.m_nVolume; rp[code] = State.positions[code]
            else: rp[code] = {'shares': p.m_nVolume, 'entry_price': p.m_dOpenPrice,
                              'entry_bar': State.last_barpos, 'bars_held': 0}
        for c in list(State.positions.keys()):
            if c not in rp: del State.positions[c]
        for c, p in rp.items():
            if c not in State.positions: State.positions[c] = p
    except: pass

def _get_price(ContextInfo, code, hist_close):
    try:
        t = ContextInfo.get_full_tick([code])
        if code in t and t[code].get('lastPrice', 0) > 0: return t[code]['lastPrice']
    except: pass
    if code in hist_close and len(hist_close[code]) > 0: return float(hist_close[code][-1])
    return 0

def _log_time(ContextInfo):
    try: return timetag_to_datetime(ContextInfo.get_bar_timetag(ContextInfo.barpos), '%Y-%m-%d %H:%M')
    except: return str(ContextInfo.barpos)


# ╔════════════════════════════════════════════════════════════╗
# ║   硬编码 Fallback                                           ║
# ╚════════════════════════════════════════════════════════════╝

def _get_fallback_csi500():
    return [
        '300003.SZ','300009.SZ','300015.SZ','300026.SZ','300039.SZ',
        '002001.SZ','002007.SZ','002019.SZ','002020.SZ','002022.SZ',
        '600079.SH','600085.SH','600196.SH','600276.SH','600380.SH',
        '300595.SZ','300601.SZ','300633.SZ','300676.SZ','300725.SZ',
        '002049.SZ','002138.SZ','002185.SZ','002273.SZ','002371.SZ',
        '002409.SZ','002436.SZ','002456.SZ','002463.SZ','002475.SZ',
        '603160.SH','603501.SH','603986.SH','688008.SH','688012.SH',
        '002230.SZ','002368.SZ','002373.SZ','002405.SZ','002410.SZ',
        '300033.SZ','300036.SZ','300059.SZ','300168.SZ','300253.SZ',
        '002064.SZ','002092.SZ','002108.SZ','002250.SZ','002258.SZ',
        '002326.SZ','002407.SZ','002408.SZ','002440.SZ','002460.SZ',
        '002013.SZ','002025.SZ','002050.SZ','002074.SZ','002097.SZ',
        '300024.SZ','300124.SZ','300274.SZ','300316.SZ','300450.SZ',
        '002459.SZ','002121.SZ','002129.SZ','002202.SZ','002245.SZ',
        '300014.SZ','300037.SZ','300068.SZ','300073.SZ','300118.SZ',
        '000060.SZ','000630.SZ','000807.SZ','000831.SZ','000878.SZ',
        '000933.SZ','000960.SZ','000975.SZ','002155.SZ','002203.SZ',
        '002555.SZ','002602.SZ','002624.SZ','300058.SZ','300133.SZ',
        '300251.SZ','300413.SZ','300418.SZ','603444.SH',
        '002120.SZ','002352.SZ','002468.SZ','600026.SH','600029.SH',
        '000400.SZ','000401.SZ','000425.SZ','000528.SZ','000538.SZ',
        '000547.SZ','000553.SZ','000581.SZ','000625.SZ','000629.SZ',
        '000636.SZ','000656.SZ','000661.SZ','000703.SZ','000708.SZ',
        '000723.SZ','000728.SZ','000729.SZ','000738.SZ','000750.SZ',
        '000776.SZ','000778.SZ','000783.SZ','000786.SZ','000800.SZ',
        '000825.SZ','000826.SZ','000830.SZ','000860.SZ','000869.SZ',
        '000887.SZ','000895.SZ','000902.SZ','000903.SZ','000912.SZ',
        '000915.SZ','000921.SZ','000927.SZ','000930.SZ','000932.SZ',
        '000937.SZ','000938.SZ','000950.SZ','000951.SZ','000957.SZ',
        '000959.SZ','000961.SZ','000962.SZ','000963.SZ',
        '000966.SZ','000967.SZ','000968.SZ','000969.SZ','000970.SZ',
        '300498.SZ','002714.SZ','002304.SZ',
    ]
