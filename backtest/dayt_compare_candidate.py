"""Run a registered candidate under the frozen independent-day comparison rules."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile
from io import BytesIO

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import pandas as pd
from backtest.dayt_benchmark import GOLDEN,verify
from backtest.dayt_registry import STRATEGIES
from output.analysis.compare_v51_v39_minute import replay,DAYT


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version',choices=list(STRATEGIES),required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.version in ('v39','v51'):
        raise ValueError('legacy versions must be reproduced using dayt_benchmark.py replay')
    if args.output.exists(): raise FileExistsError('candidate output already exists')
    verify(replay=True)
    with zipfile.ZipFile(GOLDEN/'snapshot.zip') as archive:
        daily=pd.read_csv(BytesIO(archive.read('analysis/v51_v39_minute/1d.csv')),dtype={'time':str}).set_index('time')
        minute=pd.read_csv(BytesIO(archive.read('analysis/v51_v39_minute/1m.csv')),dtype={'time':str}).set_index('time')
        golden=json.loads(archive.read('analysis/v51_v39_minute/results.json'))
    dates=sorted({row['date'] for row in golden['results']})
    source=DAYT/STRATEGIES[args.version]
    source_hash=hashlib.sha256(source.read_bytes()).hexdigest()
    rows=[]
    for day in dates:
        bars=minute.loc[minute.index.str[:8]==day]
        assert len(bars)==241, 'frozen complete-session shape changed'
        for slip in (0.,.01):
            result=replay(args.version,daily.loc[daily.index<day],bars,slip)
            if result['failure']: raise RuntimeError(str(result['failure']))
            rows.append(result)
    if hashlib.sha256(source.read_bytes()).hexdigest()!=source_hash:
        raise RuntimeError('candidate source changed during replay')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(dict(version=args.version,source_sha256=source_hash,
        model='FROZEN_COMPARISON_DAILY_RESET_NOT_CONTINUOUS',results=rows),indent=2),encoding='utf-8')
    for slip in (0.,.01):
        selected=[row for row in rows if row['slip']==slip]
        print(args.version,slip,'excess gross',sum(row['excess_gross'] for row in selected),
              'fills',sum(len(row['trades']) for row in selected),flush=True)


if __name__=='__main__': main()
