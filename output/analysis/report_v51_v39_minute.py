"""Render the completed replay, retaining losing and unclosed days."""
import json
from pathlib import Path
import sys
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'Stragety/MiniQMT_Stragety'))
from core.execution_book import ExecutionBook

DIR=ROOT/'analysis/v51_v39_minute'
data=json.loads((DIR/'results.json').read_text(encoding='utf-8'))
rows=data['results']
assert len(rows)==396 and not any(r['failure'] for r in rows)
daily=pd.read_csv(DIR/'1d.csv',dtype={'time':str}).set_index('time')


def summarize(group):
    cycles=[v for r in group for v in r['cycle_gross']]
    return dict(days=len(group), gross=sum(r['account_gross'] for r in group),
        hold=sum(r['hold_gross'] for r in group), excess=sum(r['excess_gross'] for r in group),
        trades=sum(len(r['trades']) for r in group), turnover=sum(r['turnover'] for r in group),
        active=sum(bool(r['trades']) for r in group), cycles=len(cycles),
        wins=sum(v>0 for v in cycles), cycle_gross=sum(cycles),
        unclosed=sum(any(r['ledger_unclosed'].values()) for r in group),
        positive=sum(r['excess_gross']>0 for r in group),
        worst=min(r['excess_gross'] for r in group),
        drawdown=max(r['max_drawdown'] for r in group))


def select(version,slip=0): return [r for r in rows if r['version']==version and r['slip']==slip]
def money(v): return f'{v:,.2f}'
def table(headers, lines):
    return '\n'.join(['| '+' | '.join(headers)+' |','| '+' | '.join(['---']*len(headers))+' |']+['| '+' | '.join(map(str,line))+' |' for line in lines])


sections=['''# v51 与 v39：真实1分钟数据回放比较

## 1. 结论与适用范围

**结果状态：已完成99个完整交易日、两版策略、两档滑点共396个独立日实验；不是连续实盘收益验收。**

在本次同口径模拟中，v39的毛收益高于v51，但两版均跑输对应的日内持有基线。v51的收盘未平仓天数较少，但这没有转化为更好的收益。不能据此宣布v39更适合实盘，或v51的新机制提高了收益。

报告中的“累计”是每天恢复相同初始账户后，将99个日实验相加的统计值；**不是投入一笔资金后连续运行的终值，也不是复利或年化收益率**。含未平仓的日期全部保留并按当日收盘估值。

## 2. 数据和实验设置

- 标的：两版共同标的长飞光纤601869.SH；不包含v51多标的组合分散效果。
- 来源：本地BigQMT只读行情桥接，1m共24,000行，日线400行，均为不复权数据。
- 完整日范围：2026-04-21至2026-09-10，共99天、每天241条记录，包含09:30记录；09:30条作为源数据保留，不假设是普通连续竞价成交。
- 排除2026-04-20：只有141条，缺少开盘阶段。完整日分钟聚合开、高、低、收与日线在0.02元以内一致，成交量汇总一致；无重复时间、空值或周末记录。
- 两版每天初始均为**200股可卖底仓、现金100,000元**；底仓成本用当日开盘，仅作模拟输入，现金和底仓未沿用实盘账户。
- 研究区间按日期划分：前69天（2026-04-21—07-30）检查；后30天（2026-07-31—09-10）留出式报告。参数不按结果优化，但v51本身近期参考过行情，**不能把该划分称为严格未见样本或真正样本外验证**。
- v51保持文件当前配置：首轮缩放0.60，后续缩放0.80，盘中强度shadow。影子强度不控制交易，原有盘中反弹规则仍可影响执行价。
- v39保留当前文件逻辑和共享core/config.py配置，不代表2026年8月某次部署时所有依赖的历史快照。
- 两版都保持原始交易手数规则：v51按目标金额/底仓动态计算，v39固定手数；相同账户不等于强行设成相同每笔数量。
- 当前配置关闭强制尾盘平仓，不擅自打开；未回补、未卖回都按收盘市值计入风险。

## 3. 成交模型与重要限制

实际载入两版策略源代码。v51执行原generator循环；v39仅在内存中把run循环中的sleep改为yield，不修改策略源文件的判断逻辑。日期、行情、账户、下单、日志、检查点接口被离线替身替换，不连接交易服务或写实盘状态。

每个一分钟记录完成后运行一次策略，使用当时可见的收盘价；日线严格截断到该日之前，日内开盘/高低点/成交额与成交量累计均只取当时已完成记录。分钟内高低先后顺序不构造。

**基线是理想化“分钟收盘决策并按该收盘成交”模型，不是下一分钟开盘成交模型。** 这避免把下一分钟价格提前反馈给原策略，但忽略了真实报单延迟、价差、盘口深度和排队。零成交量记录不模拟成交；限价单只有在当前价格满足限价时成交，未成交请求按本次模拟撤销，不模拟长时间排队。COMPETE等非FIX委托近似为立即可成交委托。

一分钱不利滑点情景分别抬高买价/降低卖价；FIX成交不得越过委托限价，因此部分FIX不承担完整一分钱。滑点可能改变随后阈值、退出时点，而不是简单扣除每股一分钱。

T+1约束：当天买入不增加可卖数量，每次卖出扣减原有可卖数量。没有成交量参与率限制、停牌/涨跌停队列模型、逐笔盘口或真实交易费用。因此这里是**一分钟粒度的可复现比较实验，不是精确复刻亚分钟实盘**。两版含秒级回撤和MOM窗口，分钟采样会漏掉部分事件。

连续运行未在本报告中模拟：v51遇到跨日未平仓腿会要求人工核对。每日重置仅用于标准化日实验，不能推导“次日自动无风险继续做T”。

## 4. 全样本收益与风险（零滑点、未扣费用）
''']
a,b=summarize(select('v51')),summarize(select('v39'))
metrics=[('独立实验日数','days'),('账户毛收益合计（元）','gross'),('持有基线日内收益合计（元）','hold'),
 ('相对持有增量合计（元）','excess'),('成交笔数（单边）','trades'),('成交额合计（元）','turnover'),
 ('有成交天数','active'),('完整周期数（含MOM）','cycles'),('已完成周期毛收益合计（元）','cycle_gross'),
 ('收盘仍有交易腿的天数','unclosed'),('相对持有增量为正的天数','positive'),('最差单日相对持有增量（元）','worst')]
sections.append(table(['指标','v51','v39'],[(title,money(a[key]) if key in ('gross','hold','excess','turnover','cycle_gross','worst') else a[key],money(b[key]) if key in ('gross','hold','excess','turnover','cycle_gross','worst') else b[key]) for title,key in metrics]))
sections.append(f"\n已完成周期胜率：v51 {a['wins']}/{a['cycles']}={a['wins']/a['cycles']:.2%}；v39 {b['wins']}/{b['cycles']}={b['wins']/b['cycles']:.2%}。这不是全部交易胜率，未完成腿单独估值，不能用高闭环胜率掩盖尾部风险。\n\n最差单日账户峰谷回撤：v51 {a['drawdown']:.2%}，v39 {b['drawdown']:.2%}；基于分钟估值、从首条采样起算，**不是99日连续账户最大回撤**。\n")
sections.append('## 5. 检查区间与留出式区间\n')
lines=[]
for version in ('v51','v39'):
    for name,subset in [('前69天',select(version)[:69]),('后30天',select(version)[69:])]:
        s=summarize(subset)
        lines.append([version,name,money(s['gross']),money(s['hold']),money(s['excess']),s['cycles'],s['unclosed']])
sections.append(table(['版本','区间','账户毛收益','持有基线','相对持有','完整周期','未平仓天数'],lines))
sections.append('\n后30天两版均累计51个完整周期，达到20日/30周期的数量门槛，但收益均未超过持有，且不是严格未见行情验证。**不满足收益改善验收，不能据此启用v51 active。**\n')
sections.append('## 6. 分交易方向：已实现与未平仓贡献\n')
lines=[]
for version in ('v51','v39'):
    totals={}
    for row in select(version):
        book=ExecutionBook(); realized={}
        for i,t in enumerate(row['trades']):
            label=t['label']; short=label=='REV-T sell' or 'buyback' in label or label=='MOM short'
            group=('MOM ' if label.startswith('MOM') else '')+('SHORT' if short else 'LONG')
            gross,_,_=book.record(i,label,t['shares'],t['price'])
            realized[group]=realized.get(group,0)+gross
        total=0
        for group,legs in book.legs.items():
            unrealized=sum(((entry-daily.loc[row['date'],'close']) if group.endswith('SHORT') else (daily.loc[row['date'],'close']-entry))*q for entry,q in legs)
            values=totals.setdefault(group,[0.,0.])
            values[0]+=realized.get(group,0);values[1]+=unrealized
            total+=realized.get(group,0)+unrealized
        assert abs(total-row['excess_gross'])<.001, (version,row['date'],total,row['excess_gross'])
    for group,(realized,unrealized) in totals.items(): lines.append([version,group,money(realized),money(unrealized),money(realized+unrealized)])
sections.append(table(['版本','方向','实际模拟成交已实现毛收益','日末未平腿估值','相对持有贡献'],lines))
sections.append('\nSHORT=主策略反T，LONG=主策略正T，MOM为短线动量交易腿。按实际模拟成交FIFO统一核算，而不是直接采用旧版策略内部的参考价收益。每个实验日上述各组之和均与现金/持仓得到的相对持有增量一致，已逐日断言核对。\n')
sections.append('## 7. 滑点与费用敏感性\n')
lines=[]
for version in ('v51','v39'):
    for slip in (0.,.01):
        s=summarize(select(version,slip))
        lines.append([version,slip,money(s['excess']),money(s['excess']-s['turnover']*.0002),money(s['excess']-s['turnover']*.0005),money(s['excess']-s['turnover']*.001)])
sections.append(table(['版本','不利滑点元/股','零费增量','总费用假设2bp后增量','5bp后增量','10bp后增量'],lines))
sections.append('''
这里的2/5/10bp是**按所有单边成交额计提的假设总费用率**，不是实际佣金、税率或过户费报价。不含每笔最低费用及费用影响可用现金后导致的交易路径变化，仅作固定成交路径费用敏感性。

两版零费用下的相对持有增量已经为负，因此不存在使其跑赢持有的非负盈亏平衡费用率。实际费率未提供，**不报告真实净收益，不给出净收益验收通过结论**。

## 8. 今日样例及最差日期
''')
sections.append(table(['版本','2026-09-10成交笔数','账户毛收益','持有日内收益','相对持有','日末仓位'],[(v,len(select(v)[-1]['trades']),money(select(v)[-1]['account_gross']),money(select(v)[-1]['hold_gross']),money(select(v)[-1]['excess_gross']),select(v)[-1]['final_position']) for v in ('v51','v39')]))
sections.append('\n此处为长飞、全日不中断、10万元现金和200股初始底仓的模拟，不是今天长电实盘112元，也不能用于核对你今天发生休眠/停机的账户收益。\n')
sections.append(table(['版本','最差日期','相对持有增量','净仓位偏离初始股数','未平交易腿'],[(v,r['date'],money(r['excess_gross']),r['position_gap'],str(r['ledger_unclosed'])) for v in ('v51','v39') for r in sorted(select(v),key=lambda r:r['excess_gross'])[:3]]))
sections.append('''
## 9. 能解释什么、不能解释什么

1. **没有证据表明v51比v39更赚钱**：同样日实验下v51收益更低，减少未平仓天数与提高收益不是同一指标。
   本次v51的主要负贡献不是主策略反T：主反T相对持有贡献约+239元，主正T约−13,208元，MOM正向约−7,737元，MOM反向约+6,664元。需要把正T和MOM方向的持仓风险分开审视，不能将全策略亏损简单归结为反T卖点过高。
2. **不能只看已完成周期利润**：正T未卖回的下跌风险、反T未买回的上涨机会损失都进入日末增量；仅看闭环会产生明显幸存者偏差。
3. **不能把全部差异归因于0.60/0.80**：v51还改变了手数、历史阈值、重入、资金预留和实际成交记账；本次比较的是两个完整版本，不是控制其他变量的单参数实验。
4. **不能评价active强度是否有效**：默认shadow未用于执行，必须另设明确标记的active实验才可回答。
5. 当前结果更适合暴露尾部风险和代码差异，不支持进一步压低阈值来保证收益。后续若要评估可上线收益，需要用户确定跨日未平仓处理规则和真实费用，连续账户回放并补充逐笔/盘口或多种成交模型验证。

## 10. 旧分钟脚本为何未直接使用

已有 `backtest/minute_backtest/run_backtest.py`、`run_backtest_v39.py` 读取整个daily_df的尾部作为历史，而不是严格截至回测日前；指定日期缺失时改用全部日期。模拟tick把每分钟open和前一分钟close分别当作日开盘和昨收，成交额/量未作日内累计；可卖数量直接等于总持仓，缺失T+1约束。故其既有日志收益不作为证据。

已有加载器调用mootdx.bars时将翻页位置传给offset（该参数实际为条数），未传start；本机库映射显示`'1m'`对应8，而旧脚本使用1。此次不使用该加载器，直接指定BigQMT period='1m'并检查实际时间序列。

## 11. 复现与文件

```powershell
python analysis/compare_v51_v39_minute.py --fetch
python analysis/report_v51_v39_minute.py
python -m unittest discover -s tests -p test_v51_v39_minute_replay.py
```

不带`--fetch`时仅使用本报告同目录数据快照。回放会覆盖本实验results.json，不触及实盘检查点。两版共用相同原始数据，费用未传入真实账户。

- `v51_v39_minute/1m.csv`、`1d.csv`：本次只读取得的数据快照。
- `v51_v39_minute/results.json`：396次实验逐笔成交、现金权益、未平仓及错误信息。
- `v51_v39_minute/daily_summary.csv`：逐日对比表。

策略源码SHA256：
''')
sections.append('\n'.join(f'- {v}: `{h}`' for v,h in data['hashes'].items()))
sections.append('\n## 附录：零滑点逐日相对持有增量（元）\n')
sections.append(table(['日期','v51','v39','v51−v39'],[(x['date'],money(x['excess_gross']),money(y['excess_gross']),money(x['excess_gross']-y['excess_gross'])) for x,y in zip(select('v51'),select('v39'))]))
report=ROOT/'analysis/DayT_v51_vs_v39_1min_report.md'
report.write_text('\n\n'.join(sections)+'\n',encoding='utf-8')
pd.DataFrame([{k:v for k,v in r.items() if k not in ('trades','logs','cycle_gross','ledger_unclosed')} for r in rows]).to_csv(DIR/'daily_summary.csv',index=False)
print(report)
