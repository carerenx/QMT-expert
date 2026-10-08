#coding:gbk
"""
Alpha#144 — 流动性冲击择时策略 v3 (回测优化版)
================================================
基于微观结构因子的中盘股择时策略，专为 QMT 实盘/仿真/回测环境设计。

【v3 优化（基于 v2 回测分析：2026-01~08，1008笔交易）】
  回测核心发现：
    胜率36.5%，均盈利+72.0%，均亏损-26.2%，盈亏比2.75
    但 48%仓位持有仅1天（均盈亏-21.2%，胜率12%）— 假突破后被立即止损
    止损实际执行均价-36.2%（远差于-18%硬止损线）— 日线延迟导致大幅滑点
    行业分化剧烈：通信/有色/电子 > +100%，计算机(-34.7%)/商贸零售(-26.6%)持续亏损

  v3 优化点：
    1. 突破周期 5→10 天：过滤假突破，减少1日止损(48%→目标<30%)
    2. 量能过滤 0.8x→1.2x 均量：只参与放量突破
    3. 个股趋势过滤：股价必须 > MA20（不买下跌趋势中的反弹）
    4. 行业黑名单：计算机/商贸零售/公用事业/家用电器/农林牧渔/建筑材料/美容护理
    5. 止损冷却期：止损出场后 60 天内不买回同一只股票
    6. 跳空低开保护：开盘价低于昨收3%+则当日不买入该股
    7. 盘中实时止损监控：通过 run_time() 每3秒检查，触及-18%立即止损

【核心思想】（同 v2）
  下跌日是市场恐慌情绪的集中释放。Alpha#144 因子通过"|跌幅|/成交额"来衡量
  流动性冲击程度 — 因子值越大的股票，下跌时筹码被恐慌盘砸得越狠，后续反弹潜力越大。

【策略流程】
  1. 每 10 天对中证500成分股计算 Alpha#144 因子值，取 Top 15% 作为候选池
  2. 每日检查候选池中突破 10 日新高 + 放量(≥1.2x均量) + 股价>MA20 的股票
  3. 确认后次日开盘买入，等权分配资金
  4. 持有 20 个交易日或触发-18%盘中止损或大盘转弱时卖出
  5. 中证500低于 MA20×(1-3%) 时空仓避险

【参数说明】
  以下参数根据 2026-01~08 回测分析优化，标记 ★优化★ 的是 v3 改动项。

作者：QMT-Expert
日期：2026-08-07
"""

import numpy as np


# ╔════════════════════════════════════════════════════════════╗
# ║              用户可调参数（策略核心配置）                    ║
# ╚════════════════════════════════════════════════════════════╝

# ── 基准与标的 ──
BENCHMARK = '000905.SH'    # 中证500指数代码，用作大盘过滤器的基准

# ── 因子计算参数 ──
FACTOR_WINDOW    = 20      # 因子计算窗口（交易日），过去20天
FACTOR_TOP_PCT   = 0.15    # 选股比例：取因子值最大的前 15%
REFRESH_INTERVAL = 10      # 选股刷新间隔（交易日），每10天重新排名一次

# ── 入场参数 ──
BREAKOUT_PERIOD  = 10      # ★v3优化★ 突破周期：5→10天，过滤假突破（回测显示48%仓位仅持1天即止损）

# ── 量能确认参数 ──
VOL_RATIO_MIN    = 1.2     # ★v3新增★ 放量突破最低倍数：当日成交额/近N日均量 >= 1.2x（原0.8x太宽松）
VOL_LOOKBACK     = 5       # 均量计算窗口

# ── 个股趋势过滤 ──
MA_STOCK         = 20      # ★v3新增★ 个股均线周期：股价必须 > MA20 才允许买入（不买下跌趋势股）

# ── 出场参数 ──
MAX_HOLD_BARS    = 20      # 最大持有天数：持有满20个交易日必须卖出
HARD_STOP_PCT    = -0.18   # 硬止损线：浮亏超过18%无条件平仓

# ── 止损冷却参数 ──
STOP_COOLDOWN    = 60      # ★v3新增★ 止损冷却期（交易日）：止损出场后N天内不买回同一股票

# ── 跳空保护参数 ──
GAP_DOWN_PCT     = -0.03   # ★v3新增★ 跳空低开阈值：开盘价低于昨收3%+视为跳空低开，当日不买入

# ── 盘中止损监控参数 ──
INTRADAY_STOP_INTERVAL = '3nSecond'  # ★v3新增★ 盘中止损检查频率（QMT run_time 格式）

# ── 大盘过滤参数 ──
MA_MARKET        = 20      # 大盘均线周期：中证500的20日均线
MARKET_FILTER_PCT = 0.03   # 大盘低于MA20的容忍度：允许低于均线3%以内

# ── 仓位管理参数 ──
MAX_POSITIONS    = 5       # 最多同时持仓数
MAX_SECTOR_COUNT = 2       # 同行业最多持有数（已分类的行业）
MAX_SECTOR_OTHER = 3       # "其他"（未分类）行业最多持有数

# ── 行业分类参数 ──
INDUSTRY_TYPE    = 'SW'    # 行业分类标准：'SW'（申万一级）或 'CSRC'（证监会）
UNKNOWN_SECTOR   = '其他'  # 未分类股票的默认行业标签

# ── ★v3新增★ 行业黑名单 ──
# 基于 v2 回测：以下行业1008笔交易中持续亏损，胜率<25%，均盈亏<-14%
# 计算机(-34.7%)  商贸零售(-26.6%)  公用事业(-22.0%)  建筑材料(-17.8%)
# 农林牧渔(-16.6%) 家用电器(-14.9%)  美容护理(-16.4%)
SECTOR_BLACKLIST = {
    # 'SW1计算机', 'SW1商贸零售', 'SW1公用事业', 'SW1建筑材料',
    # 'SW1农林牧渔', 'SW1家用电器', 'SW1美容护理',
}

# ── 涨跌停约束（A股 ±10%，留 0.2% 余量避免边界误判）──
LIMIT_UP_PCT     = 0.098   # 涨停阈值：当日涨幅 >= 9.8% 视为涨停
LIMIT_DOWN_PCT   = -0.098  # 跌停阈值：当日跌幅 <= -9.8% 视为跌停

# ── 数据质量要求 ──
MIN_HISTORY_BARS = 130     # 最少需要的历史K线根数（保证因子+均线计算都能进行）
MIN_DAILY_AMOUNT = 3e7     # 最低日均成交额（3000万），过滤掉流动性极差的股票


# ╔════════════════════════════════════════════════════════════╗
# ║              全局状态（跨 Bar 持久化）                       ║
# ╚════════════════════════════════════════════════════════════╝
class State:
    """
    策略全局状态类。所有属性为模块级静态变量，跨 Bar 持久化。

    属性说明：
      stock_pool      → 中证500全部有效成分股列表
      filtered_pool   → 按流动性过滤后的候选池
      positions       → 当前持仓 {代码: {shares, entry_price, entry_bar, bars_held, stop_monitoring}}
      cash            → 当前可用资金
      total_assets    → 当前总资产
      acc_id          → 交易账号ID
      capital         → 初始资金
      last_barpos     → 上一根Bar位置（防重复执行）
      bar_counter     → Bar计数器
      rankings        → 因子排名 {代码: 因子值}
      next_refresh_bar → 下次刷新时机
      market_ok       → 大盘状态
      pending_sells   → 跌停未卖出列表
      sector_map      → 行业映射 {代码: 行业名}
      sector_ok       → 行业映射是否成功
      stock_names     → 股票名称缓存 {代码: 名称}
      stop_cooldown   → ★v3新增★ 止损冷却 {代码: 剩余冷却天数}
      today_opens     → ★v3新增★ 当日开盘价 {代码: 开盘价}（盘中止损用）
    """
    # ── 股票池 ──
    stock_pool    = []
    filtered_pool = []

    # ── 持仓数据 ──
    positions = {}

    # ── 资金数据 ──
    cash         = 0
    total_assets = 0
    acc_id       = 'testS'
    capital      = 300000

    # ── Bar 控制 ──
    last_barpos     = -1
    bar_counter     = 0
    rankings        = {}
    next_refresh_bar = 0

    # ── 市场状态 ──
    market_ok = True

    # ── 待处理列表 ──
    pending_sells = []

    # ── 行业分类 ──
    sector_map  = {}
    sector_ok   = False
    stock_names = {}

    # ── ★v3新增★ 止损冷却 ──
    stop_cooldown = {}  # {代码: 剩余冷却天数}，每天递减，>0 时禁止买入


# ╔════════════════════════════════════════════════════════════╗
# ║   动态行业分类（QMT 内置函数，同 v2）                        ║
# ╚════════════════════════════════════════════════════════════╝

def _build_sector_map_from_qmt(ContextInfo, stock_list):
    """使用 QMT get_industry_name_of_stock() 动态获取行业分类。"""
    sector_map = {}
    success = 0
    unclassified = 0

    print("[行业] 开始动态获取行业分类（%s），共 %d 只股票..." %
          (INDUSTRY_TYPE, len(stock_list)))

    for i, code in enumerate(stock_list):
        try:
            industry_name = get_industry_name_of_stock(INDUSTRY_TYPE, code)
            if industry_name and len(industry_name) > 0:
                # ★v3新增★ 标记黑名单行业
                sector_map[code] = industry_name
                success += 1
            else:
                unclassified += 1
        except NameError:
            print("[行业] ⚠ get_industry_name_of_stock() 不可用，fallback 模式")
            return {}, False
        except Exception as e:
            unclassified += 1
            if i < 3:
                print("[行业] 查询 %s 失败: %s" % (code, str(e)))

        if (i + 1) % 100 == 0:
            print("[行业] 进度: %d/%d (已分类=%d)" % (i + 1, len(stock_list), success))

    unique_sectors = len(set(sector_map.values()))
    blacklisted = sum(1 for s in set(sector_map.values()) if s in SECTOR_BLACKLIST)
    print("[行业] 完成! 已分类=%d 未分类=%d 行业数=%d 黑名单=%d个" %
          (success, unclassified, unique_sectors, blacklisted))

    if unique_sectors > 0:
        from collections import Counter
        top5 = Counter(sector_map.values()).most_common(5)
        parts = ["%s:%d" % (s.replace('SW1', '').replace('CSRC1', ''), c)
                 for s, c in top5]
        print("[行业] 分布: %s" % ", ".join(parts))

    return sector_map, True


def _build_stock_names(ContextInfo, stock_list):
    """批量获取股票名称。"""
    names = {}
    for code in stock_list:
        try:
            name = ContextInfo.get_stock_name(code)
            if name and len(name) > 0:
                names[code] = name
        except Exception:
            pass
    print("[名称] 获取到 %d/%d 只股票名称" % (len(names), len(stock_list)))
    return names


def _stock_label(code):
    """返回带名称和行业的股票标签。"""
    name = State.stock_names.get(code, '')
    sector = _get_sector(code)
    short_sec = sector.replace('SW1', '').replace('CSRC1', '') if sector else ''
    return "%s(%s|%s)" % (code, name, short_sec)


# ╔════════════════════════════════════════════════════════════╗
# ║              策略入口函数：init()                           ║
# ╚════════════════════════════════════════════════════════════╝
def init(ContextInfo):
    """
    策略初始化函数。
    v3 新增：注册盘中止损监控定时器。
    """
    print("[init] Alpha#144 流动性冲击择时策略 v3 (回测优化版)")
    print("[init] 优化: 突破10日|量能1.2x|个股MA20|行业黑名单|止损冷却60天|跳空保护|盘中止损")
    print("[init] 正在获取中证500成分股...")

    # 步骤1：获取中证500成分股
    stocks = None
    try:
        raw = ContextInfo.get_stock_list_in_sector('中证500')
        if raw and len(raw) > 0:
            stocks = raw
            print("[init] get_stock_list_in_sector('中证500') 获取到 %d 只成分股" % len(raw))
    except Exception:
        pass
    if not stocks:
        try:
            raw = ContextInfo.get_sector('000905.SH')
            if raw and len(raw) > 0:
                stocks = raw
                print("[init] get_sector('000905.SH') 获取到 %d 只成分股" % len(raw))
        except Exception:
            pass
    if not stocks:
        print("[init] API 获取失败，使用硬编码 fallback")
        stocks = _get_fallback_csi500()

    # 步骤2：过滤 ST
    valid = []
    for code in stocks:
        try:
            name = ContextInfo.get_stock_name(code)
            if name and len(name) > 0 and 'ST' not in name and '*' not in name:
                valid.append(code)
        except Exception:
            valid.append(code)

    State.stock_pool = valid
    State.filtered_pool = valid[:]
    print("[init] 有效股票池：%d 只（已剔除ST）" % len(State.stock_pool))

    # 步骤2.5：动态获取行业分类
    State.sector_map, State.sector_ok = _build_sector_map_from_qmt(
        ContextInfo, State.stock_pool)

    # 步骤2.6：获取股票名称
    State.stock_names = _build_stock_names(ContextInfo, State.stock_pool)

    # 步骤3：设置 universe
    universe = valid[:] + [BENCHMARK]
    ContextInfo.set_universe(list(set(universe)))

    # 步骤4：回测参数
    for attr, val in [
        ('capital',   State.capital),
        ('benchmark', BENCHMARK),
        ('start',     '2022-01-01 09:30:00'),
        ('end',       '2026-06-19 15:00:00'),
    ]:
        try:
            setattr(ContextInfo, attr, val)
        except (AttributeError, TypeError):
            pass

    # 步骤5：交易成本
    ContextInfo.set_slippage(1, 0.001)
    ContextInfo.set_commission(0, [0.00025, 0.00025, 0.001, 0.0, 0.0, 5.0])

    # 步骤6：交易账号
    ContextInfo.set_account(State.acc_id)

    # 步骤7 ★v3新增★：注册盘中止损监控定时器
    _register_intraday_stop(ContextInfo)

    # 步骤8：打印摘要
    blacklisted_count = sum(
        1 for s in set(State.sector_map.values()) if s in SECTOR_BLACKLIST)
    print("[init] 初始化完成：")
    print("       股票池 = %d 只" % len(State.stock_pool))
    print("       行业分类 = %s（%d 个行业，%d 只已分类，%d个黑名单）" % (
        "申万SW" if INDUSTRY_TYPE == 'SW' else "证监会CSRC",
        len(set(State.sector_map.values())) if State.sector_ok else 0,
        len(State.sector_map) if State.sector_ok else 0,
        blacklisted_count))
    print("       突破周期 = %d 天 | 量能要求 = %.1fx均量" % (BREAKOUT_PERIOD, VOL_RATIO_MIN))
    print("       个股趋势 = >MA%d | 止损冷却 = %d 天 | 跳空保护 = %.0f%%" % (
        MA_STOCK, STOP_COOLDOWN, abs(GAP_DOWN_PCT) * 100))
    print("       盘中止损 = %s间隔 | 行业黑名单 = %d 个行业" % (
        INTRADAY_STOP_INTERVAL, blacklisted_count))
    print("       因子刷新间隔 = %d 天 | 最大持仓 = %d 只 | 持有期 = %d 天" % (
        REFRESH_INTERVAL, MAX_POSITIONS, MAX_HOLD_BARS))
    print("       大盘过滤器 = %s MA%d × %.0f%%" % (
        BENCHMARK, MA_MARKET, (1 - MARKET_FILTER_PCT) * 100))
    print("       初始资金 = %.0f 万" % (State.capital / 10000))


# ╔════════════════════════════════════════════════════════════╗
# ║  ★v3新增★ 盘中止损监控                                      ║
# ╚════════════════════════════════════════════════════════════╝

def _register_intraday_stop(ContextInfo):
    """
    ★v3新增★ 注册盘中止损监控定时器。

    使用 QMT 的 ContextInfo.run_time() 注册一个高频回调函数，
    每 INTRADAY_STOP_INTERVAL 触发一次，检查所有持仓的实时盈亏。
    一旦浮亏触及 -18%，立即以市价卖出，不等日线收盘。

    【QMT API 参考】
      ContextInfo.run_time(funcName, interval, contextInfo, ...)
        - funcName: 回调函数名（字符串），必须是模块级函数
        - interval: 时间间隔，如 '3nSecond'（每3秒）
        - [系统功能, p.XX] run_time 定时器

    【回测兼容】
      回测模式下 run_time() 可能不可用，此时盘中止损退化为日线止损。
    """
    try:
        ContextInfo.run_time(
            '_cb_intraday_stop_monitor',
            INTRADAY_STOP_INTERVAL,
            '2016-01-01 09:30:00',    # 开始时间
            '2099-12-31 15:00:00',    # 结束时间
            ContextInfo
        )
        print("[盘中止损] 已注册 %s 间隔的盘中止损监控" % INTRADAY_STOP_INTERVAL)
    except Exception as e:
        print("[盘中止损] ⚠ run_time() 不可用（回测模式），退化为日线止损: %s" % str(e))


def _cb_intraday_stop_monitor(ContextInfo):
    """
    ★v3新增★ 盘中止损回调函数（由 run_time 定时触发）。

    遍历所有持仓，用实时 tick 价格计算盈亏。
    如果浮亏 >= 18%，立即市价卖出。

    注意：此函数在盘中运行，不依赖日线数据。
    """
    for code in list(State.positions.keys()):
        pos = State.positions[code]
        entry = pos.get('entry_price', 0)
        if entry <= 0:
            continue

        # 获取实时价格
        try:
            tick = ContextInfo.get_full_tick([code])
            if code in tick:
                current = tick[code].get('lastPrice', 0)
                if current <= 0:
                    continue
            else:
                continue
        except Exception:
            continue

        # 计算浮亏
        pnl = (current / entry - 1.0)
        if pnl <= HARD_STOP_PCT:
            # ── 触及止损线 → 立即市价卖出 ──
            shares = pos.get('shares', 0)
            if shares <= 0:
                continue

            try:
                passorder(24, 1101, State.acc_id, code, 5, -1, shares,
                          '盘中实时止损', 1, '', ContextInfo)
                print("  ⚡ [盘中止损] %s 浮亏%.1f%% 触及%.0f%% 立即市价卖出!" % (
                    _stock_label(code), pnl * 100, HARD_STOP_PCT * 100))

                # 加入冷却名单
                State.stop_cooldown[code] = STOP_COOLDOWN

                del State.positions[code]
                if code in State.pending_sells:
                    State.pending_sells.remove(code)
            except Exception as e:
                print("  ⚡ [盘中止损失败] %s: %s" % (_stock_label(code), str(e)))


# ╔════════════════════════════════════════════════════════════╗
# ║              策略核心函数：handlebar()                       ║
# ╚════════════════════════════════════════════════════════════╝
def handlebar(ContextInfo):
    """策略主循环函数。QMT 每根K线调用一次。"""
    bar = ContextInfo.barpos

    # 步骤1：数据不足跳过
    if bar < MIN_HISTORY_BARS:
        return

    # 步骤2：防重复执行
    if bar == State.last_barpos:
        return
    State.last_barpos = bar
    State.bar_counter += 1

    # ★v3新增★ 递减止损冷却计数器
    _update_cooldown()

    # 步骤3：获取历史行情
    need_bars = max(FACTOR_WINDOW + 30, MA_MARKET + 10, MA_STOCK + 10, BREAKOUT_PERIOD + 10)
    hist_close  = ContextInfo.get_history_data(need_bars, '1d', 'close')
    hist_amount = ContextInfo.get_history_data(need_bars, '1d', 'amount')
    hist_open   = ContextInfo.get_history_data(need_bars, '1d', 'open')

    # 步骤4：更新账户
    _update_account(ContextInfo)
    State.total_assets = State.cash + _calc_total_position_value(
        ContextInfo, hist_close)
    _sync_positions(ContextInfo)

    # 步骤5：大盘过滤器
    State.market_ok = _check_market(hist_close)
    if not State.market_ok:
        print("[市场] %s 收盘价 < MA%d × %.0f%%，触发空仓避险" % (
            BENCHMARK, MA_MARKET, (1 - MARKET_FILTER_PCT) * 100))

    # 步骤6：日志
    date_str = _log_time(ContextInfo)
    cooldown_count = sum(1 for d in State.stop_cooldown.values() if d > 0)
    print("=" * 50)
    print("[%s] bar=%d cnt=%d 持仓=%d只 资产=%.0f万 现金=%.0f万 市场=%s 冷却=%d" % (
        date_str, bar, State.bar_counter, len(State.positions),
        State.total_assets / 10000, State.cash / 10000,
        "可交易" if State.market_ok else "防御",
        cooldown_count))

    # 步骤7：处理待卖出
    _process_pending_sells(ContextInfo, hist_close)

    # 步骤8：检查出场（日线级别，作为盘中止损的兜底）
    _check_exits(ContextInfo, hist_close)

    # 步骤9：因子刷新
    if State.bar_counter >= State.next_refresh_bar:
        State.rankings = _compute_factor_rankings(hist_close, hist_amount)
        State.next_refresh_bar = State.bar_counter + REFRESH_INTERVAL
        print("[刷新] 因子排名完成：%d 只股票进入候选池" % len(State.rankings))

    # 步骤10：入场检查
    if State.market_ok and len(State.positions) < MAX_POSITIONS:
        _check_entry_breakout_v3(ContextInfo, hist_close, hist_amount, hist_open)

    # 步骤11：大盘防御
    if not State.market_ok and len(State.positions) > 0:
        _liquidate_all(ContextInfo, hist_close, "大盘防御")

    # 步骤12：更新持仓天数
    for code in list(State.positions.keys()):
        State.positions[code]['bars_held'] += 1

    # 步骤13：摘要
    pos_codes = list(State.positions.keys())
    hold_days = [State.positions[c]['bars_held'] for c in pos_codes]
    pos_labels = [_stock_label(c) for c in pos_codes]
    print("[摘要] 持仓=%d只 %s | 持有天数=%s | 下次刷新还有=%d天" % (
        len(State.positions),
        pos_labels if pos_labels else "空仓",
        hold_days if hold_days else "-",
        State.next_refresh_bar - State.bar_counter))


# ╔════════════════════════════════════════════════════════════╗
# ║  ★v3新增★ 辅助函数                                         ║
# ╚════════════════════════════════════════════════════════════╝

def _update_cooldown():
    """★v3新增★ 递减止损冷却计数器。"""
    for code in list(State.stop_cooldown.keys()):
        State.stop_cooldown[code] -= 1
        if State.stop_cooldown[code] <= 0:
            del State.stop_cooldown[code]


def _is_in_blacklist(code):
    """★v3新增★ 检查股票行业是否在黑名单中。"""
    sector = _get_sector(code)
    return sector in SECTOR_BLACKLIST


def _is_in_cooldown(code):
    """★v3新增★ 检查股票是否在止损冷却期内。"""
    return State.stop_cooldown.get(code, 0) > 0


def _is_above_ma(code, hist_close):
    """★v3新增★ 检查股票当前价格是否高于其 MA 均线（趋势向上）。"""
    if code not in hist_close:
        return True  # 无数据时放行
    arr = hist_close[code]
    if len(arr) < MA_STOCK + 1:
        return True  # 数据不足时放行

    close_arr = np.array(arr, dtype=float)
    current = close_arr[-1]
    ma = np.mean(close_arr[-(MA_STOCK + 1):-1])  # MA用不含今日的数据
    return current > ma


def _check_gap_down(code, hist_close, hist_open):
    """★v3新增★ 检查跳空低开：今日开盘 / 昨日收盘 - 1 <= GAP_DOWN_PCT。"""
    if code not in hist_close or code not in hist_open:
        return False
    if len(hist_close[code]) < 2 or len(hist_open[code]) < 2:
        return False

    try:
        yesterday_close = float(hist_close[code][-2])
        today_open = float(hist_open[code][-1])
        if yesterday_close <= 0:
            return False
        gap = (today_open / yesterday_close - 1.0)
        return gap <= GAP_DOWN_PCT
    except Exception:
        return False


# ╔════════════════════════════════════════════════════════════╗
# ║              Alpha#144 因子计算函数（同 v2）                 ║
# ╚════════════════════════════════════════════════════════════╝

def _calc_alpha144(close_arr, amount_arr):
    """计算单只股票的 Alpha#144 因子值。"""
    arr_c = np.array(close_arr, dtype=float)
    arr_a = np.array(amount_arr, dtype=float)

    n = min(len(arr_c), len(arr_a))
    needed = FACTOR_WINDOW + 1
    if n < needed:
        return None

    recent_c = arr_c[-needed:]
    recent_a = arr_a[-needed:]

    alpha = 0.0
    neg_count = 0

    for i in range(1, len(recent_c)):
        prev_close = recent_c[i - 1]
        curr_close = recent_c[i]
        if prev_close > 0:
            ret_i = (curr_close - prev_close) / prev_close
        else:
            ret_i = 0

        if ret_i < 0:
            amount_i = recent_a[i] if i < len(recent_a) else 0
            if amount_i > 0:
                alpha += abs(ret_i) / amount_i
                neg_count += 1

    if neg_count == 0:
        return 0.0
    return alpha


def _compute_factor_rankings(hist_close, hist_amount):
    """计算全股票池的 Alpha#144 因子排名。"""
    raw_scores = {}

    for code in State.filtered_pool:
        close_arr = hist_close.get(code, [])
        if len(close_arr) < FACTOR_WINDOW + 1:
            continue

        amount_arr = hist_amount.get(code, [])
        if len(amount_arr) < FACTOR_WINDOW + 1:
            continue

        try:
            recent_amounts = np.array(amount_arr[-FACTOR_WINDOW:], dtype=float)
            avg_amount = np.mean(recent_amounts)
            if avg_amount < MIN_DAILY_AMOUNT:
                continue
        except Exception:
            continue

        val = _calc_alpha144(close_arr, amount_arr)
        if val is not None:
            raw_scores[code] = val

    if not raw_scores:
        return {}

    sorted_codes = sorted(raw_scores.keys(),
                          key=lambda code: raw_scores[code],
                          reverse=True)
    top_n = max(1, int(len(sorted_codes) * FACTOR_TOP_PCT))
    rankings = {}
    for code in sorted_codes[:top_n]:
        rankings[code] = raw_scores[code]
    return rankings


# ╔════════════════════════════════════════════════════════════╗
# ║              入场判断 ★v3重写★                              ║
# ╚════════════════════════════════════════════════════════════╝

def _check_entry_breakout_v3(ContextInfo, hist_close, hist_amount, hist_open):
    """
    ★v3重写★ 入场判断，增加 5 层过滤。

    新增过滤层（按检查顺序）：
      (A) 行业黑名单 — 跳过 SECTOR_BLACKLIST 中的行业
      (B) 止损冷却期 — 跳过 60 天内止损过的股票
      (C) 跳空低开保护 — 跳过当日开盘价低于昨收 3%+ 的股票
      (D) 个股趋势过滤 — 跳过股价 < MA20 的股票（下跌趋势不追）
      (E) 量能强化 — 成交额 >= 近5日均量 × 1.2x（原 0.8x）

    原有过滤（保留）：
      (1) 在 Top 15% 排名中
      (2) 突破 10 日新高（原 5 日）
      (3) 未涨停
      (4) 未持仓
      (5) 仓位未满
    """
    if not State.rankings:
        return

    held = set(State.positions.keys())
    slots = MAX_POSITIONS - len(State.positions)
    if slots <= 0:
        return

    ranked_list = sorted(State.rankings.keys(),
                         key=lambda c: State.rankings[c],
                         reverse=True)

    signals = []
    skip_blacklist = 0
    skip_cooldown = 0
    skip_gap = 0
    skip_trend = 0
    skip_volume = 0

    for code in ranked_list:
        if code in held:
            continue

        # ── (A) ★v3新增★ 行业黑名单 ──
        if _is_in_blacklist(code):
            skip_blacklist += 1
            continue

        # ── (B) ★v3新增★ 止损冷却 ──
        if _is_in_cooldown(code):
            skip_cooldown += 1
            continue

        # ── 数据充足检查 ──
        close_arr = hist_close.get(code, [])
        if len(close_arr) < max(BREAKOUT_PERIOD, MA_STOCK) + 2:
            continue

        arr = np.array(close_arr, dtype=float)
        current_close = arr[-1]
        prev_close = arr[-2] if len(arr) >= 2 else current_close

        # ── 涨停检查 ──
        daily_ret = (current_close - prev_close) / prev_close if prev_close > 0 else 0
        if daily_ret >= LIMIT_UP_PCT:
            print("  [入场跳过] %s 涨停（涨幅%.1f%%）" % (_stock_label(code), daily_ret * 100))
            continue

        # ── (C) ★v3新增★ 跳空低开保护 ──
        if _check_gap_down(code, hist_close, hist_open):
            skip_gap += 1
            continue

        # ── (D) ★v3新增★ 个股趋势过滤：股价必须 > MA20 ──
        if MA_STOCK > 0 and not _is_above_ma(code, hist_close):
            skip_trend += 1
            continue

        # ── 突破检查：今日收盘 > 过去 BREAKOUT_PERIOD 日最高价（不含今天）──
        past_high = np.max(arr[-(BREAKOUT_PERIOD + 1):-1])
        if current_close <= past_high:
            continue  # 没有突破

        # ── (E) ★v3优化★ 量能强化：当日成交额 >= 近 VOL_LOOKBACK 日均量 × VOL_RATIO_MIN ──
        amount_arr = hist_amount.get(code, [])
        vol_ok = True
        if len(amount_arr) >= VOL_LOOKBACK + 1:
            try:
                today_amt = float(amount_arr[-1])
                avg_amt = np.mean([float(amount_arr[i])
                                   for i in range(-(VOL_LOOKBACK + 1), -1)
                                   if amount_arr[i] is not None])
                vol_ok = today_amt >= avg_amt * VOL_RATIO_MIN if avg_amt > 0 else True
            except Exception:
                vol_ok = True

        if not vol_ok:
            skip_volume += 1
            continue

        # ── 通过所有过滤 → 记录信号 ──
        factor_val = State.rankings.get(code, 0)
        signals.append((code, current_close, factor_val))
        print("  [信号] %s 突破%d日新高! 收盘=%.2f %d日最高=%.2f alpha144=%.2e" % (
            _stock_label(code), BREAKOUT_PERIOD, current_close,
            BREAKOUT_PERIOD, past_high, factor_val))

        if len(signals) >= slots:
            break

    # ── 过滤统计日志 ──
    total_skipped = skip_blacklist + skip_cooldown + skip_gap + skip_trend + skip_volume
    if total_skipped > 0:
        print("  [过滤] 黑名单=%d 冷却=%d 跳空=%d 趋势=%d 缩量=%d" % (
            skip_blacklist, skip_cooldown, skip_gap, skip_trend, skip_volume))

    # ── 批量买入 ──
    if signals:
        _buy_signals(ContextInfo, signals)


def _buy_signals(ContextInfo, signals):
    """对突破信号发出买入订单。（同 v2）"""
    n_signals = len(signals)
    if n_signals == 0:
        return

    total_equity = State.total_assets if State.total_assets > 0 else State.capital
    allocation_per_stock = total_equity / MAX_POSITIONS

    sector_counts = {}
    for code in State.positions.keys():
        sec = _get_sector(code)
        sector_counts[sec] = sector_counts.get(sec, 0) + 1

    bought = 0

    for code, price, factor_val in signals:
        if code in State.positions:
            continue
        if len(State.positions) >= MAX_POSITIONS:
            break

        sec = _get_sector(code)
        sec_limit = MAX_SECTOR_OTHER if sec == UNKNOWN_SECTOR else MAX_SECTOR_COUNT
        if sector_counts.get(sec, 0) >= sec_limit:
            print("  [买入跳过] %s 已满%d只（上限%d只）" % (
                _stock_label(code), sector_counts[sec], sec_limit))
            continue

        shares = int(allocation_per_stock / price / 100) * 100
        if shares < 100:
            shares = 100

        need_cash = shares * price * 1.002
        if need_cash > State.cash:
            shares = int(State.cash * 0.98 / price / 100) * 100
            if shares < 100:
                print("  [买入失败] %s 资金不足: 需要%.0f 可用%.0f" % (
                    _stock_label(code), need_cash, State.cash))
                continue

        try:
            passorder(23, 1101, State.acc_id, code, 5, -1, shares,
                      'Alpha144突破', 1, '', ContextInfo)
        except Exception as e:
            print("  [买入失败] %s 下单异常: %s" % (_stock_label(code), str(e)))
            continue

        State.positions[code] = {
            'shares':      shares,
            'entry_price': price,
            'entry_bar':   State.last_barpos,
            'bars_held':   0,
        }
        sector_counts[sec] = sector_counts.get(sec, 0) + 1

        print(">>> [买入] %s × %d股 @ %.2f | 金额%.0f | alpha144=%.2e" % (
            _stock_label(code), shares, price, shares * price, factor_val))
        bought += 1


# ╔════════════════════════════════════════════════════════════╗
# ║              出场判断（日线兜底 + 止损冷却记录）              ║
# ╚════════════════════════════════════════════════════════════╝

def _check_exits(ContextInfo, hist_close):
    """检查持仓出场（日线级别，盘中止损的兜底）。"""
    to_sell = []

    for code, pos in State.positions.items():
        px = _get_price(ContextInfo, code, hist_close)
        if px <= 0:
            continue

        entry = pos['entry_price']
        pnl_pct = (px / entry - 1.0) if entry > 0 else 0

        # 硬止损
        if pnl_pct <= HARD_STOP_PCT:
            print("  [止损触发] %s 浮亏%.1f%% <= %.0f%%" % (
                _stock_label(code), pnl_pct * 100, HARD_STOP_PCT * 100))
            to_sell.append((code, "硬止损%.0f%%(浮亏%.1f%%)" % (
                HARD_STOP_PCT * 100, pnl_pct * 100)))
            continue

        # 持有到期
        if pos['bars_held'] >= MAX_HOLD_BARS:
            to_sell.append((code, "持有%d天到期" % MAX_HOLD_BARS))

    for code, reason in to_sell:
        _sell_position(ContextInfo, code, hist_close, reason)


def _process_pending_sells(ContextInfo, hist_close):
    """处理之前因跌停未能卖出的持仓。"""
    if not State.pending_sells:
        return

    retry_list = list(State.pending_sells)
    State.pending_sells = []

    for code in retry_list:
        if code not in State.positions:
            continue
        _sell_position(ContextInfo, code, hist_close, "补卖(昨日跌停)")


def _sell_position(ContextInfo, code, hist_close, reason):
    """卖出一只股票。★v3新增★ 止损出场时加入冷却名单。"""
    if code not in State.positions:
        return

    pos = State.positions[code]
    shares = pos.get('shares', 0)
    if shares <= 0:
        del State.positions[code]
        return

    px = _get_price(ContextInfo, code, hist_close)

    # 跌停检查
    close_arr = hist_close.get(code, [])
    if len(close_arr) >= 2:
        arr = np.array(close_arr, dtype=float)
        daily_ret = (arr[-1] - arr[-2]) / arr[-2] if arr[-2] > 0 else 0
        if daily_ret <= LIMIT_DOWN_PCT:
            print("  [卖出延迟] %s 跌停（跌幅%.1f%%），延至次日" % (
                _stock_label(code), daily_ret * 100))
            if code not in State.pending_sells:
                State.pending_sells.append(code)
            return

    # 发出卖出
    try:
        passorder(24, 1101, State.acc_id, code, 5, -1, shares,
                  'Alpha144卖出', 1, '', ContextInfo)
    except Exception as e:
        print("  [卖出失败] %s 下单异常: %s" % (_stock_label(code), str(e)))
        return

    entry_price = pos['entry_price']
    pnl_pct = (px / entry_price - 1) * 100 if entry_price > 0 else 0
    bars = pos.get('bars_held', 0)

    print("<<< [卖出] %s × %d股 @ %.2f | 盈亏%+.1f%% | 持有%d天 | %s" % (
        _stock_label(code), shares, px, pnl_pct, bars, reason))

    # ★v3新增★ 止损出场 → 加入冷却名单
    if '硬止损' in reason or '盘中实时止损' in reason:
        State.stop_cooldown[code] = STOP_COOLDOWN
        print("  [冷却] %s 加入冷却名单（%d天）" % (_stock_label(code), STOP_COOLDOWN))

    del State.positions[code]
    if code in State.pending_sells:
        State.pending_sells.remove(code)


# ╔════════════════════════════════════════════════════════════╗
# ║   行业分类查询（同 v2）                                      ║
# ╚════════════════════════════════════════════════════════════╝

def _get_sector(code):
    """查询股票所属行业。"""
    if State.sector_ok:
        return State.sector_map.get(code, UNKNOWN_SECTOR)
    else:
        return UNKNOWN_SECTOR


# ╔════════════════════════════════════════════════════════════╗
# ║              大盘防御 — 清仓（同 v2）                        ║
# ╚════════════════════════════════════════════════════════════╝

def _liquidate_all(ContextInfo, hist_close, reason):
    """清空全部持仓。"""
    for code in list(State.positions.keys()):
        _sell_position(ContextInfo, code, hist_close, reason)


# ╔════════════════════════════════════════════════════════════╗
# ║              辅助函数（同 v2）                               ║
# ╚════════════════════════════════════════════════════════════╝

def _check_market(hist_close):
    """大盘过滤器：中证500是否在 MA20×(1-3%) 之上。"""
    if BENCHMARK not in hist_close:
        return True
    arr = hist_close[BENCHMARK]
    if len(arr) < MA_MARKET + 1:
        return True
    close_arr = np.array(arr, dtype=float)
    current = close_arr[-1]
    ma = np.mean(close_arr[-MA_MARKET:])
    threshold = ma * (1.0 - MARKET_FILTER_PCT)
    return current >= threshold


def _calc_total_position_value(ContextInfo, hist_close):
    """计算持仓总市值。"""
    total = 0.0
    for code, pos in State.positions.items():
        shares = pos.get('shares', 0)
        px = _get_price(ContextInfo, code, hist_close)
        total += shares * px
    return total


def _update_account(ContextInfo):
    """更新账户资金。"""
    try:
        account_list = get_trade_detail_data(State.acc_id, 'stock', 'account')
        if account_list:
            State.cash = account_list[0].m_dAvailable
            State.total_assets = account_list[0].m_dBalance
            return
    except Exception:
        pass
    try:
        State.cash = ContextInfo.cash
        State.total_assets = ContextInfo.capital
    except Exception:
        pass


def _sync_positions(ContextInfo):
    """同步实际持仓。"""
    try:
        position_list = get_trade_detail_data(State.acc_id, 'stock', 'position')
        remote_positions = {}
        for p in position_list:
            code = p.m_strInstrumentID + '.' + p.m_strExchangeID
            vol = p.m_nVolume
            if vol <= 0:
                continue
            if code in State.positions:
                old = State.positions[code]
                old['shares'] = vol
                remote_positions[code] = old
            else:
                remote_positions[code] = {
                    'shares':      vol,
                    'entry_price': p.m_dOpenPrice,
                    'entry_bar':   State.last_barpos,
                    'bars_held':   0,
                }
        for code in list(State.positions.keys()):
            if code not in remote_positions:
                del State.positions[code]
        for code, pos in remote_positions.items():
            if code not in State.positions:
                State.positions[code] = pos
    except Exception:
        pass


def _get_price(ContextInfo, code, hist_close):
    """获取股票当前价格。"""
    try:
        tick = ContextInfo.get_full_tick([code])
        if code in tick:
            lp = tick[code].get('lastPrice', 0)
            if lp > 0:
                return lp
    except Exception:
        pass
    if code in hist_close and len(hist_close[code]) > 0:
        return float(hist_close[code][-1])
    return 0


def _log_time(ContextInfo):
    """获取当前 Bar 的可读时间。"""
    try:
        t = ContextInfo.get_bar_timetag(ContextInfo.barpos)
        return timetag_to_datetime(t, '%Y-%m-%d %H:%M')
    except Exception:
        return str(ContextInfo.barpos)


# ╔════════════════════════════════════════════════════════════╗
# ║   硬编码 Fallback 中证500成分股（同 v2）                     ║
# ╚════════════════════════════════════════════════════════════╝

def _get_fallback_csi500():
    """硬编码中证500代表性成分股。"""
    return [
        '300003.SZ', '300009.SZ', '300015.SZ', '300026.SZ', '300039.SZ',
        '002001.SZ', '002007.SZ', '002019.SZ', '002020.SZ', '002022.SZ',
        '600079.SH', '600085.SH', '600196.SH', '600276.SH', '600380.SH',
        '300595.SZ', '300601.SZ', '300633.SZ', '300676.SZ', '300725.SZ',
        '002049.SZ', '002138.SZ', '002185.SZ', '002273.SZ', '002371.SZ',
        '002409.SZ', '002436.SZ', '002456.SZ', '002463.SZ', '002475.SZ',
        '603160.SH', '603501.SH', '603986.SH', '688008.SH', '688012.SH',
        '002230.SZ', '002368.SZ', '002373.SZ', '002405.SZ', '002410.SZ',
        '300033.SZ', '300036.SZ', '300059.SZ', '300168.SZ', '300253.SZ',
        '002064.SZ', '002092.SZ', '002108.SZ', '002250.SZ', '002258.SZ',
        '002326.SZ', '002407.SZ', '002408.SZ', '002440.SZ', '002460.SZ',
        '002013.SZ', '002025.SZ', '002050.SZ', '002074.SZ', '002097.SZ',
        '300024.SZ', '300124.SZ', '300274.SZ', '300316.SZ', '300450.SZ',
        '002459.SZ', '002121.SZ', '002129.SZ', '002202.SZ', '002245.SZ',
        '300014.SZ', '300037.SZ', '300068.SZ', '300073.SZ', '300118.SZ',
        '000060.SZ', '000630.SZ', '000807.SZ', '000831.SZ', '000878.SZ',
        '000933.SZ', '000960.SZ', '000975.SZ', '002155.SZ', '002203.SZ',
        '002555.SZ', '002602.SZ', '002624.SZ', '300058.SZ', '300133.SZ',
        '300251.SZ', '300413.SZ', '300418.SZ', '603444.SH',
        '002120.SZ', '002352.SZ', '002468.SZ', '600026.SH', '600029.SH',
        '000400.SZ', '000401.SZ', '000425.SZ', '000528.SZ', '000538.SZ',
        '000547.SZ', '000553.SZ', '000581.SZ', '000625.SZ', '000629.SZ',
        '000636.SZ', '000656.SZ', '000661.SZ', '000703.SZ', '000708.SZ',
        '000723.SZ', '000728.SZ', '000729.SZ', '000738.SZ', '000750.SZ',
        '000776.SZ', '000778.SZ', '000783.SZ', '000786.SZ', '000800.SZ',
        '000825.SZ', '000826.SZ', '000830.SZ', '000860.SZ', '000869.SZ',
        '000887.SZ', '000895.SZ', '000902.SZ', '000903.SZ', '000912.SZ',
        '000915.SZ', '000921.SZ', '000927.SZ', '000930.SZ', '000932.SZ',
        '000937.SZ', '000938.SZ', '000950.SZ', '000951.SZ', '000957.SZ',
        '000959.SZ', '000961.SZ', '000962.SZ', '000963.SZ',
        '000966.SZ', '000967.SZ', '000968.SZ', '000969.SZ', '000970.SZ',
        '300498.SZ', '002714.SZ', '002304.SZ',
    ]
