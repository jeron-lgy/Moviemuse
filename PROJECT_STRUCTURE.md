# MovieMuse 项目结构与运行边界

这份文件是新会话和维护者的快速导航。判断某项修改属于哪里时，先确定它运行在哪台机器、由哪个进程拥有、数据写到哪里，再进入对应目录。

## 三个核心运行单元

| 运行单元 | 属性 | 运行位置 | 代码归属 | 自有数据 | 不负责 |
| --- | --- | --- | --- | --- | --- |
| MovieMuse Web 控制台 | 主业务应用/控制端 | Unraid Docker 或本机调试服务 | `app/`、`frontend/`、`deploy/unraid-frontend/`（不含 `monitoring/`） | `/data`，正式环境映射到 `/mnt/user/appdata/moviemuse/data` | Windows GPU 计算、宿主稳定性采样 |
| Windows 算力端 | 可选远程 worker | Windows GPU 主机 | `run_worker.py`、`app/subtitle_service.py`、`app/transcode_service.py`、`deploy/windows-backend/` | Windows 本地 worker 数据和 Whisper 模型 | Web 页面、订阅数据库、Unraid 容器管理 |
| Unraid 稳定性监控 | 临时诊断工具/观察端 | Unraid 宿主机 | `deploy/unraid-frontend/monitoring/` | `/mnt/user/appdata/moviemuse/monitoring-data` | 业务处理、自动修复、容器或 session 处置 |

这里的“运行单元”按部署位置和进程所有权划分，不表示必须拆成三个仓库。

辅助集成：

- `browser-extension/moviemuse_jellyfin/`：Jellyfin 浏览器扩展，不属于上述三个常驻运行单元。
- `ops/unraid/`：Unraid 宿主机维护源码，例如 User Scripts。它不是第四个应用，也不属于 MovieMuse 监控；部署和调度属于独立的主机运维变更。
- `tests/`：跨运行单元的本地自动化测试。
- `assets/`：仓库文档图片等静态资料。
- `Ref/`：参考资料，不是运行时代码入口。

## Web 控制台

```text
app/                         FastAPI 后端、服务层和后端静态资源
frontend/                    Vue 3 + Vite 控制台源码
deploy/unraid-frontend/      Dockerfile、Compose 和 Unraid 部署说明
tests/                       后端与业务流程 unittest
```

开发顺序：

1. 后端行为先改 `app/`。
2. UI 先读附近 Vue 文件，复用 `frontend/src/components/ui/` 和 `tokens.css`。
3. 前端请求统一走 `frontend/src/lib/api.js`。
4. 本地测试通过后才构建部署产物。
5. 只有明确要求同步到 Unraid 时，才部署本地构建结果。

本机重复视频调试固定使用 `127.0.0.1:18183`，避免与 Docker 或其他 SQLite 写入者并行。

## Windows 算力端

```text
run_worker.py                         worker 入口
start_windows_backend.bat             本机启动入口
deploy/windows-backend/               分发和部署说明
requirements-windows-worker.lock.txt  Windows 依赖锁定
```

算力端通过 API 接收字幕和转码任务。模型只属于 Windows worker 数据目录，不进入 Git，不应由 Unraid 控制端在远程模式下意外加载。

共享代码例外：

- `app/subtitle_service.py` 和 `app/transcode_service.py` 是控制端本地模式与 Windows worker 共用的实现，不是 worker 私有副本。
- 修改这两个共享模块时，需要同时考虑控制端的“远程模式不得加载本地模型”边界和 Windows worker 的实际计算行为。
- `app/postprocess_service.py`、订阅数据库和调度循环仍由 Web 控制端拥有，Windows worker 不直接管理。

## Unraid 稳定性监控

```text
deploy/unraid-frontend/monitoring/
  collect-moviemuse-health.sh
  summarize-moviemuse-health.sh
  README.md
```

它与 Web 控制台共享部署大类，但不是容器组成部分：

- 脚本运行在 Unraid 宿主机。
- 应用数据和 SQLite 只读。
- 所有运行输出只能进入 `monitoring-data`。
- 本地 Git 文件始终是唯一最新源；禁止直接编辑 Unraid 副本。
- 部署脚本副本和创建 User Scripts/cron 是两个独立授权动作。
- 第一版只告警和留证，不执行 restart、destroy、VACUUM、移动或删除。
- 它会观察宿主机、全部 Docker cgroup 和 libvirt VM，但不拥有这些容器/VM 的配置或
  生命周期；采样这些运行单元不表示把它们的代码并入 MovieMuse 仓库。
- `schema_version=2` 样本加入宿主内存压力、全容器排名、VM QEMU RSS、boot ID、
  采样断档和持久 syslog incident，用于区分容器 memcg OOM、宿主 global OOM 与硬重启。

详细字段、阈值、验证和回滚见 `deploy/unraid-frontend/monitoring/README.md`。

## Unraid 宿主机运维脚本

```text
ops/unraid/
  README.md
  user-scripts/
    u_backup/
      u_backup.sh
      README.md
      tests/
```

这类文件负责 Unraid 自身的维护动作，不是 MovieMuse 的第四个应用：

- `ops/unraid/` 是唯一开发源，现场 User Scripts 目录只是部署副本。
- 每个脚本独占一个目录，README 必须标明源码、现场路径、数据目录、调度、验证和回滚方法。
- 运维脚本可以修改其明确拥有的宿主机目标，但不得顺带修改 MovieMuse、Windows worker 或稳定性监控。
- 同步脚本、启停调度、执行清理是不同的外部状态变更；实施前分别核对授权。
- 运维脚本与业务、Windows worker、监控改动使用独立提交。

## Git 边界

- 业务修复、Windows worker、监控工具和宿主机运维脚本应尽量使用不同提交。
- 监控专项提交只包含 `monitoring/`、本结构导航和必要说明/测试。
- 宿主机运维提交只包含 `ops/unraid/`、必要的结构导航和对应测试。
- `data/`、`docker-data/`、`monitoring-data/`、模型、媒体、SQLite、日志和构建产物都不提交。
- 开始修改前检查 `git status --short --branch`；不得覆盖用户已有改动。
- Unraid 现场副本不是开发分支，不允许从现场反向形成未记录源码。
