# -*- coding: utf-8 -*-
"""Fetch one public daily-data archive and inspect headers; repository root:
python Stragety/RedisQMT/Trend100/sources/download_daily_snapshot.py

FREE_DATA_v0 raw daily/reference archive, <=1.1GB, published SHA256 checked.
No minute backtest, Redis/QMT, executable extraction or bundled code execution.
Archive contents and vendor claims still need independent checks.
See ../AlternativeDataSources.md.
"""
import csv
import hashlib
import json
from pathlib import Path
import tarfile

import requests


ROOT = Path(__file__).resolve().parents[4]
OUT = ROOT / 'output/analysis/trend100_alternative_sources_20261102'
URL = 'https://github.com/noblechang/a-share-data/releases/download/FREE_DATA_v0/a-share-raw-v0.tar'
EXPECTED_SHA = '9e5d9d7386f695f5b255b4d44adb435aec410c5df4276f8db44be111deed0d35'


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / 'a-share-raw-v0.tar'
    record = {'source': URL, 'release': 'FREE_DATA_v0', 'expected_sha256': EXPECTED_SHA,
              'redis_calls': 0, 'orders': 0, 'certified_for_backtest': False}
    try:
        if not target.exists():
            temporary = target.with_suffix('.tar.part')
            if temporary.exists():
                raise ValueError('Existing partial archive: inspect prior download before restarting')
            response = requests.get(URL, timeout=(5, 25), stream=True)
            response.raise_for_status()
            size = 0
            announced = 0
            with temporary.open('wb') as stream:
                for chunk in response.iter_content(1024 * 1024):
                    size += len(chunk)
                    if size > 1_100_000_000:
                        raise ValueError('Archive exceeds 1.1GB bound')
                    stream.write(chunk)
                    if size - announced >= 64 * 1024 * 1024:
                        print(json.dumps({'downloaded_bytes': size}), flush=True)
                        announced = size
            digest = sha256(temporary)
            if digest != EXPECTED_SHA:
                raise ValueError('Archive SHA256 does not match release manifest')
            temporary.rename(target)
        else:
            digest = sha256(target)
            if digest != EXPECTED_SHA:
                raise ValueError('Existing archive SHA256 mismatch')
        record.update(bytes=target.stat().st_size, sha256=digest, checksum_matches=True)
        with tarfile.open(target, mode='r:') as archive:
            members = archive.getmembers()
            with (OUT / 'github_daily_tar_inventory.csv').open('w', encoding='utf-8', newline='') as stream:
                writer = csv.writer(stream)
                writer.writerow(['name', 'size', 'regular_file'])
                writer.writerows((member.name, member.size, member.isfile()) for member in members)
            record['members'] = len(members)
            record['first_members'] = [{'name': member.name, 'size': member.size} for member in members[:15]]
    except Exception as error:
        record['error_type'] = type(error).__name__
        record['error'] = str(error) if isinstance(error, ValueError) else 'Download or archive inspection failed; no dataset certified'
    (OUT / 'github_daily_download.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(record, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
