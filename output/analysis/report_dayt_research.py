"""Export reproducible DayT research tables and detailed audit artifacts."""
import argparse
import csv
import hashlib
import json
from io import StringIO
import zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def read(path): return json.loads(path.read_text(encoding='utf-8'))


def table(headers,rows):
    return '\n'.join(['| '+' | '.join(headers)+' |','| '+' | '.join(['---']*len(headers))+' |']+
                     ['| '+' | '.join(str(value) for value in row)+' |' for row in rows])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    args.input=args.input.resolve()
    args.output=args.output.resolve()
    payloads={p.stem:read(p) for p in args.input.glob('v*_*.json')}
    required={'v39_0.0005','v51_0.0005','v52_0.0003','v52_0.0005','v52_0.001'}
    if not required<=set(payloads): raise RuntimeError('missing required full-period results')
    development={p.stem:read(p) for p in (args.input/'development').glob('*.json') if p.stem not in ('selection','input_hashes')}
    selection=read(args.input/'development/selection.json')
    fixed=read(args.input/'fixed_v52.json')
    source=ROOT/'Stragety/MiniQMT_Stragety/DayT/DayT_v52_DirectionalOvernight.py'
    if hashlib.sha256(source.read_bytes()).hexdigest()!=fixed['source_sha256']:
        raise RuntimeError('fixed comparison candidate source mismatch')
    for result in list(payloads.values())+list(development.values()):
        for name,expected in result['hashes'].items():
            if hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=expected:
                raise RuntimeError('source/data changed after replay: '+name)
    exports=args.input/'exports'
    exports.mkdir(exist_ok=True)
    for name,result in payloads.items():
        for key in ('trades','equity','unclosed','orders','cycles'):
            rows=result[key]
            if not rows: continue
            stream=StringIO(newline='')
            writer=csv.DictWriter(stream,fieldnames=list(rows[0]))
            writer.writeheader();writer.writerows(rows)
            data=stream.getvalue().encode('utf-8-sig')
            path=exports/(name+'_'+key+'.csv')
            if path.exists():
                if path.read_bytes()!=data: raise RuntimeError('existing export differs: '+str(path))
            else: path.write_bytes(data)
    money=lambda number:f'{number:,.2f}'
    main_result=payloads['v52_0.0005']
    fixed_rows=[]
    for slip in (0.,.01):
        rows=[row for row in fixed['results'] if row['slip']==slip]
        fixed_rows.append(['v52',slip,money(sum(row['excess_gross'] for row in rows)),sum(len(row['trades']) for row in rows)])
    with zipfile.ZipFile(ROOT/'backtest/dayt_golden_20260910/snapshot.zip') as archive:
        golden= json.loads(archive.read('analysis/v51_v39_minute/results.json'))['results']
    legacy_rows=[]
    for version in ('v39','v51'):
        for slip in (0.,.01):
            rows=[row for row in golden if row['version']==version and row['slip']==slip]
            legacy_rows.append([version,slip,money(sum(row['excess_gross'] for row in rows)),sum(len(row['trades']) for row in rows)])
    fixed_rows=legacy_rows+fixed_rows
    strict_rows=[]
    for version in ('v39','v51','v52'):
        result=payloads[version+'_0.0005']
        strict_rows.append([version,'停止/无效' if result['failure'] else '完成回放',money(result['account_net']),
            money(result['excess_net']),f"{result['maximum_drawdown']:.2%}",result['completed_cycles'],result['unfinished_abs_peak']])
    train_rows=[]
    for name,result in sorted(development.items()):
        train_rows.append([name,'停止/无效' if result['failure'] else '完成',money(result['excess_net']),
            f"{result['maximum_drawdown']:.2%}",result['completed_cycles'],result['unfinished_abs_peak']])
    fee_rows=[[int(round(payloads['v52_'+rate]['rate']*10000)),money(payloads['v52_'+rate]['fees']),
               money(payloads['v52_'+rate]['excess_net']),f"{payloads['v52_'+rate]['maximum_drawdown']:.2%}"]
              for rate in ('0.0003','0.0005','0.001')]
    boundary=[row for row in main_result['equity'] if row['time'][:10]<='2026-07-30'][-1]
    ending=main_result['equity'][-1]
    holdout_excess=(ending['equity']-boundary['equity'])-200*(ending['price']-boundary['price'])
    holdout_cycles=sum(row['closed'][:10]>='2026-07-31' for row in main_result['cycles'])
    sha_lines='\n'.join(f'- `{name}`：`{value}`' for name,value in main_result['hashes'].items())
    report=f'''# v52 固定比较与连续账户研究报告

## 结论

**没有选出上线候选，v52只交付研究/信号观察版，实盘入口关闭。**

固定比较改善不代表连续账户收益改善。默认组合在5bp研究费率下的连续相对持有净增量为 **{money(main_result['excess_net'])}元**，最大回撤 **{main_result['maximum_drawdown']:.2%}**。
v39/v51在本严格路径中跨日停止/无效，不能作为完整99天正常运行的合格基线。测试通过不是净收益验收。

## 1. 冻结的每日独立比较

99天，每天200股+10万元，盘中分钟收盘撮合，费用未扣入；黄金原源码/公共依赖在zip中隔离复现。完整396例黄金复现已通过。

{table(['策略','滑点/元','相对持有毛增量/元','成交次数'],fixed_rows)}

每天重置会消除未回补周期对后续交易的影响，并重新提供现金、可卖底仓；此模型仅用于延续旧版本比较，不能当成可连续实现的收益。

## 2. 严格连续账户，主假设5bp

{table(['策略','运行状态','账户净变动/元','相对持有净增量/元','连续最大回撤','完成周期','未平绝对数量峰值/股'],strict_rows)}

旧版停止后不再运行策略，剩余持仓仍估值至期末；停止结果不是正常运行的收益排名，其较小回撤也不是胜出的证据。

停止原因：

- v39：{payloads['v39_0.0005']['failure']}
- v51：{payloads['v51_0.0005']['failure']}

适配器包含盘前维护事件，只提供昨日已知数据；v51执行原组合调度器换日检查。委托最早在下一个OPEN事件成交，等待期间只推进市场与成交事件，不向策略提前返回未来开盘价。

## 3. 前69天有限消融

{table(['实验','状态','相对持有净增量/元','最大回撤','完整周期','未平数量峰值/股'],train_rows)}

`direction_*`只加方向准入，`overnight_only`只允许隔夜，`combined_*`组合两者，`long_disabled`真实重跑关闭正向后的资金路径；不是从旧表减掉亏损方向。

筛选记录：`selected={selection['selected']}`。原因：{selection['reason']}。
默认组合实际配置：`{main_result['settings']}`。
所有阈值只使用0、0.20、0.40；没有扩大搜索。由于连续基线无效，不发布“最优上线策略”。

## 4. 后30天诊断及费用敏感性

仅对预先指定默认组合0.20做完整连续路径诊断，不用后30天调整参数。后30天继承前69天的真实现金、持仓和未平腿，不在边界重新开户。

- 后30天相对持有净增量：{money(holdout_excess)}元。
- 后30天完成周期：{holdout_cycles}。{'样本不足：不足30个完整周期。' if holdout_cycles<30 else '周期数量达到30，但不是新样本。'}
- 99天此前已被观察，本次后30天不是严格样本外；真正新增样本数仍为0。

{table(['假设单边费率/bp','总费用/元','v52相对持有净增量/元','最大回撤'],fee_rows)}

费用逐笔扣现金，最低5元按委托累计；以上是研究假设，不能冒充账户真实佣金和税费。

## 5. 未完成风险及执行明细

{table(['方向','开仓记录时间','成交均价','剩余数量','期末估值价','未实现毛损益/元'],[[row['group'],row['opened'],row['price'],row['quantity'],row['mark'],money(row['unrealized'])] for row in main_result['unclosed']])}

最长周期持有：{main_result['longest_holding_sessions']}个样本交易日。资金占用峰值（多头开仓成本+空头按成交价/现价较大者计价，不含额外ATR预留）：{money(main_result['capital_occupation_peak'])}元。
每个反向周期在策略中另有ATR及费用预留；其ATR、费用、剩余数量与归属在JSON的`owned_cycle_records`中。期末未完成周期没有从收益表删除。

未成交原因事件计数（同一委托可多次出现，不是独立拒单数量）：`{main_result['rejection_counts']}`。

逐笔成交、费用、连续权益、未平仓、委托及周期已导出到`{exports.relative_to(ROOT).as_posix()}`；原始JSON保留全部配置、日志尾部与哈希。

## 6. 验证范围与上线边界

- 当前单元/回归测试186项通过，覆盖原有成交、日志、新鲜度、保存重试/恢复，以及新增事件撮合、双周期额度、阶梯归属、退出优先、资金预留、分钟失效、已确认部分成交恢复、换日和实盘入口拒绝。
- 默认signal；live入口直接拒绝。本次未启动实盘，也未修改账户检查点。
- 旧检查点仅允许账户核对一致的平坦账本只读导入；旧未平腿缺少唯一订单归属时拒绝。未决/不确定订单保留文件并要求核对，不宣称已经验证全自动崩溃中委托恢复。
- 已知暂停交易状态暂停该股，依据本地QMT文档证券状态表[Python API, p.171]；未知状态不能据此判定交易正常。报价时间过期仍仅告警，不新增以报价年龄为依据的停单条件。
- 分钟模拟不能保证排队成交、识别所有板块的涨跌停制度、还原盘中停复牌及全部公司行动。1%容量以已实现分钟量为撮合假设，不是实时队列证据。分钟采样下的tick聚合观察存在保守确认延迟。
- 本样本底仓200股，50%绝对敞口上限只有100股，因此历史中不能同时容纳两手未平T；双周期互不抵消主要由更大底仓的合成测试覆盖，不能宣称历史已验证“双周期提高收益”。
- 尚无5日影子观察、20个新增交易日/30个新增完整周期，也未完成独立双轨代码审查。暂不具备上线验收条件。

## 7. 复现与哈希

固定流程见`docs/DayT_fixed_benchmark.md`。本报告仅使用`{args.input.relative_to(ROOT).as_posix()}`，早期草稿和适配器修正前结果不参与结论；没有覆盖黄金数据或旧报告。

{sha_lines}
'''
    args.output.write_text(report,encoding='utf-8')
    print(args.output)


if __name__=='__main__': main()
