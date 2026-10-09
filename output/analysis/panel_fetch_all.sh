#!/usr/bin/env bash
# 并行拉取全市场日线（走 redisQMT 桥接）。
#
# ## 实测数据（别凭直觉调分片数）
#
# | 数据源 | 单只耗时 | 并发上限 | 结论 |
# |---|---|---|---|
# | redisQMT 桥接 | 1.14 秒（100 只/114 秒） | 服务端，可多进程 | **唯一可用** |
# | baostock | 18 秒 | **约 7 个会话**，超了直接封号 | 已弃用（账号被锁） |
# | akshare 东财 | — | — | `push2his.eastmoney.com` 被系统代理拦截 |
#
# 5554 只 ÷ 3 分片 × 1.14 秒 ≈ **35 分钟**。
#
# ## 续跑
#
# 按批落盘（每批 100 只）。批文件存在即跳过，所以中断重跑只补缺的，
# 增减分片数也不会重复劳动。
#
# 用法：
#   bash analysis/panel_fetch_all.sh        # 默认 3 分片
#   bash analysis/panel_fetch_all.sh 5
#
# 监控：
#   watch -n 60 'ls analysis/panel_20260920/raw | wc -l'

set -u

SHARDS="${1:-3}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOGDIR="$ROOT/analysis/panel_20260920/_fetch_logs"

mkdir -p "$LOGDIR"

echo "启动 $SHARDS 个分片，日志在 $LOGDIR"
for i in $(seq 0 $((SHARDS - 1))); do
    nohup python -u "$ROOT/analysis/panel_fetch_20260920.py" \
        --shard "$i/$SHARDS" > "$LOGDIR/bridge_shard_${i}.log" 2>&1 &
done

echo "已启动。当前已拉批次：$(ls "$ROOT/analysis/panel_20260920/raw" 2>/dev/null | wc -l)"
