# Named Pipe 本机部署

已于 2026-11-02 将桥接 0.3.78、xtquant 兼容层、客户端配置和 QMT 单文件入口复制到本目录。
运行不再依赖 `xtquant_big_convert` 项目。旧的 `integrations/bigqmt` 保留；默认
`run_bigqmt.py` 使用本目录。仅适用于外部 Python 与大 QMT 位于同一台 Windows 电脑。

## 文件

- `src/bigqmt_signal_trader/`：桥接包，包含 RPC 管道与独立事件管道。
- `src/xtquant/`：供旧策略导入的兼容层，仅由启动器放到导入路径前面。
- `src/bigqmt_signal_trader_client_config.py`：已填写账号的本机私有配置，Git 忽略。
- `deploy/PIPE_ASCII_ALL_IN_ONE.py`：已填写账号、下单关闭的 QMT 服务端源码快照，Git 忽略。
- `UPSTREAM.json`、`LICENSE`：来源、复制时校验值与许可证。

客户端配置使用 `transport=pipe`、`redis_enabled=False`、`pipe_name=bigqmt_rpc`，
关闭 FormulaServer 直连。配置变量仍叫 `BIGQMT_REDIS_CONFIG`，并不意味着使用 Redis。
公开的 `*.example.py` 不含真实账号；首次克隆后应按模板填写客户端私有配置。

## 运行

在 `C:\03MyProj\QMT-expert` 执行：

```powershell
# 需要安装依赖时；当前 Python 3.14 已有这些依赖
& C:\Programs\Python314\python.exe -m pip install -r integrations\bigqmt_pipe\requirements.txt

# 本地配置检查，不连接、不启动策略
& C:\Programs\Python314\python.exe run_bigqmt.py --check

# 只读查询服务、资金、持仓，不启动策略
& C:\Programs\Python314\python.exe run_bigqmt.py --probe

# 默认 v41 策略的信号模式，不发委托
& C:\Programs\Python314\python.exe run_bigqmt.py --mode signal
```

指定策略可追加 `--strategy <完整路径>`，其余参数透传给策略。不同策略的模式参数可能不同。
启动前核对策略账号、依赖与行情需求。此次没有实际启动策略，也没有验证全部策略 API。

## QMT 端

继续运行当前已验证的 `PIPE_ASCII_ALL_IN_ONE` 模型即可，不需要重新部署或重启。
换电脑或重建模型时，复制 `deploy/PIPE_ASCII_ALL_IN_ONE.py` 的完整源码到 QMT 新建模型，
编译保存后放入模型交易运行，不勾选独立 Python 进程。

单文件模型的配置在源码顶部，外部 `local_config.py` 不控制它。当前
`rpc_allow_order_methods=False`，未启用下单和撤单。仅有读取正常不能证明交易执行正常。
管道访问仍受 Windows 权限和券商运行环境限制。

## 已验证范围

默认入口已确认使用本目录中的桥接和兼容层，实际连接 `pipe`，服务端返回 0.3.78，
资金、持仓及行情查询成功。服务端确认交易方法关闭。历史覆盖、行情推送、交易回调、
实盘下单与撤单尚未在本次部署中验证。
