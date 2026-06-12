# AGENTS.md

## 项目结构
- 后端：`app/` 里的 FastAPI。
- 前端：`frontend/` 里的 Vue 3 + Vite。
- Unraid/Docker 部署：`deploy/unraid-frontend/`。
- Python 测试：`tests/`。

## 前端
- 修改 UI 前先阅读附近已有的 Vue 文件。
- 优先复用 `frontend/src/components/ui/` 里的现有基础组件。
- 优先使用 `frontend/src/styles/tokens.css` 里的设计变量。
- 前端 API 请求使用 `frontend/src/lib/api.js`。
- 除非任务明确要求重新设计，否则保持当前浅色 MovieMuse 控制台风格。

## 测试
- 前端改动：在 `frontend/` 下运行 `npm run build`。
- 后端逻辑改动：相关时运行 `python -m unittest discover tests`。
- 容器/UI 验证应使用 Docker MCP 或 UI 测试 compose 配置，不要使用生产数据。
- 不要把 `tmp-tests/` 里的旧文件当作事实来源。

## 安全
- 不要提交真实媒体、API key、模型文件、`data/`、`docker-data/` 或 `tmp-tests/`。
- UI 中移动、删除、放入回收站等文件操作必须要求明确确认。
- 测试时保持 Unraid 媒体挂载只读，除非任务明确是在验证移动行为。

## Unraid 实机调试原则
- 后续开发必须先修改本地项目文件，把本地文件作为唯一开发源。
- 不要直接在 Unraid 容器或 Unraid 文件系统里修改项目代码。
- 需要实机验证时，先在本地完成修改、构建，再复制/部署到 Unraid 重建或热更新后测试。
- Unraid SSH 权限仅用于查看日志、检查容器/配置/运行状态、定位问题，以及部署本地构建产物进行验证。
