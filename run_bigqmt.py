"""Run QMT-expert MiniQMT strategies through the bundled Big QMT bridge."""

import argparse
import importlib.util
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent
# 桥接方式的调整默认路径，redis 或者 pipe
BRIDGE_SRC = ROOT / "integrations" / "bigqmt_pipe" / "src"
# BRIDGE_SRC = ROOT / "integrations" / "bigqmt_redis" / "src"

DEFAULT_STRATEGY = ROOT / "Stragety" / "MiniQMT_Stragety" / "DayT" / "DayTradeing_v41_stragety_miniqmt.py"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--strategy", type=Path, default=DEFAULT_STRATEGY)
    bridge_group = parser.add_mutually_exclusive_group()
    bridge_group.add_argument("--bridge", choices=("pipe", "redis"),
                              help="select bundled bridge (default: %s)" % BRIDGE_SRC.parent.name.removeprefix("bigqmt_"))
    bridge_group.add_argument("--bridge-src", type=Path,
                        help="use another bridge src directory, e.g. a Named Pipe-capable checkout")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="check local setup without connecting")
    group.add_argument("--probe", action="store_true", help="query bridge health, account and positions only")
    args, forwarded = parser.parse_known_args(argv)
    if forwarded[:1] == ["--"]:
        forwarded = forwarded[1:]
    if (args.check or args.probe) and forwarded:
        parser.error("strategy arguments cannot be combined with --check or --probe")

    bridge_src = args.bridge_src.expanduser().resolve() if args.bridge_src else BRIDGE_SRC
    if args.bridge:
        bridge_src = ROOT / "integrations" / ("bigqmt_" + args.bridge) / "src"
    if not (bridge_src / "xtquant" / "__init__.py").is_file():
        parser.error("bridge src must contain xtquant/__init__.py: %s" % bridge_src)
    sys.path.insert(0, str(bridge_src))
    # Verify BEFORE importing the adapter: a previously imported native SDK must
    # never service even one query from this explicitly Big QMT entry.
    import xtquant
    if Path(xtquant.__file__).resolve().parent != bridge_src / "xtquant":
        parser.error("native xtquant is already loaded; start this entry in a fresh Python process")

    launcher_src = ROOT / "integrations" / "bigqmt_pipe" / "src"
    if bridge_src != launcher_src:
        # This helper belongs to QMT-expert; the selected upstream checkout
        # supplies the bridge and shim, but need not ship our strategy launcher.
        import bigqmt_signal_trader
        name = "bigqmt_signal_trader.external_strategy_launcher"
        spec = importlib.util.spec_from_file_location(
            name, launcher_src / "bigqmt_signal_trader" / "external_strategy_launcher.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)

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
            parser.error("client config ACCOUNT must match the strategy config ACCOUNT; see integrations/bigqmt_pipe/README.md")
        transport = (config.get("redis_config") or {}).get("transport", "redis")
        dependency = {"zmq": "zmq", "redis": "redis", "mysql": "pymysql"}.get(transport)
        if dependency and importlib.util.find_spec(dependency) is None:
            parser.error("missing %s; install integrations/bigqmt_pipe/requirements.txt" % dependency)
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
