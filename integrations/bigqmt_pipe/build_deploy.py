"""Build the standalone, read-only QMT Pipe model without starting QMT.

Run from the repository root: python integrations/bigqmt_pipe/build_deploy.py
Account: trading_account.txt. Configuration: src/*_local_config.example.py.
The upstream flat-module builder is reused; no network or pip is needed.
"""

import ast
import importlib.machinery
import importlib.util
from pathlib import Path
import pprint
import sys

from sync_account import sync_account


DIRECTORY = Path(__file__).resolve().parent


def load_builder(name):
    path = DIRECTORY / "src" / "bigqmt_signal_trader" / "_singlefile" / (name + ".py.txt")
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


def main():
    account_path = DIRECTORY.parents[1] / "trading_account.txt"
    accounts = [line.split("#", 1)[0].strip()
                for line in account_path.read_text(encoding="utf-8-sig").splitlines()]
    accounts = [account for account in accounts if account]
    if len(accounts) != 1 or not accounts[0].isascii() or not accounts[0].isdigit():
        raise ValueError("trading_account.txt must contain exactly one numeric account ID")

    base = load_builder("build_single_file")
    flat = load_builder("build_no_redis_single_file_flat")
    source_dir = DIRECTORY / "src"
    sources = base.collect_package(str(source_dir / "bigqmt_signal_trader"), str(source_dir))
    extra = {}
    for name in ("bigqmt_signal_trader_strategy.py", "bigqmt_signal_trader_redis_rpc_runtime.py"):
        extra[name] = (source_dir / name).read_bytes()

    config_path = source_dir / "bigqmt_signal_trader_local_config.example.py"
    config = {}
    for node in ast.parse(config_path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign):
            config[node.targets[0].id] = ast.literal_eval(node.value)
    config["BIGQMT_ACCOUNT_ID"] = accounts[0]
    rpc = config["BIGQMT_REDIS_CONFIG"]
    if (rpc["transport"] != "pipe" or rpc["redis_enabled"]
            or rpc["rpc_allow_order_methods"] or rpc["native_xtdata_enabled"]):
        raise ValueError("deployment template must use read-only Pipe with Redis/native xtdata disabled")

    template = flat.FLAT_TEMPLATE
    template = template[template.index("import builtins as _builtins"):]
    start = template.index('BIGQMT_ACCOUNT_ID = "YOUR_ACCOUNT_ID"')
    end = template.index("# ===================================================================", start)
    config_text = "\n\n".join(
        name + " = " + pprint.pformat(value, sort_dicts=False)
        for name, value in config.items()) + "\n"
    template = template[:start] + config_text + template[end:]
    template = template.replace('BIGQMT_REDIS_CONFIG["transport"] = "zmq"',
                                'BIGQMT_REDIS_CONFIG["transport"] = "pipe"')
    template = template.replace("# Force zmq transport (this is the no-redis version).",
                                "# Force Windows Named Pipe; never dial a Redis or ZMQ endpoint.")
    template = template.replace("no-redis mode: transport=zmq", "pipe mode: transport=pipe")
    template = template.replace("all-in-one no-redis strategy flat modules=", "all-in-one pipe strategy flat modules=")
    source = template.replace("__MODULE_FUNCS_BLOCK__", flat.module_funcs_block(sources, extra))
    header = '''# -*- coding: utf-8 -*-
# Build/check only: python integrations/bigqmt_pipe/build_deploy.py
# Account sync only: python integrations/bigqmt_pipe/sync_account.py
# Run: paste this ENTIRE file into QMT model PIPE_ASCII_ALL_IN_ONE; compile,
# then run in Model Trading without independent Python process mode.
# Read-only RPC via Windows Named Pipe bigqmt_rpc on the same computer.
# No screening/buy/sell logic; order/cancel, Redis and native xtdata disabled.
# Edit the config block below; external local_config.py is not loaded.
# Details: ../连接流程.md and ../README.md. UTF-8; do not convert to GBK.
# Bundled bridge: xtquant-big-convert 0.3.78 (MIT, ../LICENSE).

'''
    source = header + source
    compile(source, "PIPE_ASCII_ALL_IN_ONE.py", "exec")
    output = DIRECTORY / "deploy" / "PIPE_ASCII_ALL_IN_ONE.py"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(source, encoding="utf-8", newline="\n")
    sync_account(output, accounts[0])
    print("Built %s (%d embedded modules); account=%s; transport=pipe; orders=False"
          % (output, len(sources) + len(extra), accounts[0]))


if __name__ == "__main__":
    main()
