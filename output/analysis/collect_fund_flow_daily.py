r"""每日采集分钟级资金流，为「订单流信号」积累可回测样本。

背景：本仓库全部回测结论都指向「这份数据里没有可提取的日内 alpha」。
唯一未验证的信息源是**日内大单净流向**——东财的分钟级资金流正是这个，
但它**只有当天**（240 条，09:31–15:00），无法回测。

所以只能前向积累：每天收盘后跑一次，把当天的 240 分钟存下来。
攒够若干周后，就能用同一套评测台检验
「分钟级主力净流入」是否预测下一分钟收益。

用法（建议每个交易日 15:10 后运行一次）：
    python analysis/collect_fund_flow_daily.py                 # 全部标的
    python analysis/collect_fund_flow_daily.py --codes 601869  # 单个标的

注意：东财接口限流很严，脚本内置 1.2s 间隔与指数退避；
密集手工会触发 IP 封禁（本次会话已踩过）。

分钟级资金流**只能取当天**（东财不提供历史分钟资金流，已实测：
带日期范围 / klt=5 / lmt=1000 全部失败）。所以要检验它是否有预测力，
**只能前向积累**——每个交易日收盘后跑一次，攒够 20+ 个交易日后再分析。

自动化的两种方式：

1) Windows 任务计划（推荐）——每个工作日 15:10 运行：
   schtasks /create /tn QMT_FF_Collect /sc weekly /d MON,TUE,WED,THU,FRI /st 15:10 /tr "python C:/MyW/QMT-Expert/analysis/collect_fund_flow_daily.py"

2) 手动：收盘后自己跑一次即可。
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / "analysis/capturet_v1_lab_20260919/ff_store"
UNIVERSE = {
    "600584": "0.600584", "600105": "0.600105", "601869": "1.601869",
    "600519": "1.600519", "000001": "0.000001", "600036": "1.600036",
    "601318": "1.601318", "000858": "0.000858", "002415": "0.002415",
    "600030": "1.600030", "601899": "1.601899", "600276": "1.600276",
    "002594": "0.002594", "601012": "1.601012", "600887": "1.600887",
    "000651": "0.000651",
}
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Referer": "https://data.eastmoney.com/",
}
FIELDS = "f51,f52,f53,f54,f55,f56"   # 时间, 主力, 小单, 中单, 大单, 超大单
COLUMNS = ["time", "main", "small", "mid", "big", "xbig"]


# 主入口限流后会被封；push2delay 是备用入口，实测在主入口被拒时仍可用。
HOSTS = ("push2.eastmoney.com", "push2delay.eastmoney.com")


def fetch_minute_flow(secid: str, tries: int = 4) -> list[list[str]]:
    last = None
    for attempt in range(tries):
        for host in HOSTS:
            url = (f"https://{host}/api/qt/stock/fflow/kline/get"
                   f"?lmt=0&klt=1&secid={secid}&fields1=f1,f2,f3,f7&fields2={FIELDS}")
            try:
                request = urllib.request.Request(url, headers=HEADERS)
                payload = urllib.request.urlopen(request, timeout=25).read()
                data = json.loads(payload.decode("utf-8", "ignore")).get("data") or {}
                rows = [line.split(",") for line in (data.get("klines") or [])]
                if rows:
                    return rows
            except Exception as error:                  # 限流/瞬时故障
                last = error
        time.sleep(1.5 * (2 ** attempt))
    raise RuntimeError(f"{secid}: {last}")


def collect(codes: dict[str, str]) -> dict[str, int]:
    STORE.mkdir(parents=True, exist_ok=True)
    written = {}
    for code, secid in codes.items():
        try:
            rows = fetch_minute_flow(secid)
        except Exception as error:
            print(f"  {code} 失败: {error}", flush=True)
            continue
        if not rows:
            print(f"  {code} 空数据", flush=True)
            continue
        day = rows[0][0][:10].replace("-", "")
        path = STORE / f"{code}.csv"
        header = "" if path.exists() else ",".join(COLUMNS) + "\n"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(header)
            for row in rows:
                handle.write(",".join(row) + "\n")
        written[code] = len(rows)
        print(f"  {code} {day}: {len(rows)} 分钟", flush=True)
        time.sleep(1.2)                                  # 保守限速
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codes", default="",
                        help="逗号分隔的代码；留空则采集整个标的池")
    args = parser.parse_args()
    codes = UNIVERSE
    if args.codes:
        wanted = {c.strip() for c in args.codes.split(",") if c.strip()}
        codes = {k: v for k, v in UNIVERSE.items() if k in wanted}
        if not codes:
            raise SystemExit("没有匹配的代码: " + args.codes)
    print(f"采集 {len(codes)} 个标的的分钟级资金流 -> {STORE}")
    written = collect(codes)
    print(f"完成: {len(written)}/{len(codes)} 个标的写入")


if __name__ == "__main__":
    main()
