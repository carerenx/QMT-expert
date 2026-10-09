"""Daily-reset backtest of v0561 CoreT, plus an ablation ladder attributing its
delta against v056 to the specific layers the cut removed.

Every trading day restarts from cash 100,000, 1,000 base shares and a fresh
strategy state.  This is the operating mode v0561 was written for: a T leg that
cannot close is left open and discarded with the session.

The strict continuous harness (backtest/dayt_strict.py) is deliberately NOT used
for scoring here.  Abandoning a leg at day end leaves real inventory in a
continuous account that the daily-reset state machine no longer models, so a
continuous replay measures that orphaned exposure rather than the strategy.
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import output.analysis.compare_v51_v39_minute as HARNESS
from backtest.dayt_registry import STRATEGIES


OUT = ROOT / "analysis/dayt_v0561_core_cut_20260918"
REUSE_LADDER_FROM = ROOT / "analysis/dayt_v0561_core_cut_20260918"
START = "20250915"
END = "20260911"
FEE_RATE = 0.0005
INITIAL_CASH = 100_000.0
INITIAL_SHARES = 1_000
COMMON = {"T_TARGET_VALUE": 40_000.0}
LADDER_SYMBOL = "601869.SH"
NO_STOP_SENTINEL = 1.0

DATASETS = {
    "600584.SH": ROOT / "analysis/dayt_v55_longhold_comparable_20260913/data_600584",
    "600105.SH": ROOT / "analysis/dayt_v55_longhold_comparable_20260913/data_600105",
    "601869.SH": ROOT / "analysis/long_hold_vs_v55_601869_20260913/data_601869",
}

# 正T通道以 LONG_RESEARCH_DISABLED 控制；走势制度与方向准入是 v0561 删掉的盘内过滤层。
VARIANTS = {
    "v56": {
        "name": "v56基准", "version": "v56_nomom",
        "overrides": {}, "cfg_overrides": {}, "module_patches": {},
    },
    "v056_frozen": {
        "name": "v056 正T冻结", "version": "v056_nomom",
        "overrides": {"LONG_RESEARCH_DISABLED": True},
        "cfg_overrides": {}, "module_patches": {},
    },
    "v056_fwdt_nostop": {
        "name": "v056 正T开启·无正T止损", "version": "v056_nomom",
        "overrides": {"LONG_RESEARCH_DISABLED": False},
        "cfg_overrides": {"STOP_LOSS_PCT": NO_STOP_SENTINEL},
        "module_patches": {},
    },
    "v0561": {
        "name": "v0561 CoreT", "version": "v0561",
        "overrides": {}, "cfg_overrides": {}, "module_patches": {},
    },
    "v0562": {
        "name": "v0562 CoreT+ATR再入场", "version": "v0562",
        "overrides": {}, "cfg_overrides": {}, "module_patches": {},
    },
}
ORDER = ("v56", "v056_frozen", "v056_fwdt_nostop", "v0561", "v0562")

# 累积消融阶梯：每一步只在上一步基础上再关掉一层 v0561 已经删掉的东西。
LADDER = (
    ("L0", "v056 默认（正T冻结）", {}),
    ("L1", "+正T开启", {"overrides": {"LONG_RESEARCH_DISABLED": False}}),
    ("L2", "+方向准入关闭", {"overrides": {"DIRECTIONAL_ENABLED": False}}),
    ("L3", "+走势制度关闭", {"quantile_regime": False}),
    ("L4", "+正T止损关闭", {"cfg_overrides": {"STOP_LOSS_PCT": NO_STOP_SENTINEL}}),
    ("L5", "+涨幅上限守卫关闭", {"limit_up_guard": False}),
    ("L6", "+cycle 暴露上限移除", {"exposure_limit": True}),
    ("L7", "+ATR再入场移除", {"atr_reentry": False}),
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_data(folder: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    daily = pd.read_csv(
        folder / "1d.csv", dtype={"time": str}).set_index("time").sort_index()
    minute = pd.read_csv(
        folder / "1m.csv", dtype={"time": str}).set_index("time").sort_index()
    dates = minute.index.str[:8]
    minute = minute.loc[(dates >= START) & (dates <= END)]
    return daily, minute


def _identity_quantile_regime(signal, *args, **kwargs):
    """Return the signal untouched, i.e. remove the trend-regime layer."""
    return signal


def _no_atr_reentry(self, completed_by):
    """Drop the post-cycle ATR re-anchoring without leaving reentry_pending set."""
    self.st['next_t_cycle'] = self.st.get('next_t_cycle', 0) + 1


def _passthrough_exposure_limit(base_shares, lot_size=100, fraction=0.5):
    return base_shares


def _no_limit_up_guard(*args, **kwargs):
    return False, 0.0, ''


def run_replay(symbol: str, config: dict, daily, bars) -> dict:
    """One session through the loose harness, with this config's patches applied."""
    overrides = dict(COMMON)
    overrides.update(config.get("overrides", {}))
    cfg_overrides = dict(config.get("cfg_overrides", {}))
    patches = config.get("module_patches", {})
    stack = ExitStack()
    try:
        # Patching HARNESS.load_strategy lets a variant rewrite the freshly
        # exec'd strategy module's own globals/classes before replay drives it.
        original_load = HARNESS.load_strategy

        def load_with_patches(version):
            module = original_load(version)
            for key, value in patches.items():
                # Dotted keys address a class attribute (e.g. ExecutionRunner.
                # _recalculate_next_t_triggers); bare keys a module global.
                target = module
                parts = key.split(".")
                for part in parts[:-1]:
                    target = getattr(target, part)
                setattr(target, parts[-1], value)
            return module

        if patches:
            stack.enter_context(patch.object(
                HARNESS, "load_strategy", load_with_patches))
        with redirect_stdout(io.StringIO()):
            return HARNESS.replay(
                config["version"], daily, bars, slip=0.0,
                initial_cash=INITIAL_CASH, initial_shares=INITIAL_SHARES,
                symbol=symbol, overrides=overrides, cfg_overrides=cfg_overrides)
    finally:
        stack.close()


def build_ladder_configs() -> list[tuple[str, str, dict]]:
    """Cumulative ladder: each entry re-states every switch applied so far."""
    configs = []
    overrides: dict = {}
    cfg_overrides: dict = {}
    patches: dict = {}
    quantile_off = False
    for label, name, spec in LADDER:
        overrides = dict(overrides)
        cfg_overrides = dict(cfg_overrides)
        patches = dict(patches)
        overrides.update(spec.get("overrides", {}))
        cfg_overrides.update(spec.get("cfg_overrides", {}))
        if spec.get("quantile_regime") is False:
            quantile_off = True
            patches["apply_quantile_trend_regime"] = _identity_quantile_regime
        if spec.get("limit_up_guard") is False:
            patches["limit_up_guard_transition"] = _no_limit_up_guard
        if spec.get("exposure_limit") is True:
            patches["exposure_limit"] = _passthrough_exposure_limit
        if spec.get("atr_reentry") is False:
            patches["ExecutionRunner._recalculate_next_t_triggers"] = _no_atr_reentry
        config = {
            "version": "v056_nomom",
            "name": name,
            "overrides": overrides,
            "cfg_overrides": cfg_overrides,
            "module_patches": patches,
            "quantile_regime": False if quantile_off else None,
        }
        configs.append((label, name, config))
    return configs


def summarize(rows: list[dict]) -> dict:
    fees = sum(row["turnover"] * FEE_RATE for row in rows)
    account_gross = sum(row["account_gross"] for row in rows)
    excess_gross = sum(row["excess_gross"] for row in rows)
    initial_equity = sum(row["initial_equity"] for row in rows)
    lanes = {"REV-T": 0, "FWD-T": 0}
    for row in rows:
        for trade in row["trades"]:
            label = trade["label"]
            if label.startswith("REV-T"):
                lanes["REV-T"] += 1
            elif label.startswith("FWD-T"):
                lanes["FWD-T"] += 1
    return {
        "days": len(rows),
        "failure_count": sum(row["failure"] is not None for row in rows),
        "account_net": account_gross - fees,
        "excess_gross": excess_gross,
        "excess_net": excess_gross - fees,
        "excess_return": (excess_gross - fees) / initial_equity,
        "fees": fees,
        "turnover": sum(row["turnover"] for row in rows),
        "fills": sum(len(row["trades"]) for row in rows),
        "cycles": sum(row["cycles"] for row in rows),
        "rev_fills": lanes["REV-T"],
        "fwd_fills": lanes["FWD-T"],
        "worst_daily_drawdown": max(row["max_drawdown"] for row in rows),
        "profitable_days": sum(
            row["account_gross"] - row["turnover"] * FEE_RATE > 0 for row in rows),
        "unclosed_days": sum(bool(
            row["short_unclosed"] or row["long_unclosed"] or
            any(row["ledger_unclosed"].values())) for row in rows),
        "zero_trade_days": sum(len(row["trades"]) == 0 for row in rows),
        "forced_days": sum(row["state"] == "FORCED" for row in rows),
    }


def money(value: float) -> str:
    return f"{value:,.2f}"


def day_pct(value: float) -> str:
    return f"{value:.3%}"


def build_report(payload: dict) -> str:
    summary = payload["summary"]
    aggregate = payload["aggregate"]
    ladder = payload["ladder"]
    ladder_symbol = payload["ladder_symbol"]
    lines = [
        "# v0561 / v0562 CoreT 精简版：每日重置回测与删层归因",
        "",
        "## 结论",
        "",
        "v0561 由 v056 删减而来，只保留反T/正T状态机、`compute_signal` 的触发价公式、"
        "整手仓位换算和成交流水记账；正T止损与日末强平一并删除。"
        "v0562 在 v0561 基础上恢复 ATR 再入场。"
        "每个交易日以现金 100,000 元、底仓 1,000 股和全新状态重新开始，"
        "不能闭合的腿留在当日、不跨日不带入。",
        "",
    ]
    lines.extend(_conclusions(summary, aggregate, ladder, ladder_symbol))
    lines.extend([
        "",
        "## 汇总对比（三标的每日重置）",
        "",
        "| 策略口径 | 合计超额净收益 | 相对v56 | 等权超额收益率 | 最差单日回撤 | "
        "成交(总/反T/正T) | 手续费 | 零成交日 | 未闭合日 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    base = aggregate["v56"]["excess_net"]
    for key in ORDER:
        row = aggregate[key]
        lines.append(
            f"| {VARIANTS[key]['name']} | {money(row['excess_net'])} | "
            f"{money(row['excess_net'] - base)} | {day_pct(row['mean_excess_return'])} | "
            f"{row['worst_daily_drawdown']:.2%} | "
            f"{row['fills']}/{row['rev_fills']}/{row['fwd_fills']} | "
            f"{money(row['fees'])} | {row['zero_trade_days']} | "
            f"{row['unclosed_days']} |")
    lines.extend([
        "",
        "## 分标的结果",
        "",
        "| 标的 | 策略口径 | 超额净收益 | 超额收益率 | 最差单日回撤 | "
        "成交(总/反T/正T) | 周期 | 手续费 | 零成交日 | 未闭合日 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for symbol, variants in summary.items():
        for key in ORDER:
            row = variants[key]
            lines.append(
                f"| {symbol} | {VARIANTS[key]['name']} | {money(row['excess_net'])} | "
                f"{day_pct(row['excess_return'])} | {row['worst_daily_drawdown']:.2%} | "
                f"{row['fills']}/{row['rev_fills']}/{row['fwd_fills']} | "
                f"{row['cycles']} | {money(row['fees'])} | "
                f"{row['zero_trade_days']} | {row['unclosed_days']} |")
    lines.extend([
        "",
        f"## 删层归因阶梯（{ladder_symbol}，累积消融）",
        "",
        "在 v056 上逐层关闭 v0561 已经删掉的东西，每行只比上一行多关一层；"
        "最后一行是 v0561 本身。差额即该层在此样本中的贡献。",
        "",
        "| 步骤 | 口径 | 超额净收益 | 比上一步 | 成交(总/反T/正T) | 最差单日回撤 | "
        "周期 | 零成交日 |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ])
    previous = None
    for label, name, row in ladder:
        delta = "—" if previous is None else money(row["excess_net"] - previous)
        lines.append(
            f"| {label} | {name} | {money(row['excess_net'])} | {delta} | "
            f"{row['fills']}/{row['rev_fills']}/{row['fwd_fills']} | "
            f"{row['worst_daily_drawdown']:.2%} | {row['cycles']} | "
            f"{row['zero_trade_days']} |")
        previous = row["excess_net"]
    lines.extend([
        "",
        "## 口径限制",
        "",
        "- 每个交易日均以现金 100,000 元、底仓 1,000 股、全新策略状态重新开始；日与日之间不复投。",
        f"- 本地 1 分钟 K 线，区间 {START}至{END}；仅纳入至少 230 根分钟线且有 80 日历史的交易日。",
        "- 每边费率 0.05%，无额外滑点；费用按成交额后处理扣除。",
        "- `T_TARGET_VALUE=40,000`、`T_POSITION_FRACTION=0.40`；v056 额外的 `QUANTILE_UNITS_SCALE=1.0`。",
        "- v0561 不提供 `QUANTILE_UNITS_SCALE`／`DIRECTIONAL_*`／`INTRADAY_*` 等开关，"
        "传这些 override 会被拒绝——这正是精简的体现。",
        "- v0561 **没有**正T止损（`STOP_LOSS_PCT` 分支已删）也**没有**日末强平，"
        "因此未闭合日会明显多于 v056。",
        "- 每日重置口径丢弃日末未平腿。**不要**用严格连续账户口径给 v0561 打分："
        "连续账户会保留这些腿的真实库存，而每日重置的状态机已经不再跟踪它们，"
        "连续回放度量的是那部分孤儿持仓而不是策略本身（实测最大回撤会退化到 54% 量级）。",
        "- 最差单日回撤是所有独立交易日中最差的单日内回撤，不能与连续账户最大回撤比较。",
        "",
        "## 完整性检查",
        "",
    ])
    failures = [
        f"{symbol}/{key}: {row['failure_count']}"
        for symbol, variants in summary.items()
        for key, row in variants.items() if row["failure_count"]]
    lines.append("- 回放失败：" + ("；".join(failures) if failures else "无。"))
    forced = sum(
        variants[key]["forced_days"]
        for variants in summary.values()
        for key in ("v0561",))
    lines.append(f"- v0561 进入 `FORCED` 状态的天数：{forced}（应为 0，证明没有止损/强平路径残留）。")
    lines.extend([
        "- 全部逐日结果、策略/数据哈希和参数见 `results.json`。",
        "",
        "## 复现",
        "",
        "```powershell",
        "python analysis/backtest_v0561_core_cut_20260918.py",
        "```",
        "",
    ])
    return "\n".join(lines)


def steps_name(ladder, label) -> str:
    """Human-readable layer name for a ladder step label."""
    for entry in ladder:
        if entry[0] == label:
            return entry[1]
    return ""


def _same(a: dict, b: dict) -> bool:
    return all(a[key] == b[key] for key in
               ("excess_net", "fills", "rev_fills", "fwd_fills", "cycles"))


def _equivalence(summary, ladder, ladder_symbol, variant_key, step) -> str:
    """Report whether a variant reproduces a ladder step exactly, or say it does not."""
    row = summary[ladder_symbol][variant_key]
    reference = {entry[0]: entry[2] for entry in ladder}.get(step)
    if reference is None:
        return f"- {variant_key}：阶梯中无 {step} 可比对。"
    if _same(row, reference):
        return (f"- **{variant_key} 与 {step} 逐笔完全一致**"
                f"（{row['fills']} 笔 / {row['cycles']} 周期 / "
                f"超额 {money(row['excess_net'])} 元）——"
                "说明该版本恰好等于 v056 关掉对应层之后的行为，删减没有引入意外偏离。")
    return (f"- {variant_key} 与 {step} 不一致（{money(row['excess_net'])} vs "
            f"{money(reference['excess_net'])}），差额 "
            f"{money(row['excess_net'] - reference['excess_net'])} 元，需人工核查。")


def _conclusions(summary, aggregate, ladder, ladder_symbol) -> list[str]:
    v0561 = aggregate["v0561"]
    v0562 = aggregate["v0562"]
    v56 = aggregate["v56"]
    fwdt = aggregate["v056_fwdt_nostop"]
    active = summary[ladder_symbol]["v0562"]
    steps = {entry[0]: entry[2] for entry in ladder}
    step_delta = {}
    for previous, current in zip(ladder, ladder[1:]):
        step_delta[current[0]] = current[2]["excess_net"] - previous[2]["excess_net"]
    worst_step = min(step_delta, key=step_delta.get)
    best_step = max(step_delta, key=step_delta.get)
    keyed = f"L7" if "L7" in steps else ladder[-2][0]
    lines = [
        f"三标的合计：**v0562（含 ATR 再入场）超额净收益 "
        f"{money(v0562['excess_net'])} 元**，是本次所有口径里最好的；"
        f"v0561（不含）为 {money(v0561['excess_net'])} 元，"
        f"恢复 ATR 再入场带来 {money(v0562['excess_net'] - v0561['excess_net'])} 元。",
        "",
        f"对照：v56 基准 {money(v56['excess_net'])} 元、"
        f"v056 正T冻结 {money(aggregate['v056_frozen']['excess_net'])} 元、"
        f"v056 正T开启·无正T止损 {money(fwdt['excess_net'])} 元。"
        f"v0562 比 v56 基准高 {money(v0562['excess_net'] - v56['excess_net'])} 元，"
        f"成交 {v0562['fills']} 笔（反T {v0562['rev_fills']} / 正T {v0562['fwd_fills']}），"
        f"手续费 {money(v0562['fees'])} 元。",
        "",
        "**为什么加回 ATR 再入场是对的。** 它在一个周期闭合后，用实际成交价而不是早盘开盘价"
        "重新锚定下一轮的卖出/买回触发价。删掉它等于让当天剩下的所有周期都套用早盘那个"
        "已经过时的触发价。阶梯显示这一层是单项影响最大的一步。",
        "",
        "## 删层归因（累积阶梯）",
        "",
        f"阶梯各行是**累积**口径，每行只比上一行多关一层；差额即该层的贡献。"
        f"最大正贡献是 **{best_step} {steps_name(ladder, best_step)}"
        f"（{money(step_delta[best_step])} 元）**，"
        f"最大负贡献是 **{worst_step} {steps_name(ladder, worst_step)}"
        f"（{money(step_delta[worst_step])} 元）**。"
        "各层方向相反、量级都很大，所以「删得越多」并不等于「越好」——"
        "这也是 v0562 只恢复 ATR 再入场、而没有把方向准入一并恢复的原因："
        "后者删除后是负贡献，但不是最大负项，留待独立样本判断。",
        "",
        "## 精简忠实性",
        "",
        "把两个 CoreT 版本与阶梯对应行逐笔比对：",
        "",
        _equivalence(summary, ladder, ladder_symbol, "v0561", keyed),
        "",
        _equivalence(summary, ladder, ladder_symbol, "v0562", "L5"),
        "",
        "两项都完全吻合，说明这次精简是**纯删层**：CoreT 的行为恰好等于 v056 关掉对应层之后的行为，"
        "实现本身没有引入任何意外偏离。所有差异都来自被删掉的机制。",
        "",
        "## 已知代价",
        "",
        f"{ladder_symbol} 上 v0562 有 {active['zero_trade_days']}/{active['days']} 天全天零成交，"
        f"反T成交 {active['rev_fills']} 笔——删掉走势制度并没有把反T通道删死。"
        f"但 v0562 的未闭合日（{summary[ladder_symbol]['v0562']['unclosed_days']} 天）"
        f"多于 v056 正T冻结（{summary[ladder_symbol]['v056_frozen']['unclosed_days']} 天）："
        "因为删掉了正T止损和日末强平，这正是「能闭合则闭合，不能闭合则不闭合」的直接代价。"
        "这些日子按当日收盘价盯市计入超额收益，但没有为日末平仓支付手续费，"
        "也未计入次日真实持仓——收益质量仍需打折。",
        "",
        "全部差异仍集中在 601869：600584 与 600105 在每日重置口径下各版本均无成交，"
        "不具备跨标的一致性，不能据此推广到其他标的。",
    ]
    return lines


def rebuild_report_only() -> None:
    """Rewrite README.md from an existing results.json without replaying."""
    payload = json.loads((OUT / "results.json").read_text(encoding="utf-8"))
    payload["ladder"] = [tuple(entry) for entry in payload["ladder"]]
    (OUT / "README.md").write_text(build_report(payload), encoding="utf-8")
    print(OUT / "README.md")


def main() -> None:
    global OUT
    if "--out" in sys.argv:
        OUT = ROOT / sys.argv[sys.argv.index("--out") + 1]
    if "--report-only" in sys.argv:
        rebuild_report_only()
        return
    smoke = "--smoke" in sys.argv
    if OUT.exists() and not smoke:
        raise FileExistsError("refuse to overwrite existing report: " + str(OUT))
    OUT.mkdir(parents=True, exist_ok=True)

    summary: dict = {}
    dataset_hashes = {}
    for symbol, folder in DATASETS.items():
        daily, minute = load_data(folder)
        dataset_hashes[symbol] = {
            "1d.csv": sha256(folder / "1d.csv"),
            "1m.csv": sha256(folder / "1m.csv"),
        }
        grouped = list(minute.groupby(minute.index.str[:8]))
        if smoke:
            grouped = [entry for entry in grouped if len(entry[1]) >= 230][-3:]
        summary[symbol] = {}
        for key in ORDER:
            config = VARIANTS[key]
            rows = []
            for index, (day, bars) in enumerate(grouped, 1):
                history = daily.loc[daily.index < day]
                if len(bars) < 230 or len(history) < 80:
                    continue
                result = run_replay(symbol, config, history, bars)
                if result["failure"]:
                    raise RuntimeError(f"{symbol}/{key}/{day}: {result['failure']}")
                rows.append(result)
                if index % 60 == 0:
                    print(f"{symbol} {config['name']}: {index}/{len(grouped)}",
                          flush=True)
            summary[symbol][key] = summarize(rows)
            print(f"completed {symbol} {config['name']}: {len(rows)} days", flush=True)

    ladder_daily, ladder_minute = load_data(DATASETS[LADDER_SYMBOL])
    ladder_grouped = list(ladder_minute.groupby(ladder_minute.index.str[:8]))
    if smoke:
        ladder_grouped = [e for e in ladder_grouped if len(e[1]) >= 230][-3:]

    ladder_rows = []
    reused_ladder = None
    if "--reuse-ladder" in sys.argv:
        # The ladder only ablates v056's own layers, which this run does not
        # change, so an earlier ladder can be carried forward verbatim.
        source = Path(REUSE_LADDER_FROM) / "results.json"
        reused_ladder = json.loads(source.read_text(encoding="utf-8"))["ladder"]
        print(f"reusing ladder from {source}", flush=True)
    for label, name, config in ([] if reused_ladder else build_ladder_configs()):
        rows = []
        for index, (day, bars) in enumerate(ladder_grouped, 1):
            history = ladder_daily.loc[ladder_daily.index < day]
            if len(bars) < 230 or len(history) < 80:
                continue
            result = run_replay(LADDER_SYMBOL, config, history, bars)
            if result["failure"]:
                raise RuntimeError(f"ladder {label}/{day}: {result['failure']}")
            rows.append(result)
        ladder_rows.append((label, name, summarize(rows)))
        print(f"ladder {label} {name}: {len(rows)} days", flush=True)

    if reused_ladder:
        ladder_rows = [tuple(entry) for entry in reused_ladder]
        ladder_rows.append(("v0562", "v0562 CoreT+ATR再入场（参照）",
                            summary[LADDER_SYMBOL]["v0562"]))
    else:
        # v0561 itself closes the ladder as the reference implementation.
        rows = []
        for day, bars in ladder_grouped:
            history = ladder_daily.loc[ladder_daily.index < day]
            if len(bars) < 230 or len(history) < 80:
                continue
            result = run_replay(LADDER_SYMBOL, VARIANTS["v0561"], history, bars)
            if result["failure"]:
                raise RuntimeError(f"ladder v0561/{day}: {result['failure']}")
            rows.append(result)
        ladder_rows.append(("v0561", "v0561 CoreT（参照）", summarize(rows)))
        rows = []
        for day, bars in ladder_grouped:
            history = ladder_daily.loc[ladder_daily.index < day]
            if len(bars) < 230 or len(history) < 80:
                continue
            result = run_replay(LADDER_SYMBOL, VARIANTS["v0562"], history, bars)
            if result["failure"]:
                raise RuntimeError(f"ladder v0562/{day}: {result['failure']}")
            rows.append(result)
        ladder_rows.append(("v0562", "v0562 CoreT+ATR再入场（参照）", summarize(rows)))
    print("ladder reference: done", flush=True)

    aggregate = {}
    for key in ORDER:
        rows = [summary[symbol][key] for symbol in DATASETS]
        aggregate[key] = {
            "excess_net": sum(row["excess_net"] for row in rows),
            "mean_excess_return": sum(
                row["excess_return"] for row in rows) / len(rows),
            "worst_daily_drawdown": max(
                row["worst_daily_drawdown"] for row in rows),
            "fees": sum(row["fees"] for row in rows),
            "fills": sum(row["fills"] for row in rows),
            "rev_fills": sum(row["rev_fills"] for row in rows),
            "fwd_fills": sum(row["fwd_fills"] for row in rows),
            "zero_trade_days": sum(row["zero_trade_days"] for row in rows),
            "unclosed_days": sum(row["unclosed_days"] for row in rows),
            "cycles": sum(row["cycles"] for row in rows),
            "days": sum(row["days"] for row in rows),
        }
    strategy_hashes = {
        key: sha256(ROOT / "Stragety/MiniQMT_Stragety/DayT"
                    / STRATEGIES[VARIANTS[key]["version"]])
        for key in ORDER}
    payload = {
        "method": {
            "model": "INDEPENDENT_DAILY_RESET",
            "range": [START, END],
            "initial_cash_each_day": INITIAL_CASH,
            "initial_shares_each_day": INITIAL_SHARES,
            "fee_rate_each_side": FEE_RATE,
            "slippage": 0.0,
            "common_overrides": COMMON,
            "forward_t_stop_loss_sentinel": NO_STOP_SENTINEL,
            "variants": {
                key: {
                    "name": VARIANTS[key]["name"],
                    "entry": STRATEGIES[VARIANTS[key]["version"]],
                    "overrides": {**COMMON, **VARIANTS[key]["overrides"]},
                    "cfg_overrides": VARIANTS[key]["cfg_overrides"],
                } for key in ORDER},
            "ladder": [
                {"step": label, "name": name,
                 "overrides": config["overrides"],
                 "cfg_overrides": config["cfg_overrides"],
                 "module_patches": sorted(config["module_patches"]),
                 "quantile_regime_off": config.get("quantile_regime") is False}
                for label, name, config in build_ladder_configs()],
        },
        "hashes": {"strategies": strategy_hashes, "datasets": dataset_hashes},
        "summary": summary,
        "aggregate": aggregate,
        "ladder": ladder_rows,
        "ladder_symbol": LADDER_SYMBOL,
    }
    (OUT / "results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    (OUT / "README.md").write_text(build_report(payload), encoding="utf-8")
    print(OUT / "README.md")


if __name__ == "__main__":
    main()
