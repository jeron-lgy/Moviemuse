# AGENTS.md

## 项目结构
- 后端：`app/` 里的 FastAPI。
- 前端：`frontend/` 里的 Vue 3 + Vite。
- Unraid/Docker 部署：`deploy/unraid-frontend/`。
- Windows 算力端：`run_worker.py` 与 `deploy/windows-backend/`。
- Unraid 主机稳定性监控：`deploy/unraid-frontend/monitoring/`。它是独立的临时诊断工具，不进入 MovieMuse 容器；运行数据只能写入 `/mnt/user/appdata/moviemuse/monitoring-data/`。
- 新会话先读 `PROJECT_STRUCTURE.md`，用其中三个运行单元的表格判断代码和数据归属。

## 应用边界
- MovieMuse Web 控制台、Windows 算力端、Unraid 主机监控是三个独立运行单元，业务改动和监控改动不要混在同一提交。
- 本地仓库文件是三个运行单元的唯一开发源；禁止直接在 Unraid 上编辑监控或业务脚本。
- 同步监控脚本到 Unraid 与创建 User Scripts/cron 调度是两项独立外部变更，都需要用户明确授权。
- 监控第一版只观测和留证，不得 restart/stop/remove 容器、destroy FlareSolverr session、修改/VACUUM SQLite 或触碰媒体文件。

## 前端
- 修改 UI 前先阅读附近已有的 Vue 文件。
- 优先复用 `frontend/src/components/ui/` 里的现有基础组件。
- 优先使用 `frontend/src/styles/tokens.css` 里的设计变量。
- 前端 API 请求使用 `frontend/src/lib/api.js`。
- 除非任务明确要求重新设计，否则保持当前浅色 MovieMuse 控制台风格。

## 测试
- 日常后端测试：直接在本地运行 `python -m unittest discover tests`。
- 重复视频/本地扫描日常调试优先用本机 Python 服务，不用 Docker：端口固定为 `18183`，访问 `http://127.0.0.1:18183/duplicates`。
- 本机 Python 调试推荐环境：
  - `MEDIA_DIRS=\\192.168.2.9\media`
  - `TRASH_DIR=\\192.168.2.9\media\trash`
  - `APP_DATA_DIR=<项目目录>\data\docker-test`
  - 启动命令：`python -m uvicorn app.main:app --host 127.0.0.1 --port 18183`
- `data\docker-test` 是 `media-toolbox-subscription-test` 的 `/data` 映射目录；日常只运行 18183 这一套本机 Python 服务，不要同时启动 18180 或 Docker 容器共写同一个 SQLite。
- 如需完全隔离测试，可临时使用 `data\local-python-debug`，但那里不会带 Docker/调试配置，页面会像新环境一样空。
- 使用真实媒体目录调试时，默认只读验证扫描、索引、页面状态；不要执行移动/删除/回收站操作，除非任务明确要求并已确认。
- 只有任务明确要求同步到 Unraid 时，才把本地修改同步/部署到 Unraid 做实机验证。

## 安全
- 永远不要提交：真实媒体、API key、模型文件。
- 数据/临时/生成物不要提交：`data/`、`docker-data/`、`tmp-tests/`、`.tmp-*/`、`trash/`、`sample-media/`、`frontend/dist/`、`frontend/node_modules/`、`*.sqlite3`、`*.log`、`__pycache__/`。
- UI 中移动、删除、放入回收站等文件操作必须要求明确确认。
- 测试时保持 Unraid 媒体挂载只读，除非任务明确是在验证移动行为。

## Unraid 实机调试原则
- 后续开发必须先修改本地项目文件，把本地文件作为唯一开发源。
- 不要直接在 Unraid 容器或 Unraid 文件系统里修改项目代码。
- 需要实机验证时，先在本地完成修改、构建，再复制/部署到 Unraid 重建或热更新后测试。
- Unraid SSH 权限仅用于查看日志、检查容器/配置/运行状态、定位问题，以及部署本地构建产物进行验证。
