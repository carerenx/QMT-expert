"""Finite, predeclared development experiment. Never optimizes on the last 30 days."""
import argparse
import json
import hashlib
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists(): raise FileExistsError('use a new experiment directory; existing results are immutable')
    args.output.mkdir(parents=True)
    paths=[ROOT/'backtest/dayt_strict.py',ROOT/'backtest/dayt_exchange.py',
           ROOT/'Stragety/MiniQMT_Stragety/DayT/DayT_v52_DirectionalOvernight.py',
           ROOT/'Stragety/MiniQMT_Stragety/core/directional_overnight.py']
    hashes={str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    (args.output/'input_hashes.json').write_text(json.dumps(hashes,indent=2),encoding='utf-8')
    experiments=[('v39', 'v39', []),('v51','v51',[]),
                 ('overnight_only','v52',['--no-directional']),
                 ('long_disabled','v52',['--no-directional','--no-long'])]
    for value in ('0','0.2','0.4'):
        experiments.extend([('direction_'+value,'v52',['--threshold',value,'--no-overnight']),
                            ('combined_'+value,'v52',['--threshold',value])])
    results=[]
    for name,version,flags in experiments:
        output=args.output/(name+'.json')
        command=[sys.executable,str(ROOT/'backtest/dayt_strict.py'),'--version',version,'--train',
                 '--output',str(output),*flags]
        print('TRAIN ONLY',name,flush=True)
        subprocess.run(command,cwd=ROOT,check=True)
        if any(hashlib.sha256(Path(path).read_bytes()).hexdigest()!=value for path,value in hashes.items()):
            raise RuntimeError('research inputs changed during run; do not publish this experiment')
        results.append(dict(name=name,**json.loads(output.read_text(encoding='utf-8'))))
    baseline=next(row for row in results if row['name']=='v51')
    eligible=[row for row in results if row['version']=='v52' and not row['failure'] and not baseline['failure']
              and row['maximum_drawdown']<=baseline['maximum_drawdown']
              and row['unfinished_abs_peak']<=baseline['unfinished_abs_peak']]
    eligible.sort(key=lambda row:(-row['excess_net'],row['maximum_drawdown'],row['capital_occupation_peak'],
                                 -row['settings'].get('DIRECTIONAL_THRESHOLD',0)))
    choice=dict(selected=eligible[0]['name'] if eligible else None,
                reason='ranked development candidate' if eligible else 'No eligible candidate; invalid baseline or risk constraints failed',
                holdout_status='NOT_RUN_BY_THIS_COMMAND',
                caution='99 sessions were previously observed; not a genuinely unseen out-of-sample test')
    (args.output/'selection.json').write_text(json.dumps(choice,indent=2),encoding='utf-8')
    print(choice,flush=True)


if __name__=='__main__': main()
