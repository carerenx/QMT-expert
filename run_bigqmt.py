"""Run QMT-expert MiniQMT strategies through the bundled Big QMT bridge."""

import argparse
import importlib.util
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent
BRIDGE_SRC = ROOT / "integrations" / "bigqmt" / "src"
DEFAULT_STRATEGY = ROOT / "Stragety" / "MiniQMT_Stragety" / "DayT" / "DayTradeing_v41_stragety_miniqmt.py"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--strategy", type=Path, default=DEFAULT_STRATEGY)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="check local setup without connecting")
    group.add_argument("--probe", action="store_true", help="query bridge health, account and positions only")
    args, forwarded = parser.parse_known_args(argv)
    if forwarded[:1] == ["--"]:
        forwarded = forwarded[1:]
    if (args.check or args.probe) and forwarded:
        parser.error("strategy arguments cannot be combined with --check or --probe")

    sys.path.insert(0, str(BRIDGE_SRC))
    # Verify BEFORE importing the adapter: a previously imported native SDK must
    # never service even one query from this explicitly Big QMT entry.
    import xtquant
    if Path(xtquant.__file__).resolve().parent != BRIDGE_SRC / "xtquant":
        parser.error("native xtquant is already loaded; start this entry in a fresh Python process")

    from bigqmt_signal_trader.external_strategy_launcher import (
        ExternalStrategyLaunchError, _load_strategy_account, launch_strategy,
    )
    from bigqmt_signal_trader.xtquant_compat import configure, load_client_config

    strategy = args.strategy.expanduser().resolve()
    if not strategy.is_file():
        parser.error("strategy file does not exist: %s" % strategy)
    try:
        account = _load_strategy_account(strategy)
        config = load_client_config()
        if str(config.get("account_id") or "") != account:
            parser.error("client config ACCOUNT must match the strategy core/config.py ACCOUNT; see integrations/bigqmt/README.md")
        transport = (config.get("redis_config") or {}).get("transport", "redis")
        dependency = {"zmq": "zmq", "redis": "redis", "mysql": "pymysql"}.get(transport)
        if dependency and importlib.util.find_spec(dependency) is None:
            parser.error("missing %s; install integrations/bigqmt/requirements.txt" % dependency)
        print("[BigQMT] strategy=%s\n[BigQMT] xtquant=%s\n[BigQMT] transport=%s" % (
            strategy, xtquant.__file__, transport), flush=True)
        if args.check:
            print("[BigQMT] local setup OK; server connectivity has NOT been checked")
            return 0
        if args.probe:
            trader, _ = configure(account_id=account)
            try:
                for method in ("ping", "query_stock_asset", "query_stock_positions"):
                    result = trader.client.call(method)
                    if method == "ping" and (
                        not isinstance(result, dict) or not result.get("pong")
                        or str(result.get("account_id") or "") != account
                    ):
                        raise RuntimeError("bridge ping did not confirm the strategy account")
                    print("[BigQMT] %s: %s" % (method, result))
            except Exception as exc:
                parser.exit(1, "[BigQMT] probe failed: %s\nStart the bridge inside Big QMT and check its account/transport configuration. No strategy was started.\n" % exc)
            return 0
        launch_strategy(strategy, forwarded)
    except (ExternalStrategyLaunchError, ModuleNotFoundError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
