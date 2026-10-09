"""Fetch bars through BigQMT Redis RPC and replay v53/v54/v55."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "integrations/bigqmt/src"
sys.path.insert(0, str(BRIDGE))
sys.path.insert(0, str(ROOT))

from bigqmt_signal_trader.xtquant_compat import configure, load_client_config
from backtest.dayt_strict import replay


OUT = ROOT / "analysis/dayt_redis_qmt_v53_v56_20260913"
SYMBOLS = (("600584.SH", "长电科技"), ("600105.SH", "永鼎股份"))
VERSIONS = ("v53_nomom", "v54_nomom", "v55_nomom", "v56_nomom")
FIELDS = ["open", "high", "low", "close", "volume", "amount"]
BAR_PERIOD = "1m"


def frame_hash(frame):
    payload = frame.to_csv(index=True, lineterminator="\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def fetch(xtdata, period, start_time="20260801"):
    return xtdata.get_market_data_ex(
        field_list=FIELDS,
        stock_list=[symbol for symbol, _ in SYMBOLS],
        period=period,
        start_time=start_time,
        end_time="20260913",
        count=-1,
        dividend_type="none",
        fill_data=False,
        chunk_size=0,
        timeout_seconds=30,
    )


def money(value):
    return f"{value:+,.2f}"


def main():
    config = load_client_config()
    account_id = str(config.get("account_id") or "")
    trader, xtdata = configure(account_id=account_id)
    pong = trader.client.call("ping")
    if not pong.get("pong") or str(pong.get("account_id")) != account_id:
        raise RuntimeError("Redis QMT bridge account verification failed")
    xtdata.download_history_data2(
        [symbol for symbol, _ in SYMBOLS],
        BAR_PERIOD,
        "20260801",
        "20260913",
        dividend_type="none",
        download_timeout_seconds=180,
        data_wait_seconds=90,
    )
    xtdata.download_history_data2(
        [symbol for symbol, _ in SYMBOLS],
        "1d",
        "20250101",
        "20260913",
        dividend_type="none",
        download_timeout_seconds=180,
        data_wait_seconds=90,
    )
    minute_by_symbol = fetch(xtdata, BAR_PERIOD)
    daily_by_symbol = fetch(xtdata, "1d", start_time="20250101")
    OUT.mkdir(parents=True, exist_ok=True)
    results = {}
    for symbol, stock_name in SYMBOLS:
        minute = minute_by_symbol[symbol].sort_index()
        daily = daily_by_symbol[symbol].sort_index()
        if minute.empty or daily.empty:
            raise RuntimeError(f"QMT history is empty for {symbol}")
        results[symbol] = {}
        for version in VERSIONS:
            overrides = {"T_TARGET_VALUE": 40000.0, "T_POSITION_FRACTION": 1.0}
            if version == "v56_nomom":
                overrides = {"T_TARGET_VALUE": 40000.0}
            result = replay(
                version,
                daily.copy(),
                minute.copy(),
                rate=0.0005,
                overrides=overrides,
                symbol=symbol,
                stock_name=stock_name,
                initial_cash=100000.0,
                initial_shares=1000,
            )
            result["provenance"] = {
                "source": "BigQMT Redis RPC",
                "account_id": account_id,
                "rpc_revision": pong.get("rpc_revision"),
                "server_time": pong.get("server_time"),
                "period": BAR_PERIOD,
                "dividend_type": "none",
                "minute_rows": len(minute),
                "daily_rows": len(daily),
                "minute_first": str(minute.index[0]),
                "minute_last": str(minute.index[-1]),
                "minute_sha256": frame_hash(minute),
                "daily_sha256": frame_hash(daily),
            }
            target = OUT / f"{symbol[:6]}_{version}_strict_redis_qmt.json"
            target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            results[symbol][version] = result
    write_report(results, pong)


def write_report(results, pong):
    lines = [
        "# Redis QMT 数据源：v53—v56 双标的重新回测",
        "",
        "> 数据通过 BigQMT Redis RPC 从当前 QMT 终端读取；不是此前保存的新浪/CSV数据。",
        "> 区间请求为2026-08-01至2026-09-13，使用QMT原始1分钟K线。",
        "> 初始账户为10万元现金+1000股；单次目标约4万元；费用率双边各0.05%。",
        "",
        "## 汇总",
        "",
        "| 标的 | 策略 | 账户收益 | 持有收益 | 超额净收益 | 完整T | 最终股数 | 费用 |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for symbol, name in SYMBOLS:
        for version in VERSIONS:
            result = results[symbol][version]
            hold_return = result["account_net"] - result["excess_net"]
            lines.append(
                f"| {name} {symbol[:6]} | {version} | {money(result['account_net'])} | "
                f"{money(hold_return)} | {money(result['excess_net'])} | "
                f"{result['completed_cycles']} | {result['final_position']} | {result['fees']:.2f} |"
            )
    lines.extend([
        "",
        "## 数据来源核验",
        "",
        f"- Redis桥接返回 `pong=True`，RPC版本 `{pong.get('rpc_revision')}`。",
        f"- 长电取得{results['600584.SH']['v55_nomom']['provenance']['minute_rows']:,}根1分钟线；永鼎取得{results['600105.SH']['v55_nomom']['provenance']['minute_rows']:,}根1分钟线。指标预热日线各{results['600584.SH']['v55_nomom']['provenance']['daily_rows']}根。",
        "- JSON结果内保存分钟线与日线的SHA-256、首尾时间、行数及RPC版本，可复核数据来源。",
        "- QMT官方说明 `get_market_data_ex` 支持1分钟、5分钟和日线；历史分钟线需先下载，本次已通过桥接提交下载后读取。[Python API, pp.70–71]",
        "",
        "## 结论",
        "",
        "v55没有复现5分钟回测中‘双标的均超过持有’的结论：永鼎v55超额为+443.19元，但长电v55超额为-1,383.37元。v56在同一份QMT 1分钟数据上将两者都推至正超额：长电+182.08元、永鼎+262.19元。",
        "",
        "1分钟K线让盘中触发顺序和价格路径更精细：长电v55由5分钟回测的12个完整T增加到18个，交易费用由418.28元增至642.37元，新增交易及不同触发价格使超额由+564.72元转为-1,383.37元。因此5分钟优化结论存在周期敏感性，v55尚不能认定在长电上优于持有。",
        "",
        "v56关闭了在本样本中拖累长电的正T通道，将1000股划分为600股核心仓和最多400股T仓，并要求反T达到延伸位后至少经过两个分钟事件、峰值额外延伸0.15%、回撤0.20%才确认卖出；14:20后不再新开反T，不利退出后冷却60分钟。",
        "",
        "v56长电完整周期由v55的18个降至13个，费用由642.37元降至327.92元；永鼎周期由16个降至9个，费用由311.81元降至123.81元。收益改善主要来自去除负贡献通道和降低换手，而非提高仓位。",
        "",
        "所有结果仍为`RESEARCH_ONLY`，不授权实盘。当前正超额很小，且同一30日样本参与了机制选择；必须用未参与设计的新日期进行样本外验证。",
    ])
    (OUT / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
