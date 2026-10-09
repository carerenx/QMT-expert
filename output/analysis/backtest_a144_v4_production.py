"""Consolidate the production-gated A144 v4 backtest evidence."""
from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from output.analysis.backtest_a144_v4_candidate_replacement import current_config
from output.analysis.research_a144_redisqmt import add_features
from output.analysis.research_a144_redisqmt import load_panel
from output.analysis.research_a144_redisqmt import run_backtest
from Stragety.RedisQMT.A144 import config_v4


OUTPUT_DIR = ROOT / "analysis" / "a144_v4_production_backtest_20260924"
V3_SUMMARY = (
    ROOT / "analysis" / "a144_v3_reverse_t_backtest_20260923" /
    "sensitivity_5m" / "summary.json")
PARAMETER_SUMMARY = (
    ROOT / "analysis" / "a144_v4_dayt_parameter_screen_20260924" /
    "summary.json")
RANKING_SUMMARY = (
    ROOT / "analysis" / "a144_v4_replacement_ranking_20260924" /
    "summary.json")
CANDIDATE_SUMMARY = (
    ROOT / "analysis" / "a144_v4_candidate_backtest_20260924" /
    "summary.json")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    frame = add_features(load_panel())
    daily_metrics, _, trades = run_backtest(
        frame,
        current_config(),
        "20220104",
        "20260918")
    v3 = read_json(V3_SUMMARY)
    parameter_screen = read_json(PARAMETER_SUMMARY)
    ranking_screen = read_json(RANKING_SUMMARY)
    candidate_study = read_json(CANDIDATE_SUMMARY)
    daily_window = v3["comparison"]["v2_base_window"]
    old_overlay = v3["comparison"]["v3_additive_window"]
    summary = {
        "production_profile": {
            "daily_live_enabled": True,
            "dayt_live_enabled": bool(config_v4.DAYT_LIVE_ENABLED),
            "dayt_signal_enabled": True,
            "reason": (
                "DayT parameter and replacement ranking screens failed "
                "out-of-sample gates"),
        },
        "daily_full_backtest": daily_metrics,
        "overlay_comparison_window": {
            "period": v3["method"]["period"],
            "v4_daily_only": daily_window,
            "v3_with_dayt": old_overlay,
            "v4_minus_v3_final_equity": (
                daily_window["final_equity"] -
                old_overlay["final_equity"]),
            "v4_minus_v3_total_return": (
                daily_window["total_return"] -
                old_overlay["total_return"]),
            "v4_minus_v3_max_drawdown": (
                daily_window["max_drawdown"] -
                old_overlay["max_drawdown"]),
            "v4_minus_v3_sharpe": (
                daily_window["sharpe"] -
                old_overlay["sharpe"]),
        },
        "rejected_dayt_parameter_candidate": {
            "selected_passes_validation": parameter_screen[
                "selected_passes_validation"],
            "baseline_validation_net": parameter_screen[
                "baseline"]["validation_net_pnl"],
            "candidate_validation_net": parameter_screen[
                "selected"]["validation_net_pnl"],
            "baseline_validation_bad_rate": parameter_screen[
                "baseline"]["validation_bad_rate"],
            "candidate_validation_bad_rate": parameter_screen[
                "selected"]["validation_bad_rate"],
        },
        "rejected_replacement_ranking_candidate": {
            "selected_variant": ranking_screen["selected"]["variant"],
            "selected_passes_holdout": ranking_screen[
                "selected_passes_holdout"],
            "baseline_holdout_20d_mean": ranking_screen[
                "baseline"]["holdout_20d_mean"],
            "candidate_holdout_20d_mean": ranking_screen[
                "selected"]["holdout_20d_mean"],
            "baseline_holdout_20d_median": ranking_screen[
                "baseline"]["holdout_20d_median"],
            "candidate_holdout_20d_median": ranking_screen[
                "selected"]["holdout_20d_median"],
        },
        "candidate_event_study": candidate_study,
        "deployment_gate": "daily-live-only",
        "trade_count": int(daily_metrics["trades"]),
        "limitations": [
            "current CSI500 snapshot has survivorship bias",
            "DayT evidence has only 15.3% symbol-session coverage",
            "DayT replay is five-minute sensitivity, not strict one-minute",
            "candidate event study is not a continuous-account backtest",
        ],
    }
    (OUTPUT_DIR / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
