# MovieMuse Unraid 稳定性监控

这是 MovieMuse 的第三个独立运行单元：一个临时、轻量、只观测的 Unraid 主机工具。它不属于 FastAPI 后端，不打包进 MovieMuse 镜像，也不属于 Windows 算力端。

建议先运行 14 天。问题消失后应降低频率或停用，不把它长期演变成另一套业务服务。

## 代码与数据边界

| 内容 | 位置 | 属性 |
| --- | --- | --- |
| 监控源码 | 仓库 `deploy/unraid-frontend/monitoring/` | Git 管理的唯一最新源 |
| Unraid 脚本副本 | `/mnt/user/appdata/moviemuse/monitoring/` | 只能由本地源码同步生成 |
| 采样与状态 | `/mnt/user/appdata/moviemuse/monitoring-data/` | 临时运行数据，不进入 Git |
| MovieMuse 应用数据 | `/mnt/user/appdata/moviemuse/data/` | 监控只读 |
| 媒体目录 | `/mnt/user/media/` 等 | 监控不访问、不修改 |

目录中的文件职责：

```text
monitoring/
  collect-moviemuse-health.sh    单次采集、阈值判断和小型事件快照
  summarize-moviemuse-health.sh  只读汇总 JSONL 样本
  README.md                      部署、字段、安全和回滚说明
```

运行后生成：

```text
monitoring-data/
  samples/       按 UTC 日期保存的普通 JSONL 样本，保留 14 天
  events/        容器身份和 Flare session 变化，保留 30 天
  incidents/     新告警或容器重建时的小型 JSON 快照，保留 30 天
  state/         锁、前一份样本、清理日期和有限错误日志
```

脚本不会写入上述 `monitoring-data` 之外的任何位置。生产采集拒绝把输出根目录改到其他路径；只有显式设置 `MOVIEMUSE_MONITOR_TEST_MODE=1` 时，测试才能使用临时绝对目录。

## 监控内容

- MovieMuse/FlareSolverr 容器 ID、镜像 ID、启动时间、重启数、OOM 状态。
- cgroup v2 的 current/peak/max/swap、`memory.events` 和 anon/file/shmem/slab。
- 进程数、线程数、RSS、数值型 PID、Chromium 分类，以及是否存在本地重模型侧面信号。
- MovieMuse `/health` 状态与耗时。
- FlareSolverr session 名称、数量、变更频率和无 session 时的 Chromium 残留。
- SQLite/WAL/SHM 大小、事件数量、`worker_offline` 速率和 payload 大小。
- `waiting_worker`、JavDB 开关、远程 worker 可用性。
- `/proc/vmstat` 与 syslog 尾部的宿主 OOM 候选信号。
- Docker cgroup 版本和 `no swap limit support` 能力提示；该能力检查只在 5 分钟深采样执行。

进程命令行只在内存里用于分类，不写入样本；远程 worker token 通过 curl 标准输入提供，不出现在 curl 进程参数、样本或日志中。incident 只保存关键字计数，不复制原始 Docker 日志。

## 依赖与现场默认值

必需命令：

```text
bash docker sqlite3 curl flock jq awk grep sed stat find readlink
```

不依赖 Python。缺少必需命令时 collector 会明确退出，且不写伪造样本。

当前现场默认值：

| 参数 | 默认值 |
| --- | --- |
| MovieMuse 容器 | `moviemuse` |
| FlareSolverr 容器 | `flaresolverr` |
| MovieMuse 健康地址 | `http://127.0.0.1:18188/health` |
| FlareSolverr API | `http://127.0.0.1:8191/v1` |
| MovieMuse 数据目录 | `/mnt/user/appdata/moviemuse/data` |
| 监控数据目录 | `/mnt/user/appdata/moviemuse/monitoring-data` |

如现场名称或端口变化，可给 User Scripts 设置以下环境变量：

```bash
MOVIEMUSE_CONTAINER_NAME=moviemuse
MOVIEMUSE_FLARE_CONTAINER_NAME=flaresolverr
MOVIEMUSE_HEALTH_URL=http://127.0.0.1:18188/health
MOVIEMUSE_FLARE_URL=http://127.0.0.1:8191/v1
MOVIEMUSE_APP_DATA_DIR=/mnt/user/appdata/moviemuse/data
```

生产环境不要设置 `MOVIEMUSE_MONITOR_TEST_MODE`。

## 同步前验证

先在本地仓库完成修改和测试。不要在 Unraid 上编辑这三个文件。

部署前可把 collector 内容通过 SSH 标准输入临时执行 `--probe`。该模式不创建目录、不写状态、不轮转文件：

```bash
ssh unraid 'bash -s -- --probe' < collect-moviemuse-health.sh | jq .
```

能力检查同样不写文件：

```bash
ssh unraid 'bash -s -- --capabilities' < collect-moviemuse-health.sh
```

## 手工部署

只有获得部署授权后才执行：

```bash
mkdir -p /mnt/user/appdata/moviemuse/monitoring
```

从本地复制三个源码文件，随后对比 SHA-256。Unraid 上的脚本只能是本地 Git 文件的副本，不应产生现场独有改动。

首次手工采样：

```bash
bash /mnt/user/appdata/moviemuse/monitoring/collect-moviemuse-health.sh
bash /mnt/user/appdata/moviemuse/monitoring/summarize-moviemuse-health.sh --hours 24
```

collector 默认每 5 分钟做一次 SQLite 聚合和远程 worker 探测，每 UTC 日最多一次 `quick_check(1)`。普通轮次只做轻量采样。

## 调度

创建 User Scripts 或 cron 是独立的外部状态变更，需要单独授权。建议每分钟运行一次：

```bash
bash /mnt/user/appdata/moviemuse/monitoring/collect-moviemuse-health.sh
```

collector 使用 `flock -n`；上一轮未结束时直接跳过，并把跳过原因追加到固定状态文件。它没有常驻进程。

## 初始告警

| 信号 | Warning | Critical |
| --- | --- | --- |
| MovieMuse memory.current | `>1.2 GiB` | `>1.6 GiB` |
| MovieMuse anon | `>800 MiB` | `>1.2 GiB` |
| MovieMuse/Flare OOM 增量 | — | 任意增量 |
| `/health` | 单次只记录 | 连续 3 次失败 |
| Flare session | `2` | `>=3` |
| session=0 且 Chromium 存在 | 持续 10 个有效样本 | 持续 30 个有效样本 |
| Flare session 变更 | `>6/小时` | `>20/小时` |
| `worker_offline` | `>10/小时` | `>100/小时` |
| 新 `worker_offline` payload | `>1 KiB` | `>=8 KiB` |
| SQLite 主库日增长 | `>5 MiB` | `>20 MiB` |
| `task_events` | `>=45,000` | `>=50,000` |
| WAL | 大于 64 MiB 且连续增长 3 次 | — |

远程 worker 离线本身是 warning；若同时发现 MovieMuse 本地重模型信号则升级为 critical。`javdb_source_enabled=false` 时发现 JavDB Chromium 也是 critical。

宿主 syslog 的 `global_candidate` 只代表“发现新的宿主 OOM 模式行”，不能单独证明根因。复盘时必须与容器 ID、cgroup OOM 增量和 Unraid 完整系统日志交叉判断。

## 数据格式

普通样本是一行一个 JSON 对象：

```json
{
  "schema_version": 1,
  "record_type": "health_sample",
  "timestamp": "2026-07-21T01:23:00Z",
  "sample": {"deep": false, "duration_ms": 180},
  "moviemuse": {},
  "flaresolverr": {},
  "database": {},
  "compute_worker": {},
  "monitor_state": {},
  "events": [],
  "alerts": []
}
```

不存在或无法可靠读取的字段是 `null`，不会假装为 `0`。字符串由 `jq` 编码，容器名、镜像名或 session 名含空格、引号时也不会破坏 JSONL。

## 资源与安全限制

- 单日普通样本达到 16 MiB 时停止继续写该日文件。
- 可用空间低于 128 MiB 时停止详细采样，只写有限错误记录。
- 样本 14 天、事件和 incident 30 天；incident 最多 500 个。
- 清理只使用已经校验且非 symlink 的固定子目录和严格文件名。
- SQLite 始终使用 `sqlite3 -readonly`、`immutable=1`、`PRAGMA query_only=ON` 和 1 秒 busy timeout。这样不会在应用目录创建或改写 WAL/SHM。
- `immutable=1` 只读取主库已 checkpoint 的状态；若采样时 WAL 非空，`wal_visibility` 会是 `main_only_wal_present`，表示该次聚合可能暂时落后。WAL 大小仍独立记录，后续 checkpoint 后的深采样会补齐统计。
- 不调用扫描、搜索、订阅轮询、下载、移动、删除或回收站接口。
- 不 restart/stop/remove 容器，不调用 `sessions.destroy`，不 VACUUM。
- `memory.swap.max=max` 只记录事实，不代表 Compose swap 限制生效。

## 汇总与回滚

查看最近 24 小时：

```bash
bash summarize-moviemuse-health.sh --hours 24
bash summarize-moviemuse-health.sh --hours 336 --json | jq .
```

停用时先删除或禁用 User Scripts/cron 调度。保留 `monitoring-data` 供复盘或手工归档；删除脚本不会影响 MovieMuse。Git 回滚只回退独立监控提交，不重置其他业务改动。
