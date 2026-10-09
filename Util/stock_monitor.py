# -*- coding: utf-8 -*-
"""
实时股价监控器 — 终端实时刷新
==============================
默认监控: 长飞光纤 (601869)
数据源:   腾讯财经 HTTP API（不封IP，含PE/PB/市值/换手率）
修改下方 STOCK_LIST 即可增删股票，刷新间隔改 REFRESH_INTERVAL
"""

import os
import sys
import time
import urllib.request
from datetime import datetime


# ═══════════════════════════════════════════════════════════════
#  >>> 修改这里 <<<  股票列表 — 按需增删
# ═══════════════════════════════════════════════════════════════
STOCK_LIST = [
    "601869",  # 长飞光纤
    # "600519",  # 贵州茅台
    # "000858",  # 五粮液
    # "300750",  # 宁德时代
    # "002475",  # 立讯精密
]

REFRESH_INTERVAL = 2   # 刷新间隔(秒)，建议 1~5
# ═══════════════════════════════════════════════════════════════


def get_prefix(code):
    """6位代码 → 市场前缀"""
    if code.startswith(("6", "9")):
        return "sh"
    elif code.startswith("8"):
        return "bj"
    return "sz"


def tencent_quote(codes):
    """
    批量拉取腾讯财经实时行情。
    返回: {code: {name, price, last_close, open, high, low,
                  change_pct, amount_wan, turnover_pct,
                  pe_ttm, pb, mcap_yi, limit_up, limit_down, ...}}
    """
    prefixed = [f"{get_prefix(c)}{c}" for c in codes]
    url = "https://qt.gtimg.cn/q=" + ",".join(prefixed)
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        data = resp.read().decode("gbk")
    except Exception as e:
        raise RuntimeError(f"腾讯行情请求失败: {e}")

    result = {}
    for line in data.strip().split(";"):
        if not line.strip() or "=" not in line or '"' not in line:
            continue
        key = line.split("=")[0].split("_")[-1]
        vals = line.split('"')[1].split("~")
        if len(vals) < 53:
            continue
        code = key[2:]  # 去掉 sh/sz/bj 前缀
        try:
            result[code] = {
                "name":          vals[1],
                "price":         float(vals[3])  if vals[3]  else 0,
                "last_close":    float(vals[4])  if vals[4]  else 0,
                "open":          float(vals[5])  if vals[5]  else 0,
                "high":          float(vals[33]) if vals[33] else 0,
                "low":           float(vals[34]) if vals[34] else 0,
                "change_pct":    float(vals[32]) if vals[32] else 0,
                "amount_wan":    float(vals[37]) if vals[37] else 0,   # 成交额(万)
                "turnover_pct":  float(vals[38]) if vals[38] else 0,   # 换手率%
                "pe_ttm":        float(vals[39]) if vals[39] else 0,   # PE(TTM)
                "pb":            float(vals[46]) if vals[46] else 0,   # PB
                "mcap_yi":       float(vals[44]) if vals[44] else 0,   # 总市值(亿)
                "limit_up":      float(vals[47]) if vals[47] else 0,   # 涨停价
                "limit_down":    float(vals[48]) if vals[48] else 0,   # 跌停价
                "amplitude_pct": float(vals[43]) if vals[43] else 0,   # 振幅%
                "vol_ratio":     float(vals[49]) if vals[49] else 0,   # 量比
            }
        except (ValueError, IndexError):
            continue
    return result


def fmt_amount_wan(amt_wan):
    """成交额: 万 → 亿/万"""
    if amt_wan >= 10000:
        return f"{amt_wan / 10000:.2f}亿"
    return f"{amt_wan:.0f}万"


def fmt_mcap(mcap_yi):
    """市值: 亿 → 亿"""
    if mcap_yi >= 10000:
        return f"{mcap_yi / 10000:.2f}万亿"
    return f"{mcap_yi:.0f}亿"


def is_trading_time():
    """A股交易时段（含集合竞价 9:15-15:05）"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.hour * 100 + now.minute
    return 915 <= t <= 1505


def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def main():
    print("数据源: 腾讯财经 HTTP API")
    print(f"监控 {len(STOCK_LIST)} 只股票 | 刷新间隔 {REFRESH_INTERVAL}s")
    print("按 Ctrl+C 退出\n")
    time.sleep(0.6)

    try:
        while True:
            clear_screen()
            now = datetime.now()
            ts = now.strftime("%H:%M:%S")
            trading = is_trading_time()
            status = "[IN] 交易中" if trading else "[OFF] 休市"

            # ── 获取行情 ──
            fetch_ok = True
            try:
                data = tencent_quote(STOCK_LIST)
            except Exception as e:
                data = {}
                fetch_ok = False
                err_msg = str(e)[:50]

            # ── 表头 ──
            W = 79
            print(f"+{'=' * W}+")
            print(f"|  实时股价监控  {'':>48} {ts}  {status} |")
            print(f"+{'=' * W}+")
            hdr = (
                f"| {'名称':^6} | {'代码':^6} | {'现价':>8} | {'涨跌幅':>8} | "
                f"{'最高':>8} | {'最低':>8} | {'成交额':>10} | {'换手':>6} | {'PE':>6} |"
            )
            print(hdr)
            print(f"+{'-'*8}-+-{'-'*8}-+-{'-'*8}-+-{'-'*8}-+-{'-'*8}-+-{'-'*8}-+-{'-'*10}-+-{'-'*6}-+-{'-'*6}-+")

            if not fetch_ok:
                print(f"|  [WARN] 行情获取失败: {err_msg:<44} |")
                print(f"+{'=' * W}+")
                time.sleep(REFRESH_INTERVAL)
                continue

            # ── 逐行 ──
            for code in STOCK_LIST:
                q = data.get(code)
                if not q:
                    print(
                        f"| {'?':^6} | {code:^6} | {'无数据':>8} | {'—':>8} | "
                        f"{'—':>8} | {'—':>8} | {'—':>10} | {'—':>6} | {'—':>6} |"
                    )
                    continue

                name = q["name"]
                price = q["price"]
                chg = q["change_pct"]
                high = q["high"]
                low = q["low"]
                amt = fmt_amount_wan(q["amount_wan"])
                turnover = f"{q['turnover_pct']:.2f}%"
                pe = f"{q['pe_ttm']:.0f}" if q["pe_ttm"] > 0 else "—"

                # 涨跌标记
                if chg > 0:
                    tag = "+"
                elif chg < 0:
                    tag = ""
                else:
                    tag = " "

                print(
                    f"| {name[:4]:^6} | {code:^6} | {tag}{price:>7.2f} | {chg:>+7.2f}% | "
                    f"{high:>8.2f} | {low:>8.2f} | {amt:>10} | {turnover:>6} | {pe:>6} |"
                )

            # ── 表尾 ──
            print(f"+{'=' * W}+")
            print(f"|  数据源: 腾讯财经 qt.gtimg.cn  |  刷新间隔 {REFRESH_INTERVAL}s  |  按 Ctrl+C 退出{'':>11} |")
            print(f"+{'=' * W}+")

            time.sleep(REFRESH_INTERVAL)

    except KeyboardInterrupt:
        print("\n\n监控已停止。")
        sys.exit(0)


if __name__ == "__main__":
    main()
