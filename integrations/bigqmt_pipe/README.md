# Named Pipe 本机部署

已于 2026-11-02 将桥接 0.3.78、xtquant 兼容层、客户端配置和 QMT 单文件入口复制到本目录。
运行不再依赖 `xtquant_big_convert` 项目。旧集成保留；通过
`run_bigqmt.py --bridge pipe` 使用本目录。仅适用于外部 Python 与大 QMT 位于同一台 Windows 电脑。

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

### 统一账号

账号统一在项目根目录 [`trading_account.txt`](../../trading_account.txt) 修改。
支持 `#` 注释、行尾备注和空行；切换时只保留一个未注释账号，零个或多个启用账号会报错。
Pipe 客户端私有配置及其示例、MiniQMT 的 `core/config.py` 读取同一文件；
切换后须重启相关外部 Python 进程。其他独立策略配置和 Redis 客户端未迁移。
旧客户端配置请按当前 `.example.py` 更新账号读取逻辑。

QMT 单文件模型独立运行：执行 `python integrations/bigqmt_pipe/sync_account.py`
更新部署快照，再把完整源码更新到 QMT；或用 `--qmt-source <源码路径>` 更新指定文件。
同步只改账号，保留编码和权限；需在 QMT 重新编译、加载后生效，不会自动切换运行中的模型。
详细步骤见 [连接流程](连接流程.md)。

2026-11-13：统一账号后 `--check`、`--probe` 均通过，服务确认账号为 `8890697593`。
实际服务返回 `allow_order_methods=True`，与下文原部署快照的关闭状态不同；本次未修改运行中模型权限，未启动策略或发送委托。

在 `C:\03MyProj\QMT-expert` 执行：

```powershell
# 需要安装依赖时；当前 Python 3.14 已有这些依赖
& C:\Programs\Python314\python.exe -m pip install -r integrations\bigqmt_pipe\requirements.txt

# 本地配置检查，不连接、不启动策略
& C:\Programs\Python314\python.exe run_bigqmt.py --bridge pipe --check

# 只读查询服务、资金、持仓，不启动策略
& C:\Programs\Python314\python.exe run_bigqmt.py --bridge pipe --probe

# 默认 v41 策略的信号模式，不发委托
& C:\Programs\Python314\python.exe run_bigqmt.py --bridge pipe --mode signal
```

指定策略可追加 `--strategy <完整路径>`，其余参数透传给策略。不同策略的模式参数可能不同。
启动前核对策略账号、依赖与行情需求。此次没有实际启动策略，也没有验证全部策略 API。

### 简化切换命令

```powershell
# 只检查本地配置，不连接服务、不启动策略
python run_bigqmt.py --bridge pipe --check
python run_bigqmt.py --bridge redis --check
```

省略 `--bridge` 使用启动器的 `BRIDGE_SRC` 默认值（当前为 Redis）。运行策略时保留该参数，将 `--check` 替换为策略参数
（例如默认 v41 的 `--mode signal`）。原有 `--bridge-src <目录>` 仍可用，但不能与
`--bridge` 同时指定。两端服务和客户端配置须匹配；Redis 被封禁时，此参数不能恢复连接。
策略统一通过 `run_bigqmt.py` 启动并选择桥接；A144 v12 已移除策略内固定 Pipe 的逻辑。

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
