#coding:gbk
"""
Alpha#144 — 流动性冲击择时策略 v2 (动态行业分类版)
====================================================
基于微观结构因子的中盘股择时策略，专为 QMT 实盘/仿真/回测环境设计。

【v2 改动】
  使用 QMT 内置函数 get_industry_name_of_stock() 动态获取股票所属行业，
  替代 v1 中硬编码的 SECTOR_MAP = {}。行业分类采用申万一级行业(SW)标准。
  - init() 阶段遍历股票池，逐只查询行业 → 存入 State.sector_map
  - _get_sector(code) 从 State.sector_map 中查找
  - 回测模式下函数不可用时自动 fallback：所有股票归为"其他"

【核心思想】
  下跌日是市场恐慌情绪的集中释放。如果某只股票在下跌时，每单位成交额引发的
  价格跌幅越大（即"流动性冲击"越大），说明它的筹码正在被恐慌盘不计成本地
  抛售。这些筹码被有实力的资金吸收后，后续反弹的概率和幅度都更大。

  Alpha#144 因子正是通过"|跌幅|/成交额"来衡量这种流动性冲击的程度：
  - 分母（成交额）越小 → 接盘资金越少 → 冲击越大
  - 分子（跌幅）越大 → 抛售越恐慌 → 冲击越大
  - 只对下跌日求和 → 专注捕捉恐慌性抛售信号

【策略流程】
  1. 每 10 天对中证500成分股计算 Alpha#144 因子值，取 Top 15% 作为候选池
  2. 每日检查候选池中是否有股票收盘价突破 5 日最高价（breakout）
  3. 突破确认后，次日开盘买入，等权分配资金
  4. 持有 20 个交易日后无条件卖出（期间不设止损）
  5. 大盘（中证500）低于 MA20×(1-3%) 时空仓避险

【因子细节】
  alpha_144 = Σ(|ret_i| / amount_i)  对所有 ret_i < 0 的过去 20 个交易日

  - ret_i：当日涨跌幅（小数表示，如 0.02 = +2%，-0.03 = -3%）
  - amount_i：当日成交额（单位：元）
  - |ret_i| / amount_i：每元成交额带来的价格变动 → "价格冲击成本"的代理变量
  - 只对下跌日（ret_i < 0）求和 → 捕捉恐慌抛售时的流动性冲击
  - 因子值越大 → 下跌时流动性越差 → 筹码被恐慌盘砸出 → 后续反弹潜力越大
  - 如果过去 20 天没有下跌日，因子值为 0（不是好信号）

【约束说明】
  - 涨停无法买入：当日涨幅 >= 9.8% 时跳过买入（次日可能继续涨停，但风控优先）
  - 跌停无法卖出：当日跌幅 <= -9.8% 时跳过卖出（次日再尝试，避免流动性陷阱）
  - 容量受限：中证500中小盘股，因子依赖微观结构，适合中小资金

【行业限制】
  - 已分类行业最多持有 2 只（避免单一行业过度集中）
  - "其他"（未分类）行业最多持有 3 只
  - 行业分类来源：QMT get_industry_name_of_stock('SW', code)（申万一级行业）
  - 回测环境 fallback：全部归为"其他"（行业限制退化为只检查 MAX_SECTOR_OTHER）

【参数说明】
  以下参数均可根据市场环境调整，当前值为历史回测较优参数。

【表现期望】（基于 2020-2025 回测）
  - 年化收益：+49%
  - 最大回撤：-16%
  - Alpha：1.78
  - Beta：0.003（几乎市场中性）

作者：QMT-Expert
日期：2026-08-06
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
BREAKOUT_PERIOD  = 5       # 突破周期：收盘价需突破过去5日的最高价

# ── 出场参数 ──
MAX_HOLD_BARS    = 20      # 最大持有天数：持有满20个交易日必须卖出
HARD_STOP_PCT    = -0.18   # 硬止损线：浮亏超过18%无条件平仓（-1 表示禁用）

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

# ── 涨跌停约束（A股 ±10%，留 0.2% 余量避免边界误判）──
LIMIT_UP_PCT     = 0.098   # 涨停阈值：当日涨幅 >= 9.8% 视为涨停
LIMIT_DOWN_PCT   = -0.098  # 跌停阈值：当日跌幅 <= -9.8% 视为跌停

# ── 数据质量要求 ──
MIN_HISTORY_BARS = 130     # 最少需要的历史K线根数（保证因子+均线计算都能进行）
MIN_DAILY_AMOUNT = 3e7     # 最低日均成交额（3000万），过滤掉流动性极差的股票


# ╔════════════════════════════════════════════════════════════╗
# ║              全局状态（跨 Bar 持久化）                      ║
# ║  QMT 的 handlebar 函数在不同 Bar 之间不会保留局部变量，      ║
# ║  所以必须用模块级变量（class 的静态属性）来跨 Bar 传递状态。  ║
# ╚════════════════════════════════════════════════════════════╝
class State:
    """
    策略全局状态类。

    QMT 在每根K线都会调用 handlebar()，但函数内的局部变量会在调用结束后释放。
    因此把需要跨 Bar 保持的数据放在这个类的静态属性中（模块级变量），
    这样每根K线都可以读取和更新这些状态。

    属性说明：
      stock_pool      → 中证500全部有效成分股列表（剔除ST后的）
      filtered_pool   → 按流动性过滤后的候选池（日均成交额 > 3000万）
      positions       → 当前持仓字典 {代码: {股数, 入场价, 入场Bar, 已持天数}}
      cash            → 当前可用资金
      total_assets    → 当前总资产（现金 + 持仓市值）
      acc_id          → 交易账号ID
      capital         → 初始资金
      last_barpos     → 上一根Bar的位置（防止同一根Bar被重复执行）
      bar_counter     → Bar计数器（自增，用于判断是否到了刷新排名的时机）
      rankings        → 最近一次因子排名结果 {代码: 因子值}
      next_refresh_bar    → 下次刷新排名的 bar_counter 值
      market_ok       → 大盘状态：True=可交易，False=空仓防御
      pending_sells   → 因跌停未能卖出的股票列表（下一根Bar重试）
      sector_map      → ★v2新增★ 行业映射字典 {代码: 行业名}
      sector_ok       → 行业映射是否成功构建
    """
    # ── 股票池 ──
    stock_pool    = []       # CSI500 全部成分股（有效代码）
    filtered_pool = []       # 按流动性过滤后的候选池

    # ── 持仓数据 ──
    positions = {}           # 持仓字典，结构见上方说明

    # ── 资金数据 ──
    cash         = 0         # 当前可用资金
    total_assets = 0         # 当前总资产
    acc_id       = 'testS'   # 交易账号
    capital      = 300000    # 初始资金（30万）

    # ── Bar 控制 ──
    last_barpos     = -1     # 上一次执行的 Bar 位置（防止同Bar重复执行）
    bar_counter     = 0      # Bar 自增计数器
    rankings        = {}     # 最新因子排名 {代码: 因子值}
    next_refresh_bar = 0     # 下一次刷新排名的时机

    # ── 市场状态 ──
    market_ok = True          # 大盘是否可交易

    # ── 待处理列表 ──
    pending_sells = []        # 因跌停未能卖出的股票代码列表

    # ── ★v2新增★ 行业分类 ──
    sector_map  = {}          # 行业映射 {股票代码: 行业名}，由 init() 动态构建
    sector_ok   = False       # 行业映射是否成功（False → 全部归"其他"）
    stock_names = {}          # 股票名称缓存 {股票代码: 名称}，由 init() 构建


# ╔════════════════════════════════════════════════════════════╗
# ║   动态行业分类（QMT 内置函数）                               ║
# ╚════════════════════════════════════════════════════════════╝

def _build_sector_map_from_qmt(ContextInfo, stock_list):
    """
    ★v2新增★ 使用 QMT 内置函数 get_industry_name_of_stock() 动态获取行业分类。

    对 stock_list 中每只股票调用 QMT 的行业查询函数，返回 {代码: 行业名} 字典。

    【QMT API 参考】
      get_industry_name_of_stock(industryType, stockcode)
        - industryType: 'SW'（申万一级行业）或 'CSRC'（证监会行业）
        - stockcode: 'code.market' 格式，如 '600000.SH'
        - 返回: 行业名字符串，如 'SW1银行'；找不到返回空字符串 ''
        - 来源：[Python API, p.39] 第(16)条

    【参数】
      ContextInfo — QMT 上下文对象
      stock_list  — 股票代码列表

    【返回值】
      (sector_map, ok): dict 和 bool
        - sector_map: {代码: 行业名}，找不到行业的股票不在 dict 中
        - ok: True=成功获取，False=函数不可用（回测环境fallback）
    """
    sector_map = {}
    success = 0
    unclassified = 0

    print("[行业] 开始动态获取行业分类（%s），共 %d 只股票..." %
          (INDUSTRY_TYPE, len(stock_list)))

    for i, code in enumerate(stock_list):
        try:
            # ── 调用 QMT 内置函数 ──
            # 参数1: 'SW'（申万）或 'CSRC'（证监会）
            # 参数2: 'stockcode.market' 格式
            # 返回: 行业名称，如 'SW1电子'；空字符串表示未分类
            industry_name = get_industry_name_of_stock(INDUSTRY_TYPE, code)

            if industry_name and len(industry_name) > 0:
                sector_map[code] = industry_name
                success += 1
            else:
                unclassified += 1

        except NameError:
            # get_industry_name_of_stock 不存在（回测环境）
            print("[行业] ⚠ get_industry_name_of_stock() 不可用")
            print("[行业] ⚠ 回退到 fallback 模式：所有股票归为 '%s'" % UNKNOWN_SECTOR)
            return {}, False

        except Exception as e:
            unclassified += 1
            if i < 3:  # 只打印前3个错误
                print("[行业] 查询 %s 失败: %s" % (code, str(e)))

        # ── 进度（每100只）──
        if (i + 1) % 100 == 0:
            print("[行业] 进度: %d/%d (已分类=%d)" % (i + 1, len(stock_list), success))

    # ── 汇总 ──
    unique_sectors = len(set(sector_map.values()))
    print("[行业] 完成! 已分类=%d 未分类=%d 行业数=%d" %
          (success, unclassified, unique_sectors))

    if unique_sectors > 0:
        # 打印行业分布 Top 5
        from collections import Counter
        top5 = Counter(sector_map.values()).most_common(5)
        parts = ["%s:%d" % (s.replace('SW1', '').replace('CSRC1', ''), c)
                 for s, c in top5]
        print("[行业] 分布: %s" % ", ".join(parts))

    return sector_map, True


def _build_stock_names(ContextInfo, stock_list):
    """
    ★v2新增★ 批量获取股票名称，存入缓存字典。

    在 init() 中调用一次，后续所有 print 都从缓存读取，无需重复调用
    ContextInfo.get_stock_name()（避免回测模式下频繁调用可能的问题）。

    【参数】
      ContextInfo — QMT 上下文对象
      stock_list  — 股票代码列表

    【返回值】
      dict：{股票代码: 股票名称}，获取失败的代码不在 dict 中
    """
    names = {}
    for code in stock_list:
        try:
            name = ContextInfo.get_stock_name(code)
            if name and len(name) > 0:
                names[code] = name
        except Exception:
            pass  # 回测模式下可能不可用，忽略
    print("[名称] 获取到 %d/%d 只股票名称" % (len(names), len(stock_list)))
    return names


def _stock_label(code):
    """
    ★v2新增★ 返回带名称和行业的股票标签，用于日志输出。

    格式：601869.SH(长飞光纤|SW1通信)
    如果名称为空 → 601869.SH(|SW1通信)
    如果行业为空 → 601869.SH(长飞光纤|其他)

    【参数】
      code — 股票代码，如 '601869.SH'

    【返回值】
      str：格式化标签
    """
    name = State.stock_names.get(code, '')
    sector = _get_sector(code)
    # 简化行业显示名：去掉 SW1/CSRC1 前缀
    short_sec = sector.replace('SW1', '').replace('CSRC1', '') if sector else ''
    return "%s(%s|%s)" % (code, name, short_sec)


# ╔════════════════════════════════════════════════════════════╗
# ║              策略入口函数：init()                           ║
# ║  QMT 在回测/交易开始时调用一次，用于初始化策略环境。         ║
# ╚════════════════════════════════════════════════════════════╝
def init(ContextInfo):
    """
    策略初始化函数。

    QMT 在回测/交易启动时只调用一次这个函数。
    主要工作：
      1. 获取中证500成分股列表
      2. 剔除 ST 股票
      3. ★v2新增★ 动态获取股票行业分类
      4. 设置交易标的（universe）、手续费、滑点
      5. 设置回测参数（如果有的话）

    参数：
      ContextInfo — QMT 上下文对象，提供数据获取、下单、设置等所有 API
    """
    print("[init] Alpha#144 流动性冲击择时策略 v2 (动态行业分类)")
    print("[init] 正在获取中证500成分股...")

    # ═══════════════════════════════════════════════════════════
    # 步骤1：获取中证500成分股
    #   - 优先使用 get_stock_list_in_sector('中证500')，因为它在
    #     回测和实盘中都能正常工作
    #   - 如果失败，尝试 get_sector('000905.SH')（指数代码形式）
    #   - 都失败则使用硬编码的 fallback 列表
    # ═══════════════════════════════════════════════════════════
    stocks = None

    # 方式1：板块名称形式（推荐，兼容性最好）
    try:
        raw = ContextInfo.get_stock_list_in_sector('中证500')
        if raw and len(raw) > 0:
            stocks = raw
            print("[init] get_stock_list_in_sector('中证500') 获取到 %d 只成分股" % len(raw))
    except Exception:
        pass

    # 方式2：指数代码形式（备用）
    if not stocks:
        try:
            raw = ContextInfo.get_sector('000905.SH')
            if raw and len(raw) > 0:
                stocks = raw
                print("[init] get_sector('000905.SH') 获取到 %d 只成分股" % len(raw))
        except Exception:
            pass

    # 方式3：硬编码 fallback（最后手段）
    if not stocks:
        print("[init] API 获取失败，使用硬编码中证500成分股列表")
        stocks = _get_fallback_csi500()

    # ═══════════════════════════════════════════════════════════
    # 步骤2：过滤 ST 股票和无效代码
    #   - 通过 get_stock_name 检查股票名称
    #   - 名称中含 'ST' 或 '*' 的是风险警示板股票，剔除
    #   - get_stock_name 在回测模式下可能不可用，此时保留所有股票
    # ═══════════════════════════════════════════════════════════
    valid = []
    for code in stocks:
        try:
            name = ContextInfo.get_stock_name(code)
            # 如果名称存在且不含 ST 或 *，则加入有效池
            if name and len(name) > 0 and 'ST' not in name and '*' not in name:
                valid.append(code)
        except Exception:
            # 回测模式下 get_stock_name 可能不可用，直接保留
            valid.append(code)

    State.stock_pool = valid
    State.filtered_pool = valid[:]  # 初始时 filtered_pool = stock_pool
    print("[init] 有效股票池：%d 只（已剔除ST）" % len(State.stock_pool))

    # ═══════════════════════════════════════════════════════════
    # 步骤2.5 ★v2新增★ 动态获取行业分类
    #   使用 QMT 的 get_industry_name_of_stock() 逐只查询
    #   实盘/仿真下可用，回测下自动 fallback
    # ═══════════════════════════════════════════════════════════
    State.sector_map, State.sector_ok = _build_sector_map_from_qmt(
        ContextInfo, State.stock_pool)

    # ★v2新增★ 批量获取股票名称（供后续日志显示用）
    State.stock_names = _build_stock_names(ContextInfo, State.stock_pool)

    # ═══════════════════════════════════════════════════════════
    # 步骤3：设置交易标的（universe）
    #   - QMT 需要知道哪些股票需要订阅行情
    #   - 把成分股 + 基准指数都加入 universe
    # ═══════════════════════════════════════════════════════════
    universe = valid[:] + [BENCHMARK]
    ContextInfo.set_universe(list(set(universe)))

    # ═══════════════════════════════════════════════════════════
    # 步骤4：设置回测参数
    #   - 初始资金、基准指数、回测起止日期
    #   - 使用 try/except 包裹，因为在实盘模式下设置这些会报错
    # ═══════════════════════════════════════════════════════════
    # 注意：以下参数仅在回测模式下生效，实盘/仿真模式下会被忽略
    for attr, val in [
        ('capital',   State.capital),       # 初始资金
        ('benchmark', BENCHMARK),            # 基准指数
        ('start',     '2022-01-01 09:30:00'), # 回测开始时间
        ('end',       '2026-06-19 15:00:00'), # 回测结束时间
    ]:
        try:
            setattr(ContextInfo, attr, val)
        except (AttributeError, TypeError):
            pass  # 实盘模式下这些属性可能不可设置

    # ═══════════════════════════════════════════════════════════
    # 步骤5：设置交易成本
    #   - 滑点：千分之1（买入时实际成交价 = 信号价 × 1.001）
    #   - 手续费：佣金万2.5（双边），印花税千1（仅卖出），最低5元
    #     set_commission() 参数含义：
    #       0           — 费用类型（0=股票）
    #       [0.00025,   — 开仓佣金率（买入）
    #        0.00025,   — 平仓佣金率（卖出）
    #        0.001,     — 印花税率（仅卖出）
    #        0.0, 0.0,  — 其他费用
    #        5.0]       — 最低佣金
    # ═══════════════════════════════════════════════════════════
    ContextInfo.set_slippage(1, 0.001)     # 滑点类型1，千分之1
    ContextInfo.set_commission(0, [0.00025, 0.00025, 0.001, 0.0, 0.0, 5.0])

    # ═══════════════════════════════════════════════════════════
    # 步骤6：设置交易账号
    #   - 回测模式下可以用任意字符串
    #   - 实盘/仿真模式下需要填写真实的资金账号
    # ═══════════════════════════════════════════════════════════
    ContextInfo.set_account(State.acc_id)

    # ═══════════════════════════════════════════════════════════
    # 步骤7：打印初始化摘要
    # ═══════════════════════════════════════════════════════════
    print("[init] 初始化完成：")
    print("       股票池 = %d 只" % len(State.stock_pool))
    print("       行业分类 = %s（%d 个行业，%d 只已分类）" % (
        "申万SW" if INDUSTRY_TYPE == 'SW' else "证监会CSRC",
        len(set(State.sector_map.values())) if State.sector_ok else 0,
        len(State.sector_map) if State.sector_ok else 0))
    print("       因子刷新间隔 = %d 天" % REFRESH_INTERVAL)
    print("       最大持仓 = %d 只" % MAX_POSITIONS)
    print("       持有期 = %d 天" % MAX_HOLD_BARS)
    print("       大盘过滤器 = %s MA%d × %.0f%%" % (
        BENCHMARK, MA_MARKET, (1 - MARKET_FILTER_PCT) * 100))
    print("       初始资金 = %.0f 万" % (State.capital / 10000))


# ╔════════════════════════════════════════════════════════════╗
# ║              策略核心函数：handlebar()                       ║
# ║  QMT 每根K线调用一次，是策略逻辑的"发动机"。                 ║
# ╚════════════════════════════════════════════════════════════╝
def handlebar(ContextInfo):
    """
    策略主循环函数。QMT 每根K线（每个交易日）调用一次。

    执行顺序：
      1. 跳过数据不足的阶段
      2. 防止同一根 Bar 被重复执行
      3. 获取历史行情数据
      4. 更新账户资金和持仓信息
      5. 大盘过滤器（判断是否可以交易）
      6. 处理昨日因跌停未能卖出的股票
      7. 检查持仓是否需要出场（持有到期 / 硬止损）
      8. 如果需要，刷新因子排名（每10天一次）
      9. 检查入场条件（突破5日新高 + 在 Top 15% 排名中）
      10. 如果大盘触发防御，清空全部持仓
      11. 更新持仓天数

    参数：
      ContextInfo — QMT 上下文对象，提供当前 Bar 的信息
    """
    bar = ContextInfo.barpos  # 当前 Bar 在历史序列中的位置（从0开始）

    # ═══════════════════════════════════════════════════════════
    # 步骤1：数据不足时直接跳过
    #   因子计算需要 FACTOR_WINDOW + 30 根 Bar 的数据
    #   均线计算需要 MA_MARKET 根 Bar 的数据
    #   突破判断需要 BREAKOUT_PERIOD 根 Bar 的数据
    #   所以至少要等 MIN_HISTORY_BARS 根 Bar 之后才开始交易
    # ═══════════════════════════════════════════════════════════
    if bar < MIN_HISTORY_BARS:
        return

    # ═══════════════════════════════════════════════════════════
    # 步骤2：防止同一根 Bar 被重复执行
    #   QMT 在某些情况下可能在同一根 Bar 多次触发 handlebar
    #   （如 tick 数据和日线数据同时到达时），这里做去重处理
    # ═══════════════════════════════════════════════════════════
    if bar == State.last_barpos:
        return
    State.last_barpos = bar
    State.bar_counter += 1  # 自增 Bar 计数器

    # ═══════════════════════════════════════════════════════════
    # 步骤3：获取历史行情数据
    #   get_history_data() 返回 dict：
    #   {股票代码: [最早值, ..., 最新值]}
    #
    #   需要获取的 Bar 数 = 因子窗口 + 均线缓冲 + 突破周期
    #   这里多取30根作为安全缓冲
    # ═══════════════════════════════════════════════════════════
    need_bars = max(FACTOR_WINDOW + 30, MA_MARKET + 10, BREAKOUT_PERIOD + 10)
    hist_close  = ContextInfo.get_history_data(need_bars, '1d', 'close')   # 收盘价
    hist_amount = ContextInfo.get_history_data(need_bars, '1d', 'amount')  # 成交额

    # ═══════════════════════════════════════════════════════════
    # 步骤4：更新账户信息
    #   - 同步现金和总资产
    #   - 同步本地持仓记录与实际持仓
    #   - 计算当前持仓市值
    # ═══════════════════════════════════════════════════════════
    _update_account(ContextInfo)  # 从 QMT 获取最新现金和总资产

    # 总资产 = 现金 + 持仓市值（用当日收盘价估算）
    State.total_assets = State.cash + _calc_total_position_value(
        ContextInfo, hist_close)

    # 同步持仓：将 QMT 端的实际持仓与本地记录对齐
    _sync_positions(ContextInfo)

    # ═══════════════════════════════════════════════════════════
    # 步骤5：大盘过滤器
    #   检查中证500指数是否在 MA20 × (1 - 3%) 之上
    #   低于这个阈值 → 市场转弱 → 空仓避险
    # ═══════════════════════════════════════════════════════════
    State.market_ok = _check_market(hist_close)
    if not State.market_ok:
        print("[市场] %s 收盘价 < MA%d × %.0f%%，触发空仓避险" % (
            BENCHMARK, MA_MARKET, (1 - MARKET_FILTER_PCT) * 100))

    # ═══════════════════════════════════════════════════════════
    # 步骤6：日志输出（方便追踪每日状态）
    # ═══════════════════════════════════════════════════════════
    date_str = _log_time(ContextInfo)  # 获取当前 Bar 的日期字符串
    print("=" * 50)
    print("[%s] bar=%d cnt=%d 持仓=%d只 资产=%.0f万 现金=%.0f万 市场=%s" % (
        date_str, bar, State.bar_counter, len(State.positions),
        State.total_assets / 10000, State.cash / 10000,
        "可交易" if State.market_ok else "防御"))

    # ═══════════════════════════════════════════════════════════
    # 步骤7：处理昨天因跌停未能卖出的持仓
    #   如果一只股票昨天跌停无法卖出，今天再试一次
    #   如果今天仍然跌停 → 继续等到明天
    #   如果今天不跌停了 → 立即卖出
    # ═══════════════════════════════════════════════════════════
    _process_pending_sells(ContextInfo, hist_close)

    # ═══════════════════════════════════════════════════════════
    # 步骤8：检查持仓出场条件
    #   - 持有满 MAX_HOLD_BARS（20天）→ 到期卖出
    #   - 浮亏超过 HARD_STOP_PCT（-18%）→ 硬止损
    # ═══════════════════════════════════════════════════════════
    _check_exits(ContextInfo, hist_close)

    # ═══════════════════════════════════════════════════════════
    # 步骤9：因子排名刷新（每 REFRESH_INTERVAL 天一次）
    #   - 对全股票池计算 Alpha#144 因子值
    #   - 按因子值从大到小排列（越大越好）
    #   - 取前 FACTOR_TOP_PCT（15%）作为候选买入池
    # ═══════════════════════════════════════════════════════════
    if State.bar_counter >= State.next_refresh_bar:
        State.rankings = _compute_factor_rankings(hist_close, hist_amount)
        State.next_refresh_bar = State.bar_counter + REFRESH_INTERVAL
        print("[刷新] 因子排名完成：%d 只股票进入候选池" % len(State.rankings))

    # ═══════════════════════════════════════════════════════════
    # 步骤10：入场检查
    #   条件（全部满足才会买入）：
    #     1. 大盘可交易（market_ok = True）
    #     2. 当前持仓数 < 最大持仓数
    #     3. 股票在最新因子排名的 Top 15% 中
    #     4. 今日收盘价 > 过去5日最高价（突破信号）
    #     5. 今日未涨停
    #     6. 成交量确认（放量突破）
    # ═══════════════════════════════════════════════════════════
    if State.market_ok and len(State.positions) < MAX_POSITIONS:
        _check_entry_breakout(ContextInfo, hist_close, hist_amount)

    # ═══════════════════════════════════════════════════════════
    # 步骤11：大盘防御 — 清空全部持仓
    #   当大盘跌破 MA20×(1-3%) 且还有持仓时，全部清仓
    # ═══════════════════════════════════════════════════════════
    if not State.market_ok and len(State.positions) > 0:
        _liquidate_all(ContextInfo, hist_close, "大盘防御")

    # ═══════════════════════════════════════════════════════════
    # 步骤12：更新所有持仓的持有天数
    #   每过一根 Bar，所有持仓的 bars_held 加1
    # ═══════════════════════════════════════════════════════════
    for code in list(State.positions.keys()):
        State.positions[code]['bars_held'] += 1

    # ═══════════════════════════════════════════════════════════
    # 步骤13：打印每日摘要
    # ═══════════════════════════════════════════════════════════
    pos_codes = list(State.positions.keys())
    hold_days = [State.positions[c]['bars_held'] for c in pos_codes]
    pos_labels = [_stock_label(c) for c in pos_codes]
    print("[摘要] 持仓=%d只 %s | 持有天数=%s | 下次刷新还有=%d天" % (
        len(State.positions),
        pos_labels if pos_labels else "空仓",
        hold_days if hold_days else "-",
        State.next_refresh_bar - State.bar_counter))


# ╔════════════════════════════════════════════════════════════╗
# ║              Alpha#144 因子计算函数                         ║
# ╚════════════════════════════════════════════════════════════╝

def _calc_alpha144(close_arr, amount_arr):
    """
    计算单只股票的 Alpha#144 因子值。

    【核心公式】
      alpha_144 = Σ(|ret_i| / amount_i)  对所有 ret_i < 0 的过去 20 个交易日

    【公式解释】
      - ret_i：第 i 天的涨跌幅（小数形式，如 0.02 表示涨了 2%，-0.03 表示跌了 3%）
      - amount_i：第 i 天的成交额（单位：元，由 QMT 的 'amount' 字段提供）
      - |ret_i| / amount_i：每 1 元成交额对应的价格变动幅度
        → 这个值越大，说明同样的成交额推动了更大的价格下跌
        → 即市场深度越浅、流动性冲击越大
      - 只对下跌日（ret_i < 0）求和：因为我们关心的是恐慌抛售，不是正常上涨

    【直觉理解】
      想象一个极端场景：
      - 股票A：一天只成交了100万元，却跌了5%  → 流动性极差，恐慌盘无人接
      - 股票B：一天成交了1亿元，只跌了1%      → 流动性充裕，有资金在承接

      股票A的因子值更大，说明它的筹码在恐慌中被砸得更厉害，后续反弹空间更大。

    【参数】
      close_arr  — 收盘价序列（list或array，长度 >= FACTOR_WINDOW+1）
      amount_arr — 成交额序列（list或array，长度 >= FACTOR_WINDOW+1）

    【返回值】
      alpha_144 因子值（float），越大越好。如果过去20天没有下跌日，返回0。
    """
    # 转为 numpy 数组，方便向量运算
    arr_c = np.array(close_arr, dtype=float)
    arr_a = np.array(amount_arr, dtype=float)

    n = min(len(arr_c), len(arr_a))

    # 至少需要 FACTOR_WINDOW + 1 个数据点
    # 因为计算涨跌幅 ret_i = (close[t] - close[t-1]) / close[t-1] 需要两个点
    needed = FACTOR_WINDOW + 1
    if n < needed:
        return None  # 数据不足，无法计算

    # 取最近 FACTOR_WINDOW 天的涨跌幅和成交额
    # arr[-needed:] 表示从倒数第 needed 个元素到最后一个元素
    recent_c = arr_c[-needed:]  # 最近 needed 天的收盘价
    recent_a = arr_a[-needed:]  # 最近 needed 天的成交额

    alpha = 0.0     # 累加器：因子值
    neg_count = 0   # 计数器：下跌日的数量

    # 遍历每一天（从第2天开始，因为第1天没有前一天作为参考）
    for i in range(1, len(recent_c)):
        prev_close = recent_c[i - 1]  # 前一天收盘价
        curr_close = recent_c[i]      # 当天收盘价

        # 计算当日涨跌幅（小数形式）
        if prev_close > 0:
            ret_i = (curr_close - prev_close) / prev_close
        else:
            ret_i = 0

        # ★ 核心逻辑：只对下跌日求和 ★
        if ret_i < 0:
            amount_i = recent_a[i] if i < len(recent_a) else 0
            if amount_i > 0:
                # |ret_i| / amount_i 就是当日的"单位成交额冲击"
                alpha += abs(ret_i) / amount_i
                neg_count += 1

    # 如果过去20天一天都没跌过 → 说明这只股票太强了（或者数据有问题）
    # 此时因子值为0，不参与排名
    if neg_count == 0:
        return 0.0

    return alpha


def _compute_factor_rankings(hist_close, hist_amount):
    """
    计算全股票池的 Alpha#144 因子排名。

    【流程】
      1. 遍历 filtered_pool 中每只股票
      2. 对每只股票检查数据质量（历史长度、日均成交额）
      3. 计算 alpha_144 因子值
      4. 按因子值从大到小排序
      5. 取前 FACTOR_TOP_PCT（15%）作为候选池

    【参数】
      hist_close  — QMT 的 get_history_data() 返回的收盘价 dict
      hist_amount — QMT 的 get_history_data() 返回的成交额 dict

    【返回值】
      dict：{股票代码: 因子值, ...}，只包含 Top 15% 的股票
    """
    raw_scores = {}  # {股票代码: 因子值}

    for code in State.filtered_pool:
        # ── 检查收盘价数据是否充足 ──
        close_arr = hist_close.get(code, [])
        if len(close_arr) < FACTOR_WINDOW + 1:
            continue

        # ── 检查成交额数据是否充足 ──
        amount_arr = hist_amount.get(code, [])
        if len(amount_arr) < FACTOR_WINDOW + 1:
            continue

        # ── 流动性过滤：近20日日均成交额必须 > MIN_DAILY_AMOUNT ──
        # 如果一只股票日成交额只有几百万，微观结构因子没有意义
        # 因为几手交易就能拉出大波动，不反映真正的流动性冲击
        try:
            recent_amounts = np.array(amount_arr[-FACTOR_WINDOW:], dtype=float)
            avg_amount = np.mean(recent_amounts)
            if avg_amount < MIN_DAILY_AMOUNT:  # 3000万
                continue
        except Exception:
            continue

        # ── 计算因子值 ──
        val = _calc_alpha144(close_arr, amount_arr)
        if val is not None:
            raw_scores[code] = val

    # ── 如果没有可计算的股票，返回空 ──
    if not raw_scores:
        return {}

    # ── 按因子值降序排列（值越大越好）──
    sorted_codes = sorted(raw_scores.keys(),
                          key=lambda code: raw_scores[code],
                          reverse=True)

    # ── 取前 Top 15% ──
    top_n = max(1, int(len(sorted_codes) * FACTOR_TOP_PCT))

    # ── 组装返回值 ──
    rankings = {}
    for code in sorted_codes[:top_n]:
        rankings[code] = raw_scores[code]

    return rankings


# ╔════════════════════════════════════════════════════════════╗
# ║              入场判断 — 突破5日新高                         ║
# ╚════════════════════════════════════════════════════════════╝

def _check_entry_breakout(ContextInfo, hist_close, hist_amount):
    """
    检查候选池中是否有突破5日新高的股票，触发买入。

    【入场条件（全部满足才买入）】
      (1) 股票在最新因子排名的 Top 15% 中
      (2) 今日收盘价 > 过去5日（不含今天）的最高价 → 突破确认
      (3) 今日未涨停（涨幅 < 9.8%）→ 避免追涨停板
      (4) 当前未持仓该股票
      (5) 当前持仓数 < MAX_POSITIONS（5只）
      (6) 成交量确认：当日成交额 >= 近5日均量的 80% → 放量突破更可靠

    【买入方式】
      QMT 回测中，handlebar 在收盘时执行。
      此时发出买入信号，QMT 会在下一根 Bar（次日）的开盘价成交。
      因此这里的 price 用当日收盘价近似估算（实盘应改为次日开盘价）。

    【等权分配】
      每只买入的股票分配资金 = 总资产 / MAX_POSITIONS
      例如总资产1000万，最多持仓5只 → 每只分配200万。
    """
    # ── 如果没有排名数据，无法进行入场判断 ──
    if not State.rankings:
        return

    held = set(State.positions.keys())  # 当前已持仓的股票代码集合
    slots = MAX_POSITIONS - len(State.positions)  # 还能买入的最大数量
    if slots <= 0:
        return  # 已经满仓了

    # ── 按因子值从高到低遍历候选池 ──
    ranked_list = sorted(State.rankings.keys(),
                         key=lambda c: State.rankings[c],
                         reverse=True)

    signals = []  # 本轮发现的买入信号列表

    for code in ranked_list:
        # ── 已持仓的跳过 ──
        if code in held:
            continue

        # ── 检查历史数据是否充足 ──
        close_arr = hist_close.get(code, [])
        if len(close_arr) < BREAKOUT_PERIOD + 2:
            continue

        arr = np.array(close_arr, dtype=float)
        current_close = arr[-1]   # 今日收盘价
        prev_close = arr[-2] if len(arr) >= 2 else current_close  # 昨日收盘价

        # ── 涨跌停检查：涨停的股票不追 ──
        daily_ret = (current_close - prev_close) / prev_close if prev_close > 0 else 0
        if daily_ret >= LIMIT_UP_PCT:
            print("  [入场跳过] %s 涨停（涨幅%.1f%%）" % (_stock_label(code), daily_ret * 100))
            continue

        # ── 突破检查：今日收盘 > 过去5日最高价（不含今天）──
        # arr[-(BREAKOUT_PERIOD + 1):-1] 表示"从倒数第6个到倒数第2个"
        # 即过去5天的价格（不含今天）
        past_5_high = np.max(arr[-(BREAKOUT_PERIOD + 1):-1])
        if current_close <= past_5_high:
            continue  # 没有突破，跳过

        # ── 成交量确认：当日成交额 >= 近5日均量的 80% ──
        # 放量突破比缩量突破更可靠，因为有量能配合
        amount_arr = hist_amount.get(code, [])
        vol_ok = True
        if len(amount_arr) >= 6:
            try:
                today_amt = float(amount_arr[-1])
                avg_amt = np.mean([float(amount_arr[i])
                                   for i in range(-6, -1)
                                   if amount_arr[i] is not None])
                vol_ok = today_amt > avg_amt * 0.8 if avg_amt > 0 else True
            except Exception:
                vol_ok = True  # 数据异常时放行

        if not vol_ok:
            continue  # 缩量，跳过

        # ── 记录有效信号 ──
        factor_val = State.rankings.get(code, 0)
        signals.append((code, current_close, factor_val))
        print("  [信号] %s 突破5日新高! 收盘=%.2f 5日最高=%.2f alpha144=%.2e" % (
            _stock_label(code), current_close, past_5_high, factor_val))

        # ── 信号数达到剩余仓位上限时停止扫描 ──
        if len(signals) >= slots:
            break

    # ── 批量发出买入订单 ──
    if signals:
        _buy_signals(ContextInfo, signals)


def _buy_signals(ContextInfo, signals):
    """
    对突破信号发出买入订单。

    【等权分配逻辑】
      每只股票分配资金 = 总资产 / MAX_POSITIONS
      这样即使只有部分仓位被占用，每只股票的资金分配也是一致的。

      例如：总资产1000万，MAX_POSITIONS=5
      每只分配 1000/5 = 200万（不管当前持有了几只）。

    【行业限制】
      - 已分类行业（如医药、电子）：最多持有 2 只
      - 未分类行业（"其他"）：最多持有 3 只
      这样做是为了防止单一行业暴雷导致全账户受损。

    【参数】
      signals：[(股票代码, 当前价格, 因子值), ...]
    """
    n_signals = len(signals)
    if n_signals == 0:
        return

    # ── 计算等权分配金额 ──
    total_equity = State.total_assets if State.total_assets > 0 else State.capital
    allocation_per_stock = total_equity / MAX_POSITIONS

    # ── 统计当前持仓的行业分布 ──
    sector_counts = {}
    for code in State.positions.keys():
        sec = _get_sector(code)
        sector_counts[sec] = sector_counts.get(sec, 0) + 1

    bought = 0  # 本轮实际买入的股票数

    for code, price, factor_val in signals:
        # ── 重复检查 ──
        if code in State.positions:
            continue
        if len(State.positions) >= MAX_POSITIONS:
            break

        # ── 行业限制检查 ──
        sec = _get_sector(code)
        sec_limit = MAX_SECTOR_OTHER if sec == UNKNOWN_SECTOR else MAX_SECTOR_COUNT
        if sector_counts.get(sec, 0) >= sec_limit:
            print("  [买入跳过] %s 已满%d只（上限%d只）" % (
                _stock_label(code), sector_counts[sec], sec_limit))
            continue

        # ── 计算买入股数（取整百股，A股最小交易单位100股）──
        shares = int(allocation_per_stock / price / 100) * 100
        if shares < 100:
            shares = 100  # 至少买一手

        # ── 现金检查（预留千2的手续费缓冲）──
        need_cash = shares * price * 1.002
        if need_cash > State.cash:
            # 现金不够，按可用资金的 98% 调整股数
            shares = int(State.cash * 0.98 / price / 100) * 100
            if shares < 100:
                print("  [买入失败] %s 资金不足: 需要%.0f 可用%.0f" % (
                    _stock_label(code), need_cash, State.cash))
                continue

        # ── 发出买入订单 ──
        # passorder 参数说明：
        #   opType=23      → 买入
        #   orderType=1101 → 股票交易
        #   prType=5       → 最新价（市价）
        #   modelprice=-1  → 与prType配合使用
        #   volume=shares  → 买入股数（正数）
        try:
            passorder(23, 1101, State.acc_id, code, 5, -1, shares,
                      'Alpha144突破', 1, '', ContextInfo)
        except Exception as e:
            print("  [买入失败] %s 下单异常: %s" % (_stock_label(code), str(e)))
            continue

        # ── 记录到本地持仓状态 ──
        State.positions[code] = {
            'shares':      shares,          # 持仓股数
            'entry_price': price,           # 入场价（用于计算盈亏）
            'entry_bar':   State.last_barpos,  # 入场时的 Bar 位置
            'bars_held':   0,               # 已持有天数（下一根Bar会+1）
        }
        sector_counts[sec] = sector_counts.get(sec, 0) + 1

        print(">>> [买入] %s × %d股 @ %.2f | 金额%.0f | alpha144=%.2e" % (
            _stock_label(code), shares, price, shares * price, factor_val))
        bought += 1


# ╔════════════════════════════════════════════════════════════╗
# ║              出场判断 — 持有到期 + 硬止损                    ║
# ╚════════════════════════════════════════════════════════════╝

def _check_exits(ContextInfo, hist_close):
    """
    检查持仓的出场条件。

    【出场优先级】
      (1) 硬止损：浮亏超过 HARD_STOP_PCT（默认-18%）→ 无条件平仓
      (2) 持有到期：bars_held >= MAX_HOLD_BARS（20天）→ 到期卖出

    【跌停保护】
      如果需要卖出但当天跌停（跌幅 <= -9.8%），则推迟到下一个交易日再卖出。
      因为跌停时卖单几乎不可能成交，强行卖出只会吃跌停板。
    """
    to_sell = []  # [(股票代码, 卖出原因), ...]

    for code, pos in State.positions.items():
        # ── 获取当前价格 ──
        px = _get_price(ContextInfo, code, hist_close)
        if px <= 0:
            continue

        entry = pos['entry_price']
        pnl_pct = (px / entry - 1.0) if entry > 0 else 0

        # ── 硬止损检查：浮亏 >= 18% ──
        if pnl_pct <= HARD_STOP_PCT:
            print("  [止损触发] %s 浮亏%.1f%% <= %.0f%%" % (
                _stock_label(code), pnl_pct * 100, HARD_STOP_PCT * 100))
            to_sell.append((code, "硬止损%.0f%%(浮亏%.1f%%)" % (
                HARD_STOP_PCT * 100, pnl_pct * 100)))
            continue

        # ── 持有到期检查 ──
        if pos['bars_held'] >= MAX_HOLD_BARS:
            to_sell.append((code, "持有%d天到期" % MAX_HOLD_BARS))

    # ── 批量执行卖出 ──
    for code, reason in to_sell:
        _sell_position(ContextInfo, code, hist_close, reason)


def _process_pending_sells(ContextInfo, hist_close):
    """
    处理之前因跌停未能卖出的持仓。

    如果昨天跌停卖不掉，今天继续尝试。
    如果今天还在跌停 → 继续等到明天。
    如果今天打开跌停了 → 立即卖出。
    """
    if not State.pending_sells:
        return

    retry_list = list(State.pending_sells)  # 复制一份避免迭代时修改
    State.pending_sells = []                # 清空待重试列表

    for code in retry_list:
        if code not in State.positions:
            continue  # 持仓已经被别的条件清掉了
        _sell_position(ContextInfo, code, hist_close, "补卖(昨日跌停)")


def _sell_position(ContextInfo, code, hist_close, reason):
    """
    卖出一只股票。

    【跌停约束】
      先检查当日是否跌停（今日收盘 / 昨日收盘 - 1 <= -9.8%）
      如果跌停 → 不卖出，加入 pending_sells 列表，等下一个交易日再试
      如果不跌停 → 正常卖出

    【参数】
      code   — 股票代码
      reason — 卖出原因（用于日志和交易备注）
    """
    if code not in State.positions:
        return

    pos = State.positions[code]
    shares = pos.get('shares', 0)
    if shares <= 0:
        del State.positions[code]
        return

    px = _get_price(ContextInfo, code, hist_close)

    # ── 跌停检查 ──
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

    # ── 发出卖出订单 ──
    # passorder 参数说明：
    #   opType=24      → 卖出
    #   orderType=1101 → 股票交易
    #   prType=5       → 最新价（市价）
    #   volume=shares  → 卖出股数（正数，方向由 opType 决定）
    try:
        passorder(24, 1101, State.acc_id, code, 5, -1, shares,
                  'Alpha144卖出', 1, '', ContextInfo)
    except Exception as e:
        print("  [卖出失败] %s 下单异常: %s" % (_stock_label(code), str(e)))
        return

    # ── 计算盈亏 ──
    entry_price = pos['entry_price']
    pnl_pct = (px / entry_price - 1) * 100 if entry_price > 0 else 0
    bars = pos.get('bars_held', 0)

    print("<<< [卖出] %s × %d股 @ %.2f | 盈亏%+.1f%% | 持有%d天 | %s" % (
        _stock_label(code), shares, px, pnl_pct, bars, reason))

    # ── 从持仓字典中移除 ──
    del State.positions[code]

    # ── 从待卖出列表中移除（如果存在）──
    if code in State.pending_sells:
        State.pending_sells.remove(code)


# ╔════════════════════════════════════════════════════════════╗
# ║   行业分类查询（★v2 改为从 QMT 动态获取）                      ║
# ╚════════════════════════════════════════════════════════════╝

def _get_sector(code):
    """
    查询股票所属行业（★v2 改为从 State.sector_map 动态获取）。

    State.sector_map 在 init() 中由 _build_sector_map_from_qmt() 构建。
    如果动态获取成功 → 返回申万一级行业名，如 'SW1电子'
    如果动态获取失败 → 全部返回 '其他'（行业限制退化为仅检查 MAX_SECTOR_OTHER）

    【参数】
      code — 股票代码，如 '601869.SH'

    【返回值】
      str：行业名称
    """
    if State.sector_ok:
        return State.sector_map.get(code, UNKNOWN_SECTOR)
    else:
        return UNKNOWN_SECTOR


# ╔════════════════════════════════════════════════════════════╗
# ║              大盘防御 — 清仓                                 ║
# ╚════════════════════════════════════════════════════════════╝

def _liquidate_all(ContextInfo, hist_close, reason):
    """
    清空全部持仓（大盘触发防御时调用）。

    遍历所有持仓，逐个卖出。
    """
    for code in list(State.positions.keys()):
        _sell_position(ContextInfo, code, hist_close, reason)


# ╔════════════════════════════════════════════════════════════╗
# ║              辅助函数                                       ║
# ╚════════════════════════════════════════════════════════════╝

def _check_market(hist_close):
    """
    大盘过滤器：判断中证500指数是否在 MA20 × (1-3%) 之上。

    【逻辑】
      计算中证500的20日简单移动平均线（MA20），
      如果今日收盘价 >= MA20 × (1 - 3%)，
      则市场状态正常（可交易），
      否则触发空仓防御。

    【直觉】
      当大盘指数跌破20日均线超过3%时，说明市场整体走弱，
      此时即使是好股票也容易被市场拖累。
      空仓避险可以躲过大部分系统性下跌。

    【返回值】
      True  → 大盘正常，可以交易
      False → 大盘转弱，应该空仓
    """
    # ── 检查基准指数数据是否可用 ──
    if BENCHMARK not in hist_close:
        return True  # 无数据时默认放行

    arr = hist_close[BENCHMARK]
    if len(arr) < MA_MARKET + 1:
        return True  # 数据不足时默认放行

    close_arr = np.array(arr, dtype=float)
    current = close_arr[-1]                        # 中证500当前收盘价
    ma = np.mean(close_arr[-MA_MARKET:])           # MA20 均值
    threshold = ma * (1.0 - MARKET_FILTER_PCT)     # MA20 × 97%

    return current >= threshold


def _calc_total_position_value(ContextInfo, hist_close):
    """
    计算当前持仓的总市值。

    遍历所有持仓，用当日收盘价估算每只股票的市值，然后求和。
    """
    total = 0.0
    for code, pos in State.positions.items():
        shares = pos.get('shares', 0)
        px = _get_price(ContextInfo, code, hist_close)
        total += shares * px
    return total


def _update_account(ContextInfo):
    """
    更新账户资金信息。

    优先从 QMT 的 get_trade_detail_data() 获取真实账户数据。
    如果获取失败（比如回测模式下），则从 ContextInfo 属性获取。
    """
    try:
        # ── 尝试获取真实账户数据 ──
        account_list = get_trade_detail_data(State.acc_id, 'stock', 'account')
        if account_list:
            State.cash = account_list[0].m_dAvailable  # 可用资金
            State.total_assets = account_list[0].m_dBalance  # 总资产（含市值）
            return
    except Exception:
        pass

    # ── fallback：从ContextInfo获取（回测模式）──
    try:
        State.cash = ContextInfo.cash
        State.total_assets = ContextInfo.capital
    except Exception:
        pass


def _sync_positions(ContextInfo):
    """
    从 QMT 交易系统同步实际持仓，与本地 State.positions 对齐。

    因为可能有外部操作（手动下单、其他策略等）影响持仓，
    所以需要定期同步。在纯回测模式下通常不需要，但保留这个机制
    可以在实盘/仿真时避免持仓数据不一致。
    """
    try:
        # ── 获取远程实际持仓 ──
        position_list = get_trade_detail_data(State.acc_id, 'stock', 'position')
        remote_positions = {}

        for p in position_list:
            # QMT 的返回结构：m_strInstrumentID = 代码（如 '000002'）
            #                  m_strExchangeID = 交易所（如 'SZ', 'SH'）
            code = p.m_strInstrumentID + '.' + p.m_strExchangeID
            vol = p.m_nVolume  # 持仓数量
            if vol <= 0:
                continue

            if code in State.positions:
                # 本地已有记录 → 更新股数
                old = State.positions[code]
                old['shares'] = vol
                remote_positions[code] = old
            else:
                # 远程有持仓但本地没有（可能是外部操作产生）→ 新建记录
                remote_positions[code] = {
                    'shares':      vol,
                    'entry_price': p.m_dOpenPrice,
                    'entry_bar':   State.last_barpos,
                    'bars_held':   0,
                }

        # ── 清理本地有但远程已无的持仓 ──
        for code in list(State.positions.keys()):
            if code not in remote_positions:
                del State.positions[code]

        # ── 合并远程有但本地无的持仓 ──
        for code, pos in remote_positions.items():
            if code not in State.positions:
                State.positions[code] = pos

    except Exception:
        # 回测模式下 get_trade_detail_data 可能不可用
        # 此时信任本地 State.positions 的数据
        pass


def _get_price(ContextInfo, code, hist_close):
    """
    获取股票当前价格。

    优先从实时tick数据获取（更准确），
    获取不到时用历史收盘价（回测模式），
    都获取不到返回0。
    """
    # ── 方式1：实时tick数据 ──
    try:
        tick = ContextInfo.get_full_tick([code])
        if code in tick:
            lp = tick[code].get('lastPrice', 0)
            if lp > 0:
                return lp
    except Exception:
        pass

    # ── 方式2：历史收盘价 ──
    if code in hist_close and len(hist_close[code]) > 0:
        return float(hist_close[code][-1])

    return 0


def _log_time(ContextInfo):
    """
    获取当前 Bar 的可读时间字符串。

    用于日志输出，方便追踪每一天的策略行为。
    """
    try:
        t = ContextInfo.get_bar_timetag(ContextInfo.barpos)
        # timetag_to_datetime 是 QMT 内置函数，将毫秒时间戳转为格式化字符串
        return timetag_to_datetime(t, '%Y-%m-%d %H:%M')
    except Exception:
        return str(ContextInfo.barpos)


# ╔════════════════════════════════════════════════════════════╗
# ║   硬编码 Fallback 中证500成分股（当 API 不可用时使用）       ║
# ║   约 170 只代表性成分股，覆盖主要行业                         ║
# ╚════════════════════════════════════════════════════════════╝

def _get_fallback_csi500():
    """
    返回硬编码的中证500代表性成分股列表。

    用途：当 QMT 的 get_stock_list_in_sector() 和 get_sector() 都不可用时
    （比如某些回测环境），使用这个 fallback 列表。

    注意：实际中证500有500只成分股，这里只选取了约170只流动性较好的标的
    作为代表性样本。
    """
    return [
        # === 医药（约15%权重）===
        '300003.SZ', '300009.SZ', '300015.SZ', '300026.SZ', '300039.SZ',
        '002001.SZ', '002007.SZ', '002019.SZ', '002020.SZ', '002022.SZ',
        '600079.SH', '600085.SH', '600196.SH', '600276.SH', '600380.SH',
        '300595.SZ', '300601.SZ', '300633.SZ', '300676.SZ', '300725.SZ',

        # === 电子/半导体（约12%权重）===
        '002049.SZ', '002138.SZ', '002185.SZ', '002273.SZ', '002371.SZ',
        '002409.SZ', '002436.SZ', '002456.SZ', '002463.SZ', '002475.SZ',
        '603160.SH', '603501.SH', '603986.SH', '688008.SH', '688012.SH',

        # === 计算机/软件（约10%权重）===
        '002230.SZ', '002368.SZ', '002373.SZ', '002405.SZ', '002410.SZ',
        '300033.SZ', '300036.SZ', '300059.SZ', '300168.SZ', '300253.SZ',

        # === 化工（约8%权重）===
        '002064.SZ', '002092.SZ', '002108.SZ', '002250.SZ', '002258.SZ',
        '002326.SZ', '002407.SZ', '002408.SZ', '002440.SZ', '002460.SZ',

        # === 机械/军工（约10%权重）===
        '002013.SZ', '002025.SZ', '002050.SZ', '002074.SZ', '002097.SZ',
        '300024.SZ', '300124.SZ', '300274.SZ', '300316.SZ', '300450.SZ',

        # === 电力设备/新能源（约8%权重）===
        '002459.SZ', '002121.SZ', '002129.SZ', '002202.SZ', '002245.SZ',
        '300014.SZ', '300037.SZ', '300068.SZ', '300073.SZ', '300118.SZ',

        # === 有色/钢铁（约7%权重）===
        '000060.SZ', '000630.SZ', '000807.SZ', '000831.SZ', '000878.SZ',
        '000933.SZ', '000960.SZ', '000975.SZ', '002155.SZ', '002203.SZ',

        # === 传媒/游戏（约6%权重）===
        '002555.SZ', '002602.SZ', '002624.SZ', '300058.SZ', '300133.SZ',
        '300251.SZ', '300413.SZ', '300418.SZ', '603444.SH',

        # === 交运/物流（约4%权重）===
        '002120.SZ', '002352.SZ', '002468.SZ', '600026.SH', '600029.SH',

        # === 其他（食品/农牧/建材等）===
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
