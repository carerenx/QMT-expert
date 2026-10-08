# 在 QMT-expert 中用 miniQMT 策略接入大 QMT

接入链路：`run_bigqmt.py → 本目录 src/xtquant 兼容层 → ZeroMQ RPC → 大 QMT 内置 Python → 行情 / 账户 / 委托 / 成交回报`。

默认运行 `Stragety/MiniQMT_Stragety/DayT/DayTradeing_v41_stragety_miniqmt.py`。原策略文件和 `DayT/infra/connector.py` 不需要修改；直接运行原策略仍使用原来的 miniQMT 环境。只有通过新入口启动的进程才优先加载桥接兼容层。不要将本目录 `src` 加入全局 PYTHONPATH，也不要把这里的 `xtquant` 安装到日常 miniQMT 环境。

## 1. 外部 Python 环境

在 QMT-expert 根目录执行（已有 `.venv-bigqmt` 时跳过创建）：

```powershell
python -m venv .venv-bigqmt
.\.venv-bigqmt\Scripts\python.exe -m pip install -r integrations\bigqmt\requirements.txt
```

外部策略环境与大 QMT 内置 Python 是两个环境。上游外部包声明 Python 3.8+，本次外部测试使用 Python 3.13。本机 `C:\QMT\bin.x64\python.exe` 为 3.6.8，迁入的 42 个服务端文件已通过该解释器语法编译检查，但这不等于完整终端运行验证。选择 ZeroMQ 时，大 QMT 端也需要与其 Python 版本和位数匹配的 `pyzmq`；选择 Redis 时需要对应的 `redis` 库。

## 2. 配置

首次从 Git 获取项目时，把 `src` 中两个 `*.example.py` 分别复制为：

- `bigqmt_signal_trader_client_config.py`：外部 Python 客户端配置。
- `bigqmt_signal_trader_local_config.py`：部署到大 QMT 的服务端配置。

两份 `BIGQMT_ACCOUNT_ID` 都填当前策略同目录 `core/config.py` 的 `ACCOUNT`。这两个文件由 `.gitignore` 忽略。启动器要求客户端账号与策略账号一致。

**本机现状（2026-09-04）**：`C:\QMT\python` 已有桥接服务端，其配置使用本地 Redis `127.0.0.1:6379 / db=5`。本次生成的客户端私有配置已对齐它，无需改成 ZeroMQ。Redis 可访问，但桥接 RPC `ping` 超时、请求通道订阅者为 0：须先在大 QMT 中运行已有的 `C:\QMT\python\BIGQMT_REDIS_DRYRUN.py`，再执行 `--probe`。现有终端配置开放委托 RPC，本次未修改终端文件，也未启动实盘策略。

仓库内已生成的服务端私有配置同样使用该 Redis 连接，但保留 `rpc_allow_order_methods=False`，用于重新部署时先做只读验证。下面的两个公开模板则提供不依赖 Redis 的 ZeroMQ 方案；切换传输时同时修改两端。

默认模板使用本机 ZeroMQ，RPC 地址为 `tcp://127.0.0.1:15700`；整推行情和成交事件还会使用相邻端口，避免其他服务占用。客户端和服务端修改地址时保持一致。Redis / MySQL 传输实现也已保留，详见 [上游传输说明](docs/RPC_TRANSPORTS.md)，MySQL 需额外安装相应依赖。

服务端 `rpc_allow_order_methods=False` 为只读模式，委托和撤单 RPC 被拒绝。客户端启用 `fallback_rpc=True`，本地行情缓存缺失时会向大 QMT读取。FormulaServer 快速通道默认关闭，首次接入统一经过桥接 RPC。

## 3. 部署大 QMT 服务端

将以下文件/目录复制到实际券商大 QMT 的 `python` 目录，保持同级关系：

```text
<大QMT>/python/
  bigqmt_signal_trader/                     ← 本目录 src 下整个同名包
  bigqmt_signal_trader_strategy.py
  bigqmt_signal_trader_redis_rpc_runtime.py
  bigqmt_signal_trader_local_config.py      ← 已填写账号的服务端配置
  QMT_BigQMT_Bridge_v1.py                   ← src/BIGQMT_REDIS_DRYRUN.py 的副本
```

**不要复制 `src/xtquant` 到大 QMT 的 Python 目录**，那是外部策略专用的兼容层。服务端入口保留上游 GBK 编码声明。

在大 QMT 中导入 `QMT_BigQMT_Bridge_v1.py`，通过内置 Python 运行，绑定与配置一致的资金账号。需要终端回调驱动，不能仅在外部命令行执行这个文件。服务端以 `run_time` 定时处理请求，示例间隔为 500 毫秒；该定时器在模型回测时无效。[Python API, p.42]，见仓库 `references/md/python/python_api.md`。

大 QMT 端须有策略所需的历史日线和复权数据。本配置关闭后台历史下载任务；先在终端补齐数据。v41 读取前复权与不复权两套日线，日期或价格校验失败时原策略会锁单。

## 4. 检查与运行

以下命令均在 QMT-expert 根目录执行：

```powershell
# 仅检查本地文件、账号、兼容层和传输依赖，不连接服务
.\.venv-bigqmt\Scripts\python.exe run_bigqmt.py --check

# 服务端启动后：只读查询 ping、资金、持仓，不启动策略
.\.venv-bigqmt\Scripts\python.exe run_bigqmt.py --probe

# v41 信号模式（不发委托）
.\.venv-bigqmt\Scripts\python.exe run_bigqmt.py --mode signal

# 指定其他策略，参数原样透传
.\.venv-bigqmt\Scripts\python.exe run_bigqmt.py --strategy Stragety\MiniQMT_Stragety\DayT\DayTradeing_v40_stragety_miniqmt.py --mode signal
```

通用入口目前适配从同目录 `core/config.py` 读取 `ACCOUNT` 的策略。其他策略需要先核对账号来源及用到的 API，不能据此认定所有 xtquant API 都与原 SDK 完全等价。[兼容层说明](docs/XTQUANT_COMPAT_REPLACEMENT.md)

准备实盘时，在大 QMT 服务端配置中将 `rpc_allow_order_methods` 改为 `True`，重启桥接策略，然后运行：

```powershell
.\.venv-bigqmt\Scripts\python.exe run_bigqmt.py --mode live
```

v41 保留原有实盘确认流程。切换同一策略到大 QMT 前，应停止它原来的 miniQMT 实盘进程，避免重复执行。当前集成没有替你启用实盘或提交任何交易。

## 5. 验证与来源

```powershell
.\.venv-bigqmt\Scripts\python.exe -m pip install pytest redis DBUtils
.\.venv-bigqmt\Scripts\python.exe -m pytest integrations\bigqmt\tests -q
```

测试使用模拟 QMT 对象和本机测试传输，不需要真实账户。全套离线验证 484 项通过，4 项因未安装 `bson`、`pyarrow`、`msgpack` 而跳过；其后新增探测超时/错账号测试，入口专项共 10 项通过。`--check` 仅证明本地接入准备完成，`--probe` 成功才证明服务端可访问；真实行情、成交回报和终端内置 Python 兼容性仍需大 QMT 联调验证。

源码来自 `D:\02Project\xtquant_big_convert_CR` 工作树，保留 [MIT 许可证](LICENSE) 和 [源文件哈希清单](UPSTREAM.json)。迁入桥接包、兼容层、运行入口及对应测试；未迁入独立回测引擎、基准测试脚本或私有配置。集成修改仅涉及相对启动路径、配置模板和 QMT-expert 新入口。运行不再依赖原项目目录。
