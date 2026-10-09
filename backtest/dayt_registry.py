"""Single local strategy registration seam; golden artifacts are never edited."""
import importlib.util
from pathlib import Path
# Different runtime interface: do not inject Policy objects into the legacy runner.
RESEARCH_STRATEGIES = {
    'long_hold_factorial': {
        'entry': 'Stragety/MyPy-Q/LongHoldRotation_v4_FactorialResearch.py',
        'runner': 'backtest/long_hold_ablation.py',
        'interface': 'AblationPolicy',
        'legacy_daily_reset_compatible': False,
    },
    'profit_priority_fusion': {
        'entry': 'Stragety/MyPy-Q/LongHoldSwing_v1_ProfitPriority.py',
        'runner': 'backtest/profit_priority_research.py',
        'interface': 'Policy',
        'legacy_daily_reset_compatible': False,
    },
}

STRATEGIES = {
    'v39': 'DayTradeing_v39_stragety_miniqmt.py',
    'v51': 'DayT_v51_IntradayStrength.py',
    'v52': 'DayT_v52_DirectionalOvernight.py',
    'v39_nomom': 'DayTradeing_v39_nomom.py',
    'v39_v40_nomom': 'DayTradeing_v40_nomom_BaseRecovery.py',
    'v39_nomom_continuous_carry': 'DayTradeing_v39_nomom_ContinuousCarry.py',
    'v51_nomom': 'DayT_v51_nomom.py',
    'v52_nomom': 'DayT_v52_nomom.py',
    'v53_nomom': 'DayT_v53_nomom_CycleRiskExit.py',
    'v54_nomom': 'DayT_v54_nomom_TrendGuard.py',
    'v55_nomom': 'DayT_v55_nomom_NoOvernightMomentumGuard.py',
    'v56_nomom': 'DayT_v56_nomom_ConfirmedReversalRiskBudget.py',
    'v056_nomom': 'DayT_v056_nomom_SimplifiedReversal.py',
    'v0561': 'DayT_v0561_CoreT.py',
    'v0562': 'DayT_v0562_CoreT_ATRReentry.py',
    'v0563': 'DayT_v0563_CoreT_ATRReentry_TrendGuard.py',
    'v0564': 'DayT_v0564_DualSignalRisk.py',
    'v0565': 'DayT_v0565_EpochPeakMultiVoteRisk.py',
    'capture_v1': 'CaptureT_v1_RangeCapture.py',
    'capture_v2': 'CaptureT_v2_Symmetric.py',
    'capture_v3': 'CaptureT_v3_ForwardOnly.py',
    'capture_v4': 'CaptureT_v4_TrendGuarded.py',
    'v57_nomom': 'DayT_v57_nomom_RestartGuard.py',
    'v58_nomom': 'DayT_v58_nomom_StagedDrawdownDeRisk.py',
}

# External RedisQMT strategies use a different runner interface from the
# generator-based MiniQMT replay.  The entry is registered here without being
# injected into the legacy loader, so offline tests can supply a fake adapter
# and live Redis is never contacted by a backtest.
REDIS_STRATEGIES = {
    'dt_v1': {
        'entry': 'Stragety/RedisQMT/DT/DT_v1.py',
        'interface': 'StrategyRunner',
        'adapter': 'Stragety.RedisQMT.Common.redis_qmt.RedisQmtAdapter',
        'legacy_daily_reset_compatible': False,
    },
}


def load_redis_strategy(name, adapter, logger=None):
    """Load an external strategy against an explicitly supplied safe adapter."""
    registration = REDIS_STRATEGIES[name]
    root = Path(__file__).resolve().parents[1]
    path = root / registration['entry']
    spec = importlib.util.spec_from_file_location('redis_dayt_' + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.StrategyRunner(adapter=adapter, logger=logger)


def register_with_legacy_loader():
    from output.analysis import compare_v51_v39_minute as loader
    loader.FILES.update(STRATEGIES)
    return loader
