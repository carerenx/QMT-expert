#coding:gbk
"""
QMT 行业分类动态获取模块
=========================
使用 QMT 内置函数动态获取股票行业分类，替代硬编码的 SECTOR_MAP。

【QMT API 说明】
  行业分类相关函数（来源：Python API 文档）：

  1. get_industry_name_of_stock(industryType, stockcode)
     - 获取某只股票所属的行业名称
     - industryType: 'SW'（申万）或 'CSRC'（证监会）
     - stockcode: 'stockcode.market' 格式，如 '600000.SH'
     - 返回: 行业名字符串，如 'SW1银行'、'CSRC1采矿业'；找不到返回空字符串
     - [Python API, p.39]

  2. ContextInfo.get_industry(industry)
     - 获取指定行业的所有成分股
     - industry: 行业名称，如 'CSRC1采矿业'
     - 返回: 股票代码列表，如 ['000002.SZ', ...]
     - [Python API, p.45]

  3. ContextInfo.get_stock_list_in_sector(sectorname, realtime)
     - 获取板块成分股（支持客户端左侧任意板块）
     - sectorname: 板块名，如 '沪深300'、'中证500'、'申万一级行业板块'等
     - [Python API, p.45]

【设计思路】
  - 在 init() 阶段遍历股票池，用 get_industry_name_of_stock() 逐个查询行业
  - 构建 {股票代码: 行业名} 的映射字典
  - 支持缓存和 fallback 机制（回测模式下该函数可能不可用）
  - 支持 SW（申万）和 CSRC（证监会）两种分类体系

【使用方式】
  方式1 — 在策略中导入模块：
    from QMT_行业分类_动态获取 import build_sector_map, get_sector_for_stock

  方式2 — 直接复制 build_sector_map() 函数到策略文件中

作者：QMT-Expert
日期：2026-08-06
"""

# ╔════════════════════════════════════════════════════════════╗
# ║   行业分类类型常量                                          ║
# ╚════════════════════════════════════════════════════════════╝

# 申万行业分类（推荐用于策略，因为分类粒度适中，约28个一级行业）
INDUSTRY_SW   = 'SW'     # 申万一级行业，返回如 'SW1银行'
INDUSTRY_CSRC = 'CSRC'   # 证监会行业分类，返回如 'CSRC1采矿业'

# 默认使用申万一级行业分类
DEFAULT_INDUSTRY_TYPE = INDUSTRY_SW

# 未分类股票的默认标签
UNKNOWN_SECTOR = '其他'


# ╔════════════════════════════════════════════════════════════╗
# ║   核心函数：动态构建行业映射字典                              ║
# ╚════════════════════════════════════════════════════════════╝

def build_sector_map(ContextInfo, stock_list, industry_type=DEFAULT_INDUSTRY_TYPE,
                     verbose=True):
    """
    【核心函数】使用 QMT 的 get_industry_name_of_stock() 动态构建行业映射字典。

    遍历给定的股票列表，对每只股票调用 QMT 的行业查询函数，返回
    {股票代码: 行业名称} 的映射字典。

    【参数】
      ContextInfo   — QMT 上下文对象（init 或 handlebar 中传入）
      stock_list    — 股票代码列表，格式如 ['000001.SZ', '600000.SH', ...]
      industry_type — 行业分类标准：'SW'（申万）或 'CSRC'（证监会）
      verbose       — 是否打印详细日志

    【返回值】
      dict：{股票代码: 行业名称}，未分类的股票返回空字符串 ''（调用方可用
             SECTOR_MAP.get(code, '其他') 处理）

    【性能说明】
      - 对 500 只股票逐一查询，约需 1-3 秒（取决于 QMT 客户端数据加载情况）
      - 建议在 init() 中调用一次，结果缓存到模块级变量
      - 回测模式下该函数可能不可用，此时返回空字典（触发 fallback）

    【示例】
      # 在 init() 中调用：
      stock_list = ContextInfo.get_stock_list_in_sector('中证500')
      sector_map = build_sector_map(ContextInfo, stock_list, 'SW')
      # sector_map: {'000001.SZ': 'SW1银行', '000002.SZ': 'SW1房地产', ...}
    """
    sector_map = {}
    success_count = 0
    fail_count = 0

    if verbose:
        print("[SectorMap] 开始动态获取行业分类（%s），共 %d 只股票..." %
              (industry_type, len(stock_list)))

    for i, code in enumerate(stock_list):
        try:
            # ── 调用 QMT 内置函数获取行业名称 ──
            # get_industry_name_of_stock 是 QMT 全局函数（非 ContextInfo 方法）
            # 参数1: 行业分类标准 ('SW' 或 'CSRC')
            # 参数2: 股票代码 ('code.market' 格式)
            # 返回: 行业名称字符串，如 'SW1电子'；找不到返回空字符串 ''
            # 参考：[Python API, p.39] 第(16)条
            industry_name = get_industry_name_of_stock(industry_type, code)

            if industry_name and len(industry_name) > 0:
                sector_map[code] = industry_name
                success_count += 1
            else:
                # 未找到行业分类 → 不加入 map，调用方用 .get(code, '其他') 处理
                fail_count += 1

        except NameError:
            # get_industry_name_of_stock 函数不存在（回测环境可能不支持）
            if verbose:
                print("[SectorMap] ⚠ get_industry_name_of_stock() 不可用（可能处于回测模式）")
                print("[SectorMap] 请使用 fallback 方案或手动填写 SECTOR_MAP")
            return {}  # 返回空字典，由调用方判断并 fallback

        except Exception as e:
            fail_count += 1
            if verbose and i < 5:  # 只打印前5个错误，避免刷屏
                print("[SectorMap] 查询 %s 行业失败: %s" % (code, str(e)))

        # ── 进度日志（每100只打印一次）──
        if verbose and (i + 1) % 100 == 0:
            print("[SectorMap] 进度: %d/%d (成功=%d, 未分类=%d)" %
                  (i + 1, len(stock_list), success_count, fail_count))

    if verbose:
        unique_sectors = len(set(sector_map.values()))
        print("[SectorMap] 完成! 成功=%d 未分类=%d 行业数=%d" %
              (success_count, fail_count, unique_sectors))
        print("[SectorMap] 行业分布: %s" %
              _format_sector_summary(sector_map, max_items=10))

    return sector_map


# ╔════════════════════════════════════════════════════════════╗
# ║   辅助函数：获取单只股票的行业                                ║
# ╚════════════════════════════════════════════════════════════╝

def get_sector_for_stock(code, sector_map):
    """
    查询某只股票的行业分类。

    【参数】
      code       — 股票代码，如 '000001.SZ'
      sector_map — build_sector_map() 返回的字典

    【返回值】
      str：行业名称；未分类时返回 '其他'
    """
    return sector_map.get(code, UNKNOWN_SECTOR)


# ╔════════════════════════════════════════════════════════════╗
# ║   高级函数：使用 get_industry() 反向获取行业成分股             ║
# ╚════════════════════════════════════════════════════════════╝

def get_stocks_by_industry(ContextInfo, industry_name):
    """
    【高级用法】使用 ContextInfo.get_industry() 获取指定行业的所有成分股。

    与 build_sector_map() 的"逐个查行业"方向相反，这里是"给定行业，查有哪些股票"。
    适合用于：先确定关注的行业，再获取行业内所有股票。

    【参数】
      ContextInfo    — QMT 上下文对象
      industry_name  — 行业名称，如 'CSRC1采矿业'、'CSRC1制造业'

    【返回值】
      list：股票代码列表，如 ['000002.SZ', '600019.SH', ...]
      失败返回空列表

    【示例】
      stocks = get_stocks_by_industry(ContextInfo, 'CSRC1采矿业')
      print(stocks)  # ['000552.SZ', '000655.SZ', ...]

    参考：[Python API, p.45] 第(5)条 ContextInfo.get_industry()
    """
    try:
        result = ContextInfo.get_industry(industry_name)
        if result and len(result) > 0:
            return list(result)
        return []
    except Exception as e:
        print("[SectorMap] get_industry('%s') 失败: %s" % (industry_name, str(e)))
        return []


# ╔════════════════════════════════════════════════════════════╗
# ║   批量构建：先获取所有行业，再构建全市场映射                      ║
# ╚════════════════════════════════════════════════════════════╝

def build_sector_map_from_industries(ContextInfo, industry_names, verbose=True):
    """
    【批量模式】从行业列表出发，反向构建全市场行业映射。

    先用 ContextInfo.get_industry() 获取每个行业下的所有股票，
    再汇总成 {股票代码: 行业名} 的映射字典。

    【优点】
      - 比逐只查询更快（行业数量 << 股票数量）
      - 不依赖 get_industry_name_of_stock()（回测兼容性更好）

    【缺点】
      - 需要提前知道行业名称列表（如 'CSRC1采矿业', 'CSRC1制造业', ...）
      - 只支持 CSRC 分类（get_industry 的参数格式为 'CSRC1行业名'）
      - 申万分类需使用板块名如 'SW1银行'（待验证）

    【参数】
      ContextInfo     — QMT 上下文对象
      industry_names  — 行业名称列表，如 ['CSRC1采矿业', 'CSRC1制造业', ...]
      verbose         — 是否打印详细日志

    【返回值】
      dict：{股票代码: 行业名}

    【示例】
      # 证监会一级行业（约19个）
      csrc_industries = [
          'CSRC1采矿业', 'CSRC1制造业', 'CSRC1金融业',
          'CSRC1房地产业', 'CSRC1信息技术', ...
      ]
      sector_map = build_sector_map_from_industries(ContextInfo, csrc_industries)
    """
    sector_map = {}
    total_stocks = 0

    if verbose:
        print("[SectorMap] 批量模式：从 %d 个行业反向构建映射..." % len(industry_names))

    for ind_name in industry_names:
        stocks = get_stocks_by_industry(ContextInfo, ind_name)
        for code in stocks:
            sector_map[code] = ind_name
        total_stocks += len(stocks)

        if verbose:
            print("[SectorMap]   %s → %d 只股票" % (ind_name, len(stocks)))

    if verbose:
        # 统计被多个行业包含的股票（部分股票可能跨行业）
        unique_codes = len(sector_map)
        unique_sectors = len(set(sector_map.values()))
        print("[SectorMap] 批量完成! 总股票=%d 去重后=%d 行业数=%d" %
              (total_stocks, unique_codes, unique_sectors))

    return sector_map


# ╔════════════════════════════════════════════════════════════╗
# ║   基于指数成分股的行业分类（另一种思路）                       ║
# ╚════════════════════════════════════════════════════════════╝

def build_sector_map_via_sectors(ContextInfo, sector_names, realtime=None, verbose=True):
    """
    【板块模式】通过 QMT 板块名称获取成分股，再逐个查询行业。

    适用于：你有一组自定义板块（行业板块、概念板块等），想获取板块内所有
    股票的行业分类。

    【工作原理】
      1. 对每个板块名，调用 ContextInfo.get_stock_list_in_sector() 获取股票
      2. 对每只股票调用 get_industry_name_of_stock() 获取行业
      3. 汇总成 {代码: 行业} 映射

    【参数】
      ContextInfo  — QMT 上下文对象
      sector_names — 板块名列表，如 ['沪深300', '中证500', '我的自选']
      realtime     — 毫秒时间戳（None=最新），影响历史成分股获取
      verbose      — 是否打印详细日志

    【返回值】
      dict：{股票代码: 行业名}

    参考：[Python API, p.45] 第(6)条 ContextInfo.get_stock_list_in_sector()
    """
    all_codes = set()

    if verbose:
        print("[SectorMap] 板块模式：从 %d 个板块获取成分股..." % len(sector_names))

    for sname in sector_names:
        try:
            codes = ContextInfo.get_stock_list_in_sector(sname, realtime)
            if codes:
                all_codes.update(codes)
                if verbose:
                    print("[SectorMap]   %s → %d 只股票" % (sname, len(codes)))
        except Exception as e:
            if verbose:
                print("[SectorMap] 获取板块 '%s' 失败: %s" % (sname, str(e)))

    if verbose:
        print("[SectorMap] 去重后共 %d 只股票，开始查询行业..." % len(all_codes))

    return build_sector_map(ContextInfo, list(all_codes), verbose=verbose)


# ╔════════════════════════════════════════════════════════════╗
# ║   预定义的行业名称列表（用于批量模式）                          ║
# ╚════════════════════════════════════════════════════════════╝

# 申万一级行业（2021版，31个行业）
SW_INDUSTRY_NAMES = [
    'SW1银行', 'SW1房地产', 'SW1建筑装饰', 'SW1建筑材料',
    'SW1非银金融', 'SW1交通运输', 'SW1公用事业', 'SW1环保',
    'SW1钢铁', 'SW1基础化工', 'SW1石油石化', 'SW1有色金属',
    'SW1煤炭', 'SW1电力设备', 'SW1机械设备', 'SW1汽车',
    'SW1国防军工', 'SW1电子', 'SW1计算机', 'SW1通信',
    'SW1传媒', 'SW1食品饮料', 'SW1农林牧渔', 'SW1纺织服饰',
    'SW1轻工制造', 'SW1家用电器', 'SW1医药生物', 'SW1美容护理',
    'SW1社会服务', 'SW1商贸零售', 'SW1综合',
]

# 证监会一级行业（19个行业）— 用于 ContextInfo.get_industry()
CSRC_INDUSTRY_NAMES = [
    'CSRC1采矿业',
    'CSRC1制造业',
    'CSRC1电力、热力、燃气及水生产和供应业',
    'CSRC1建筑业',
    'CSRC1批发和零售业',
    'CSRC1交通运输、仓储和邮政业',
    'CSRC1住宿和餐饮业',
    'CSRC1信息传输、软件和信息技术服务业',
    'CSRC1金融业',
    'CSRC1房地产业',
    'CSRC1租赁和商务服务业',
    'CSRC1科学研究和技术服务业',
    'CSRC1水利、环境和公共设施管理业',
    'CSRC1居民服务、修理和其他服务业',
    'CSRC1教育',
    'CSRC1卫生和社会工作',
    'CSRC1文化、体育和娱乐业',
    'CSRC1农林牧渔',
    'CSRC1综合',
]


# ╔════════════════════════════════════════════════════════════╗
# ║   辅助工具函数                                               ║
# ╚════════════════════════════════════════════════════════════╝

def _format_sector_summary(sector_map, max_items=10):
    """格式化行业分布摘要（内部使用）"""
    from collections import Counter
    counter = Counter(sector_map.values())
    items = counter.most_common(max_items)
    parts = ["%s:%d" % (name.split('1')[-1] if '1' in name else name, cnt)
             for name, cnt in items]
    if len(counter) > max_items:
        parts.append("...共%d个行业" % len(counter))
    return ", ".join(parts)


def get_sector_summary(sector_map):
    """
    获取行业分布的统计摘要。

    【返回值】
      dict：{行业名: 股票数量}
    """
    from collections import Counter
    return dict(Counter(sector_map.values()))


def print_sector_summary(sector_map):
    """
    打印行业分布表格（方便调试和分析）。
    """
    summary = get_sector_summary(sector_map)
    print("\n" + "=" * 60)
    print("  行业分布统计（共 %d 只股票，%d 个行业）" %
          (len(sector_map), len(summary)))
    print("=" * 60)

    for sec_name, count in sorted(summary.items(),
                                   key=lambda x: x[1], reverse=True):
        bar = "█" * max(1, count // 2)
        # 简化行业名显示（去掉 SW1/CSRC1 前缀）
        display_name = sec_name.replace('SW1', '').replace('CSRC1', '')
        print("  %-16s %3d只 %s" % (display_name, count, bar))

    print("=" * 60)


# ╔════════════════════════════════════════════════════════════╗
# ║   QMT 策略集成示例 — init() 中的调用方式                      ║
# ╚════════════════════════════════════════════════════════════╝

def example_init_integration(ContextInfo):
    """
    【示例】展示如何在 QMT 策略的 init() 中集成动态行业分类。

    这段代码展示了三种获取行业分类的方式：
      方式A — 逐个查询（适合任何环境，最灵活）
      方式B — 批量反向查询（需要提前知道行业名列表）
      方式C — 先获取板块再查行业

    实际使用时，选其中一种即可。
    """
    # ═══════════════════════════════════════════════════════
    # 步骤1：获取股票池（以中证500为例）
    # ═══════════════════════════════════════════════════════
    stock_list = []
    try:
        stock_list = ContextInfo.get_stock_list_in_sector('中证500')
        print("[init] 中证500成分股: %d 只" % len(stock_list))
    except Exception:
        print("[init] 获取中证500失败，使用 fallback")

    if not stock_list:
        return {}

    # ═══════════════════════════════════════════════════════
    # 步骤2：动态构建行业映射
    # ═══════════════════════════════════════════════════════

    # ── 方式A：逐个查询（推荐，最通用）──
    print("\n--- 方式A: 申万一级行业 ---")
    sector_map_sw = build_sector_map(ContextInfo, stock_list, 'SW')

    # ── 方式B：批量反向查询（更快，需行业名列表）──
    # print("\n--- 方式B: 证监会行业 ---")
    # sector_map_csrc = build_sector_map_from_industries(
    #     ContextInfo, CSRC_INDUSTRY_NAMES)

    # ── 方式C：先获取板块成分股再查行业 ──
    # print("\n--- 方式C: 从申万行业板块获取 ---")
    # sector_map_via_sw = build_sector_map_via_sectors(
    #     ContextInfo, SW_INDUSTRY_NAMES)

    # ═══════════════════════════════════════════════════════
    # 步骤3：验证结果
    # ═══════════════════════════════════════════════════════
    if sector_map_sw:
        print("\n[init] 行业映射构建成功: %d 只股票已分类" % len(sector_map_sw))
        print_sector_summary(sector_map_sw)
    else:
        print("\n[init] ⚠ 动态获取失败，请使用手动 SECTOR_MAP 作为 fallback")

    return sector_map_sw


# ╔════════════════════════════════════════════════════════════╗
# ║   与 Alpha144 策略的集成适配器                               ║
# ╚════════════════════════════════════════════════════════════╝

def create_sector_getter(sector_map):
    """
    创建一个与 Alpha144 策略中 _get_sector() 接口兼容的查询函数。

    【用法】
      在原 Alpha144 策略的 init() 中：
        sector_map = build_sector_map(ContextInfo, State.stock_pool, 'SW')
        get_sector = create_sector_getter(sector_map)
        # 之后 handlebar 中调用 get_sector(code) 即可

      如果动态获取失败，会返回一个总是返回 '其他' 的 fallback 函数。

    【参数】
      sector_map — build_sector_map() 返回的字典，空字典表示获取失败

    【返回值】
      function: get_sector(code) -> 行业名字符串
    """
    if not sector_map or len(sector_map) == 0:
        # 动态获取失败 → fallback：所有股票归为"其他"
        print("[SectorMap] ⚠ 使用 fallback 模式：所有股票归为 '其他'")
        return lambda code: UNKNOWN_SECTOR

    return lambda code: sector_map.get(code, UNKNOWN_SECTOR)


# ╔════════════════════════════════════════════════════════════╗
# ║   模块自测（仅在直接运行时执行）                               ║
# ╚════════════════════════════════════════════════════════════╝

if __name__ == '__main__':
    """
    自测说明：
      本模块依赖 QMT 运行时环境（get_industry_name_of_stock 等函数由 QMT 注入）。
      在 QMT 外部直接运行只能验证模块可以正常导入，无法实际调用 API。

      完整测试方法：
        1. 将本文件放入 QMT 的 MyPy-Q 目录
        2. 在 QMT 策略的 init() 中调用 build_sector_map()
        3. 观察日志输出，确认行业分类是否正确
    """
    print("[自测] QMT 行业分类动态获取模块")
    print("[自测] 模块导入成功，所有函数可用于 QMT 策略。")
    print()
    print("[自测] 可用的公开函数：")
    print("  build_sector_map(ContextInfo, stock_list, industry_type)")
    print("  build_sector_map_from_industries(ContextInfo, industry_names)")
    print("  build_sector_map_via_sectors(ContextInfo, sector_names)")
    print("  get_sector_for_stock(code, sector_map)")
    print("  get_stocks_by_industry(ContextInfo, industry_name)")
    print("  create_sector_getter(sector_map)")
    print("  get_sector_summary(sector_map)")
    print("  print_sector_summary(sector_map)")
    print()
    print("[自测] 预定义的行业名称列表：")
    print("  SW_INDUSTRY_NAMES: %d 个申万一级行业" % len(SW_INDUSTRY_NAMES))
    print("  CSRC_INDUSTRY_NAMES: %d 个证监会一级行业" % len(CSRC_INDUSTRY_NAMES))
    print()
    print("[自测] 在 QMT 策略中集成示例：")
    print("  # 在 init() 中：")
    print("  sector_map = build_sector_map(ContextInfo, stock_list, 'SW')")
    print("  get_sector = create_sector_getter(sector_map)")
    print("  # 在 handlebar() 中：")
    print("  industry = get_sector('000001.SZ')  # → 'SW1银行'")
