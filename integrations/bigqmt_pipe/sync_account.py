"""Sync trading_account.txt into a QMT single-file model without running it.

python integrations/bigqmt_pipe/sync_account.py
python integrations/bigqmt_pipe/sync_account.py --qmt-source C:/QMT/python/PIPE_ASCII_ALL_IN_ONE.py
Only the top-level account assignment changes; runtime permissions stay intact.
"""

import argparse
from pathlib import Path
import re


def sync_account(source, account):
    if not account or not account.isascii() or not account.isdigit():
        raise ValueError("trading_account.txt must contain one numeric account ID")
    original = source.read_bytes()
    updated, count = re.subn(
        rb"(?m)^BIGQMT_ACCOUNT_ID[ \t]*=[ \t]*(['\"])[^\r\n]*?\1[ \t]*(?=\r?$)",
        b'BIGQMT_ACCOUNT_ID = "' + account.encode("ascii") + b'"',
        original,
    )
    if count != 1:
        raise ValueError("expected exactly one top-level BIGQMT_ACCOUNT_ID assignment")
    if updated != original:
        source.write_bytes(updated)


def main():
    directory = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qmt-source", type=Path,
                        default=directory / "deploy" / "PIPE_ASCII_ALL_IN_ONE.py")
    args = parser.parse_args()
    account_lines = (directory.parents[1] / "trading_account.txt").read_text(encoding="utf-8-sig").splitlines()
    accounts = [line.split("#", 1)[0].strip() for line in account_lines]
    accounts = [account for account in accounts if account]
    if len(accounts) != 1:
        parser.error("trading_account.txt must contain exactly one uncommented numeric account ID")
    account = accounts[0]
    sync_account(args.qmt_source, account)
    print("Account %s synced to %s. Reload the model in QMT to apply." % (account, args.qmt_source))


if __name__ == "__main__":
    main()
