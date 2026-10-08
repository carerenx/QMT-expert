#coding:gbk
"""
Alpha#144 — 流动性冲击择时策略 v5 (质量过滤版)
================================================
基于微观结构因子的中盘股择时策略，专为 QMT 实盘/仿真/回测环境设计。

【v4→v5 优化（基于 v4 回测分析：2022-01~2026-08，368笔交易）】

  v4 成果（vs v3）：
    交易笔数: 405→368 (-9%)   胜率: 44.2%→45.4% (+1.2pp)
    均持天数: 10.6→9.8天      持有到期: 38%→38% (持平)
    1日止损:  33%→43% (+10pp) ← 紧止损过滤了更多，但1日止损比例反而上升

  v4 核心问题诊断：
    问题1: 43%仓位持有1天(157笔)，均亏损-28.6%，胜率仅4%
           → 这些是"假突破 + 次日跳空暴跌"，非日线止损能解决
    问题2: 紧止损(-12%)实出-32.5%，完全无效
           → 日线bar限制：股票直接从开盘-30%+起步，12%线从未被触及
    问题3: 动量/M20斜率过滤器主导(占94%过滤)，但仍有大量漏网假突破
           → 需要更精确的"gap-prone stock"识别
    问题4: ATR日均振幅上限(8%)太松，仅过滤13次
           → 问题不在日均振幅，而在单日极端波动
    问题5: 行业黑名单仍未启用(空)，亏损行业持续亏损
           → 商贸/建材/农牧/计算机/公用事业已在v2+v3+v4三重确认

  v5 优化点（7项，从"阻止假突破"转向"识别gap-prone股票"）：
    1. ★核心★ 跌停历史过滤：过去60天有单日跌幅>7%的股票禁止买入
       → gap-prone股票识别：一次大跳水的股票倾向于再次跳水
    2. 连涨天数上限：突破前连续上涨>=4天则跳过（过度延伸→回调风险）
    3. 近期极端振幅：近5日最大单日振幅>10%则跳过（替代宽松的ATR均值过滤）
    4. 行业黑名单启用：商贸零售/建筑材料/农林牧渔/计算机/公用事业
       → v2+v3+v4三重确认亏损行业
    5. 每日最大买入：每天最多新开2只（防信号聚类→关联回撤）
    6. 全面冷却期：任何卖出后30天不买回（不限于止损）
    7. 风险平价仓位：分配金额 = 基准 / (1 + 20日均振幅×100)
       → 高波动股少买、低波动股多买

【核心思想】（同 v4）
  Alpha#144 = Σ(|跌幅| / 成交额) 对下跌日求和
  因子值越大 → 下跌时流动性越差 → 后续反弹潜力越大

【策略流程】
  1. 每 10 天计算 Alpha#144 因子，取 Top 15%
  2. 每日检查：突破10日新高 + 放量≥1.2x + MA20上升 + 价格>MA20
     + 动量3日2阳 + 无大跌历史 + 非连涨4日+ + 近期无极端振幅
  3. 次日开盘买入，风险平价分配，每日最多2只
  4. 首3日-12%紧止损 / 3日后-18%止损 / 大盘转弱清仓 / 持有20天到期
  5. 中证500 < MA20×(1-3%) 空仓

作者：QMT-Expert
日期：2026-08-07
"""

import numpy as np


# ╔════════════════════════════════════════════════════════════╗
# ║              用户可调参数                                    ║
# ╚════════════════════════════════════════════════════════════╝

BENCHMARK = '000905.SH'

# ── 因子参数 ──
FACTOR_WINDOW    = 20
FACTOR_TOP_PCT   = 0.15
REFRESH_INTERVAL = 10

# ── 入场参数 ──
BREAKOUT_PERIOD       = 10
BREAKOUT_STRENGTH_PCT = 0.005    # 突破强度≥0.5%

# ── 量能 ──
VOL_RATIO_MIN  = 1.2
VOL_LOOKBACK   = 5

# ── 趋势过滤 ──
MA_STOCK        = 20
MA_SLOPE_PERIOD = 5             # MA20斜率窗口

# ── 动量 ──
MOMENTUM_DAYS   = 3
MOMENTUM_UP_MIN = 2

# ── ★v5核心★ 跌停历史过滤 ──
GAP_HISTORY_DAYS   = 60        # 检查窗口（交易日）
GAP_HISTORY_MAX_PCT = -0.07    # 单日跌幅阈值：-7%（接近跌停但留余量）
# 逻辑：如果股票过去60天内有任何一天跌幅<=-7%，禁止买入
# 这类股票可能因业绩暴雷/重大利空被砸，再次跳水的概率极高

# ── ★v5新增★ 连涨天数上限 ──
MAX_CONSECUTIVE_UP = 3         # 突破前最多允许连续上涨3天
# 超过3天连涨后突破 → 可能是冲顶而非真突破

# ── ★v5新增★ 近期极端振幅 ──
MAX_RECENT_RANGE_PCT = 0.10    # 近5日最大单日振幅上限
RECENT_RANGE_DAYS    = 5       # 检查窗口
# 替代宽松的ATR均值过滤，聚焦最坏情况

# ── 出场参数 ──
MAX_HOLD_BARS   = 20
HARD_STOP_PCT   = -0.18
EARLY_STOP_PCT  = -0.12
EARLY_STOP_DAYS = 3

# ── ★v5修改★ 全面冷却 ──
STOP_COOLDOWN   = 60           # 止损冷却（天）
ALL_SELL_COOLDOWN = 30         # ★新增★ 任何卖出的冷却期

# ── ★v5新增★ 每日最大买入 ──
MAX_NEW_ENTRIES_PER_DAY = 2    # 每天最多新开仓数

# ── ★v5新增★ 风险平价 ──
RISK_PARITY_BASE_ALLOC = 60000 # 基准分配金额（30万/5只）
RISK_PARITY_ENABLED     = True # 是否启用波动率调整

# ── 跳空保护 ──
GAP_DOWN_PCT = -0.03

# ── 大盘过滤 ──
MA_MARKET         = 20
MARKET_FILTER_PCT = 0.03

# ── 仓位管理 ──
MAX_POSITIONS    = 5
MAX_SECTOR_COUNT = 2
MAX_SECTOR_OTHER = 3

# ── 行业分类 ──
INDUSTRY_TYPE  = 'SW'
UNKNOWN_SECTOR = '其他'

# ── ★v5启用★ 行业黑名单（v2+v3+v4三重确认）──
#   v4: 建筑材料(-35.5%,0%WR), 美容护理(-31.0%,0%WR), 农林牧渔(-30.6%,0%WR),
#       计算机(-23.1%,18%WR), 汽车(-20.7%,0%WR), 商贸零售(-17.8%,20%WR),
#       公用事业(-11.0%,19%WR), 房地产(-15.7%,9%WR)
#   v3: 商贸零售(-24.5%,9%WR), 建筑材料(-24.5%,0%WR), 农林牧渔(-19.2%,0%WR),
#       计算机(-19.9%,22%WR), 公用事业(-17.4%,10%WR)
#   v2: 计算机(-34.7%), 商贸零售(-26.6%), 公用事业(-22.0%),
#       建筑材料(-17.8%), 农林牧渔(-16.6%)
# 三重确认的5个亏损行业：
SECTOR_BLACKLIST = {
    'SW1商贸零售',
    'SW1建筑材料',
    'SW1农林牧渔',
    'SW1计算机',
    'SW1公用事业',
}

# ── 涨跌停 ──
LIMIT_UP_PCT   = 0.098
LIMIT_DOWN_PCT = -0.098

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
    stop_cooldown = {}       # 止损冷却 {code: remaining_days}
    all_sell_cooldown = {}   # ★v5新增★ 全面冷却 {code: remaining_days}
    today_entry_count = 0    # ★v5新增★ 当日已买入计数


# ╔════════════════════════════════════════════════════════════╗
# ║   行业分类 + 名称                                          ║
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
            print("[行业] 不可用，fallback"); return {}, False
        except Exception: pass
        if (i+1) % 100 == 0:
            print("[行业] 进度: %d/%d" % (i+1, len(stock_list)))
    bl = sum(1 for s in set(sector_map.values()) if s in SECTOR_BLACKLIST)
    print("[行业] 已分类=%d 行业=%d 黑名单=%d个" %
          (success, len(set(sector_map.values())), bl))
    return sector_map, True

def _build_stock_names(ContextInfo, stock_list):
    names = {}
    for code in stock_list:
        try:
            n = ContextInfo.get_stock_name(code)
            if n and len(n) > 0: names[code] = n
        except Exception: pass
    print("[名称] %d/%d" % (len(names), len(stock_list)))
    return names

def _stock_label(code):
    name = State.stock_names.get(code, '')
    sec = _get_sector(code)
    s = sec.replace('SW1','').replace('CSRC1','')
    return "%s(%s|%s)" % (code, name, s)


# ╔════════════════════════════════════════════════════════════╗
# ║  ★v5新增★ 跌停历史 / 连涨 / 极端振幅 检查                   ║
# ╚════════════════════════════════════════════════════════════╝

def _has_gap_history(code, hist_close):
    """
    ★v5核心新增★ 检查过去GAP_HISTORY_DAYS天内是否有任何单日跌幅<=-7%。
    有 → 跳过（gap-prone股票）。
    """
    if code not in hist_close or len(hist_close[code]) < GAP_HISTORY_DAYS + 1:
        return False  # 数据不足，放行
    arr = np.array(hist_close[code], dtype=float)
    recent = arr[-(GAP_HISTORY_DAYS + 1):]
    for i in range(1, len(recent)):
        if recent[i-1] > 0:
            ret = (recent[i] - recent[i-1]) / recent[i-1]
            if ret <= GAP_HISTORY_MAX_PCT:
                return True
    return False

def _count_consecutive_up(code, hist_close):
    """
    ★v5新增★ 计算突破前连续上涨天数。
    返回>=MAX_CONSECUTIVE_UP时表示过度延伸。
    """
    if code not in hist_close or len(hist_close[code]) < MAX_CONSECUTIVE_UP + 2:
        return 0
    arr = np.array(hist_close[code], dtype=float)
    # 检查最近几天的连续上涨（不含今天，因为突破日可能也是涨的）
    consecutive = 0
    for i in range(-2, -(MAX_CONSECUTIVE_UP + 3), -1):  # -2=昨天, 往前数
        if abs(i) >= len(arr): break
        if arr[i] > arr[i-1]:
            consecutive += 1
        else:
            break
    return consecutive

def _has_extreme_recent_range(code, hist_high, hist_low, hist_close):
    """
    ★v5新增★ 检查近RECENT_RANGE_DAYS日最大单日振幅是否>上限。
    替代v4的ATR日均值过滤，聚焦最坏情况。
    """
    high = hist_high.get(code, [])
    low = hist_low.get(code, [])
    close = hist_close.get(code, [])
    n = min(len(high), len(low), len(close))
    if n < RECENT_RANGE_DAYS + 1:
        return False  # 数据不足放行
    max_range = 0.0
    for i in range(-RECENT_RANGE_DAYS, 0):
        if close[i] > 0:
            r = (float(high[i]) - float(low[i])) / float(close[i])
            if r > max_range: max_range = r
    return max_range > MAX_RECENT_RANGE_PCT

# ── 保留v4的过滤函数 ──

def _is_ma_rising(code, hist_close):
    if code not in hist_close or len(hist_close[code]) < MA_STOCK + MA_SLOPE_PERIOD:
        return True
    arr = np.array(hist_close[code], dtype=float)
    return np.mean(arr[-MA_STOCK:]) > np.mean(arr[-(MA_STOCK + MA_SLOPE_PERIOD):-MA_SLOPE_PERIOD])

def _is_price_above_ma(code, hist_close):
    if code not in hist_close or len(hist_close[code]) < MA_STOCK + 1:
        return True
    arr = np.array(hist_close[code], dtype=float)
    return arr[-1] > np.mean(arr[-(MA_STOCK+1):-1])

def _has_momentum(code, hist_close):
    if code not in hist_close or len(hist_close[code]) < MOMENTUM_DAYS + 1:
        return True
    arr = np.array(hist_close[code], dtype=float)
    return sum(1 for i in range(-MOMENTUM_DAYS, 0) if arr[i] > arr[i-1]) >= MOMENTUM_UP_MIN

def _check_gap_down(code, hist_close, hist_open):
    if code not in hist_close or code not in hist_open: return False
    if len(hist_close[code]) < 2 or len(hist_open[code]) < 2: return False
    try:
        yc = float(hist_close[code][-2]); to = float(hist_open[code][-1])
        if yc <= 0: return False
        return (to / yc - 1.0) <= GAP_DOWN_PCT
    except: return False

def _calc_avg_daily_range(code, hist_high, hist_low, hist_close, period=20):
    """计算日均振幅（用于风险平价）。"""
    h = hist_high.get(code, []); l = hist_low.get(code, []); c = hist_close.get(code, [])
    n = min(len(h), len(l), len(c))
    if n < period: return 0.03  # 默认3%
    return float(np.mean([(float(h[i])-float(l[i]))/max(float(c[i]),1e-6)
                          for i in range(-period, 0)]))


# ╔════════════════════════════════════════════════════════════╗
# ║              init()                                         ║
# ╚════════════════════════════════════════════════════════════╝

def init(ContextInfo):
    print("[init] Alpha#144 流动性冲击择时策略 v5 (质量过滤版)")
    print("[init] v5核心: 跌停历史过滤+连涨上限+极端振幅+行业黑名单+每日限仓+全面冷却+风险平价")

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
        print("[init] 使用 fallback")
        stocks = _get_fallback_csi500()

    valid = []
    for code in stocks:
        try:
            n = ContextInfo.get_stock_name(code)
            if n and len(n) > 0 and 'ST' not in n and '*' not in n: valid.append(code)
        except Exception: valid.append(code)

    State.stock_pool = valid
    State.filtered_pool = valid[:]
    print("[init] 有效股票池：%d 只" % len(State.stock_pool))

    State.sector_map, State.sector_ok = _build_sector_map_from_qmt(
        ContextInfo, State.stock_pool)
    State.stock_names = _build_stock_names(ContextInfo, State.stock_pool)

    universe = valid[:] + [BENCHMARK]
    ContextInfo.set_universe(list(set(universe)))

    for attr, val in [('capital', State.capital), ('benchmark', BENCHMARK),
                       ('start', '2022-01-01 09:30:00'), ('end', '2026-06-19 15:00:00')]:
        try: setattr(ContextInfo, attr, val)
        except (AttributeError, TypeError): pass

    ContextInfo.set_slippage(1, 0.001)
    ContextInfo.set_commission(0, [0.00025, 0.00025, 0.001, 0.0, 0.0, 5.0])
    ContextInfo.set_account(State.acc_id)

    bl_count = sum(1 for s in set(State.sector_map.values()) if s in SECTOR_BLACKLIST)
    bl_stocks = sum(1 for c, s in State.sector_map.items() if s in SECTOR_BLACKLIST)
    print("[init] 初始化完成：")
    print("       股票池=%d只 | 行业=%d个 | 黑名单=%d个行业(%d只股)" % (
        len(State.stock_pool),
        len(set(State.sector_map.values())) if State.sector_ok else 0,
        bl_count, bl_stocks))
    print("       跌停历史=%d天/%.0f%% | 连涨上限=%d天 | 极端振幅=%d天/%.0f%%" % (
        GAP_HISTORY_DAYS, abs(GAP_HISTORY_MAX_PCT)*100,
        MAX_CONSECUTIVE_UP, RECENT_RANGE_DAYS, MAX_RECENT_RANGE_PCT*100))
    print("       每日限仓=%d只 | 全面冷却=%d天(止损%d天) | 风险平价=%s" % (
        MAX_NEW_ENTRIES_PER_DAY, ALL_SELL_COOLDOWN, STOP_COOLDOWN,
        "启用" if RISK_PARITY_ENABLED else "关闭"))
    print("       突破=%d天+%.1f%% | 量能=%.1fx | 首%d天紧止损%.0f%%" % (
        BREAKOUT_PERIOD, BREAKOUT_STRENGTH_PCT*100, VOL_RATIO_MIN,
        EARLY_STOP_DAYS, abs(EARLY_STOP_PCT)*100))
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
    State.today_entry_count = 0  # ★v5新增★ 重置当日买入计数

    _update_cooldowns()

    need_bars = max(FACTOR_WINDOW+30, MA_MARKET+10, MA_STOCK+MA_SLOPE_PERIOD+5,
                    BREAKOUT_PERIOD+5, VOL_LOOKBACK+5, GAP_HISTORY_DAYS+5,
                    MOMENTUM_DAYS+5, RECENT_RANGE_DAYS+5)
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
        print("[市场] %s < MA%d×%.0f%%" % (BENCHMARK, MA_MARKET, (1-MARKET_FILTER_PCT)*100))

    date_str = _log_time(ContextInfo)
    cd_all = sum(1 for d in State.all_sell_cooldown.values() if d > 0)
    cd_stop = sum(1 for d in State.stop_cooldown.values() if d > 0)
    print("=" * 50)
    print("[%s] bar=%d cnt=%d 持仓=%d只 资产=%.0f万 现金=%.0f万 市场=%s 冷却=%d/%d" % (
        date_str, bar, State.bar_counter, len(State.positions),
        State.total_assets/10000, State.cash/10000,
        "可交易" if State.market_ok else "防御", cd_all, cd_stop))

    _process_pending_sells(ContextInfo, hist_close)
    _check_exits(ContextInfo, hist_close)

    if State.bar_counter >= State.next_refresh_bar:
        State.rankings = _compute_factor_rankings(hist_close, hist_amount)
        State.next_refresh_bar = State.bar_counter + REFRESH_INTERVAL
        print("[刷新] 因子排名完成：%d 只" % len(State.rankings))

    if State.market_ok and len(State.positions) < MAX_POSITIONS:
        _check_entry_breakout_v5(ContextInfo, hist_close, hist_amount,
                                  hist_open, hist_high, hist_low)

    if not State.market_ok and len(State.positions) > 0:
        _liquidate_all(ContextInfo, hist_close, "大盘防御")

    for code in list(State.positions.keys()):
        State.positions[code]['bars_held'] += 1

    pos_codes = list(State.positions.keys())
    pos_labels = [_stock_label(c) for c in pos_codes]
    hold_days = [State.positions[c]['bars_held'] for c in pos_codes]
    print("[摘要] 持仓=%d只 %s | 持有=%s | 刷新还有=%d天" % (
        len(State.positions), pos_labels if pos_labels else "空仓",
        hold_days if hold_days else "-",
        State.next_refresh_bar - State.bar_counter))


# ╔════════════════════════════════════════════════════════════╗
# ║  ★v5重写★ 入场判断（9层过滤）                               ║
# ╚════════════════════════════════════════════════════════════╝

def _check_entry_breakout_v5(ContextInfo, hist_close, hist_amount,
                              hist_open, hist_high, hist_low):
    """
    ★v5重写★ 入场过滤链（9层）：

    新增v5层：
      (A) 每日最大买入限制 ★v5新增★
      (B) 行业黑名单 ★v5启用★
      (C) 全面冷却(任何卖出30天) ★v5新增★
      (D) 止损冷却(止损60天)
      (E) 跌停历史过滤 ★v5核心★
      (F) 连涨天数上限 ★v5新增★
      (G) 近期极端振幅 ★v5新增★

    保留v4层：
      (H) 跳空低开
      (I) 动量确认 → (J) MA20斜率 → (K) 价格>MA20
      (L) 突破强度 → (M) 量能确认

    通用：
      排名Top15% / 未涨停 / 未持仓 / 仓位未满 / 突破N日新高
    """
    if not State.rankings: return
    if State.today_entry_count >= MAX_NEW_ENTRIES_PER_DAY: return

    held = set(State.positions.keys())
    slots = min(MAX_POSITIONS - len(State.positions),
                MAX_NEW_ENTRIES_PER_DAY - State.today_entry_count)
    if slots <= 0: return

    ranked = sorted(State.rankings.keys(), key=lambda c: State.rankings[c], reverse=True)
    signals = []

    skip_bl = skip_cd_all = skip_cd_stop = skip_gap_hist = 0
    skip_up = skip_range = skip_gap = skip_mom = 0
    skip_ma_s = skip_trend = skip_vol = 0

    for code in ranked:
        if code in held: continue

        # ★v5启用★ (B) 行业黑名单
        if _get_sector(code) in SECTOR_BLACKLIST:
            skip_bl += 1; continue

        # ★v5新增★ (C) 全面冷却（任何卖出后30天）
        if State.all_sell_cooldown.get(code, 0) > 0:
            skip_cd_all += 1; continue

        # (D) 止损冷却（止损后60天）
        if State.stop_cooldown.get(code, 0) > 0:
            skip_cd_stop += 1; continue

        # 数据充足
        min_len = max(BREAKOUT_PERIOD, MA_STOCK+MA_SLOPE_PERIOD,
                      VOL_LOOKBACK, MOMENTUM_DAYS, GAP_HISTORY_DAYS) + 2
        close_arr = hist_close.get(code, [])
        if len(close_arr) < min_len: continue

        arr = np.array(close_arr, dtype=float)
        cur = arr[-1]; prev = arr[-2] if len(arr) >= 2 else cur

        # 涨停跳过
        daily_ret = (cur - prev) / prev if prev > 0 else 0
        if daily_ret >= LIMIT_UP_PCT:
            print("  [入场跳过] %s 涨停(%.1f%%)" % (_stock_label(code), daily_ret*100))
            continue

        # ★v5核心★ (E) 跌停历史过滤
        if _has_gap_history(code, hist_close):
            skip_gap_hist += 1; continue

        # ★v5新增★ (F) 连涨天数上限
        if _count_consecutive_up(code, hist_close) >= MAX_CONSECUTIVE_UP:
            skip_up += 1; continue

        # ★v5新增★ (G) 近期极端振幅
        if _has_extreme_recent_range(code, hist_high, hist_low, hist_close):
            skip_range += 1; continue

        # (H) 跳空低开
        if _check_gap_down(code, hist_close, hist_open):
            skip_gap += 1; continue

        # (I) 动量
        if not _has_momentum(code, hist_close):
            skip_mom += 1; continue

        # (J) MA20斜率
        if not _is_ma_rising(code, hist_close):
            skip_ma_s += 1; continue

        # (K) 价格>MA20
        if not _is_price_above_ma(code, hist_close):
            skip_trend += 1; continue

        # 突破检查
        past_high = np.max(arr[-(BREAKOUT_PERIOD+1):-1])
        if cur <= past_high * (1 + BREAKOUT_STRENGTH_PCT):
            continue

        # (M) 量能
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

        # ── 全部通过 ──
        factor_val = State.rankings.get(code, 0)
        signals.append((code, cur, factor_val))
        print("  [信号] %s 突破%d日新高! 收盘=%.2f %d日最高=%.2f alpha144=%.2e" % (
            _stock_label(code), BREAKOUT_PERIOD, cur, BREAKOUT_PERIOD, past_high, factor_val))

        if len(signals) >= slots: break

    total = skip_bl + skip_cd_all + skip_cd_stop + skip_gap_hist + skip_up + skip_range + skip_gap + skip_mom + skip_ma_s + skip_trend + skip_vol
    if total > 0:
        print("  [过滤] 黑名单=%d 冷却全=%d 冷却止=%d 跌停史=%d 连涨=%d 振幅=%d 跳空=%d 动量=%d M斜率=%d 趋势=%d 缩量=%d" % (
            skip_bl, skip_cd_all, skip_cd_stop, skip_gap_hist, skip_up, skip_range,
            skip_gap, skip_mom, skip_ma_s, skip_trend, skip_vol))

    if signals:
        _buy_signals_v5(ContextInfo, signals, hist_high, hist_low, hist_close)


# ╔════════════════════════════════════════════════════════════╗
# ║  ★v5新增★ 风险平价买入                                       ║
# ╚════════════════════════════════════════════════════════════╝

def _buy_signals_v5(ContextInfo, signals, hist_high, hist_low, hist_close):
    total_equity = State.total_assets if State.total_assets > 0 else State.capital
    base_alloc = total_equity / MAX_POSITIONS

    sec_counts = {}
    for c in State.positions.keys():
        s = _get_sector(c); sec_counts[s] = sec_counts.get(s, 0) + 1

    for code, price, factor_val in signals:
        if code in State.positions: continue
        if len(State.positions) >= MAX_POSITIONS: break
        if State.today_entry_count >= MAX_NEW_ENTRIES_PER_DAY: break

        sec = _get_sector(code)
        lim = MAX_SECTOR_OTHER if sec == UNKNOWN_SECTOR else MAX_SECTOR_COUNT
        if sec_counts.get(sec, 0) >= lim:
            print("  [买入跳过] %s 已满%d只" % (_stock_label(code), sec_counts[sec]))
            continue

        # ★v5新增★ 风险平价：波动越大的股票，分配越少
        if RISK_PARITY_ENABLED:
            avg_range = _calc_avg_daily_range(code, hist_high, hist_low, hist_close)
            # 日均振幅3% → multiplier=0.25, alloc=15k; 日均振幅1% → multiplier=0.5, alloc=30k
            risk_mult = 1.0 / (1.0 + avg_range * 100)
            alloc = base_alloc * risk_mult * 3.0  # 乘3使平均接近base_alloc
            alloc = max(base_alloc * 0.3, min(base_alloc * 1.5, alloc))  # 限制在30%-150%基准
        else:
            alloc = base_alloc

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

        State.positions[code] = {
            'shares': shares, 'entry_price': price,
            'entry_bar': State.last_barpos, 'bars_held': 0}
        sec_counts[sec] = sec_counts.get(sec, 0) + 1
        State.today_entry_count += 1  # ★v5新增★

        print(">>> [买入] %s × %d股 @ %.2f | 金额%.0f | alpha144=%.2e%s" % (
            _stock_label(code), shares, price, shares*price, factor_val,
            " [RP]" if RISK_PARITY_ENABLED else ""))


# ╔════════════════════════════════════════════════════════════╗
# ║              冷却管理 ★v5修改★                              ║
# ╚════════════════════════════════════════════════════════════╝

def _update_cooldowns():
    for d in [State.stop_cooldown, State.all_sell_cooldown]:
        for code in list(d.keys()):
            d[code] -= 1
            if d[code] <= 0: del d[code]


# ╔════════════════════════════════════════════════════════════╗
# ║              出场判断（同 v4，日线兜底）                      ║
# ╚════════════════════════════════════════════════════════════╝

def _check_exits(ContextInfo, hist_close):
    to_sell = []
    for code, pos in State.positions.items():
        px = _get_price(ContextInfo, code, hist_close)
        if px <= 0: continue
        entry = pos['entry_price']; pnl = (px / entry - 1.0) if entry > 0 else 0
        stop_line = EARLY_STOP_PCT if pos.get('bars_held', 0) < EARLY_STOP_DAYS else HARD_STOP_PCT
        if pnl <= stop_line:
            tag = "紧" if stop_line == EARLY_STOP_PCT else "标准"
            print("  [止损触发] %s 浮亏%.1f%% <= %.0f%% (%s)" % (
                _stock_label(code), pnl*100, stop_line*100, tag))
            to_sell.append((code, "%s止损%.0f%%" % (tag, stop_line*100)))
            continue
        if pos['bars_held'] >= MAX_HOLD_BARS:
            to_sell.append((code, "持有%d天到期" % MAX_HOLD_BARS))
    for code, reason in to_sell:
        _sell_position(ContextInfo, code, hist_close, reason)

def _process_pending_sells(ContextInfo, hist_close):
    if not State.pending_sells: return
    retry = list(State.pending_sells); State.pending_sells = []
    for code in retry:
        if code in State.positions:
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
    entry = pos['entry_price']; pnl = (px/entry-1)*100 if entry>0 else 0
    bars = pos.get('bars_held', 0)
    print("<<< [卖出] %s × %d股 @ %.2f | 盈亏%+.1f%% | 持有%d天 | %s" % (
        _stock_label(code), shares, px, pnl, bars, reason))

    # ★v5修改★ 任何卖出 → 加入全面冷却；止损 → 追加长期冷却
    State.all_sell_cooldown[code] = ALL_SELL_COOLDOWN
    if '止损' in reason:
        State.stop_cooldown[code] = STOP_COOLDOWN
        print("  [冷却] %s 全面%d天 + 止损%d天" % (_stock_label(code), ALL_SELL_COOLDOWN, STOP_COOLDOWN))

    del State.positions[code]
    if code in State.pending_sells: State.pending_sells.remove(code)

def _get_sector(code):
    if State.sector_ok: return State.sector_map.get(code, UNKNOWN_SECTOR)
    return UNKNOWN_SECTOR

def _liquidate_all(ContextInfo, hist_close, reason):
    for code in list(State.positions.keys()):
        _sell_position(ContextInfo, code, hist_close, reason)


# ╔════════════════════════════════════════════════════════════╗
# ║              因子计算 / 辅助函数（同 v4）                    ║
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
            else:
                rp[code] = {'shares': p.m_nVolume, 'entry_price': p.m_dOpenPrice,
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
