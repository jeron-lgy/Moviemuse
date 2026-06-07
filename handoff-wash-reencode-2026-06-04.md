# MovieMuse 洗版 / 转码 / 字幕后处理闭环交接文档

日期：2026-06-04  
分支：`codex/feat-wash-reencode`  
基线：从 `feat/subscription` 新建  
状态：代码已实现并通过本地测试，尚未在真实 Unraid + qBittorrent + MTeam + Jellyfin + Windows 算力端环境跑完整闭环。

## 1. 本轮需求总览

这轮会话围绕“已入库影片洗版”和后续“转码 / 字幕 / 版本替换闭环”展开，最终落成的目标是：

- 已入库封面卡片增加“洗版”入口。
- 洗版只能二选一：`中文洗版` 或 `4K 洗版`，不再支持同时追两个版本。
- 洗版不是立刻失败逻辑：添加任务后按轮询周期持续查 MTeam，直到过期期限仍没有符合资源才取消。
- 订阅页增加洗版相关 tab / 筛选 / 状态展示。
- 左侧栏增加“转码”模块，提供任务列表和设置页。
- 新增 SQLite 后处理表，承接 qB 下载完成后的任务状态机。
- qB 下载完成后按 torrent hash、category、tags、save path、content path 做保护校验，避免误操作非本系统下载。
- 转码和字幕完成后必须校验，校验通过才激活新版本。
- 只替换 MovieMuse 自己在 `/压制` 托管生成的旧 active version，不直接操作当前 Jellyfin 原库里的旧文件。
- 旧版本可移动到 trash，但只限受控路径和可校验文件，避免删错。
- 后台日志补充 MTeam 候选、过滤原因、qB 绑定、后处理事件，解决之前“日志太泛”的问题。

## 2. 主要新增文件

- `app/postprocess_service.py`
  - 新增后处理数据层。
  - 管理 `media_versions`、`postprocess_tasks`、`qb_torrents`、`task_events`、`postprocess_settings`。
  - 支持旧库自动补列的轻量迁移。

- `app/templates/transcode.html`
  - 新增“转码任务 / 转码设置”页面。
  - 支持查看任务、运行队列、单任务执行、重试、取消、查看事件日志、保存后处理设置。

- `tests/test_postprocess_flow.py`
  - 新增后处理闭环测试。
  - 覆盖 schema 迁移、MTeam 过滤、qB 保护、版本激活、旧版本 trash、输出冲突、字幕降级、worker 队列等核心逻辑。

- `handoff-wash-reencode-2026-06-04.md`
  - 本交接文档。

## 3. 主要修改文件

- `app/main.py`
  - 新增洗版创建、MTeam 搜索过滤、qB 绑定、qB 轮询、后处理队列、转码 worker、字幕回调、成品校验、版本激活、API 和页面入口。

- `app/subscription_service.py`
  - 新增洗版轮询 cron 设置。
  - 洗版状态 / 过期逻辑接入订阅数据。

- `app/templates/subscriptions.html`
  - 已入库卡片增加洗版按钮和状态显示。
  - 增加洗版 tab。
  - 已入库列表增加洗版中文 / 洗版 4K 状态筛选。

- `app/templates/subscription_tasks.html`
  - 定时任务页增加洗版轮询时间。

- `app/templates/settings.html`
  - 订阅管理里增加洗版设置项。

- `app/templates/partials/sidebar_nav.html`
  - 左侧栏增加转码二级菜单。

- `app/templates/logs.html`
  - 日志页可以看到后处理事件。

- `deploy/unraid-frontend/Dockerfile`
  - 安装 `ffmpeg`。

- `deploy/unraid-frontend/Dockerfile.local-test`
  - 本地测试镜像同步 ffmpeg。

- `deploy/unraid-frontend/docker-compose.yml`
  - 增加 `/study3`、`/压制`、`/unraid` 等后处理挂载。
  - 增加 `POSTPROCESS_DOWNLOAD_DIR`、`POSTPROCESS_OUTPUT_DIR`、Unraid 相对路径环境变量。

## 4. 数据表与状态机

新增 SQLite 表：

- `media_versions`
  - 记录某个番号的版本链。
  - `active` 表示当前 MovieMuse 托管版本。
  - `superseded` 表示已被新版本替代。
  - `trashed` 表示旧托管版本已移动到 trash。
  - `failed` 表示候选版本失败。

- `postprocess_tasks`
  - 后处理任务主表。
  - 支持任务类型：订阅下载、中文洗版、4K 洗版、转码、字幕等组合链路。

- `qb_torrents`
  - 保存 qB torrent hash、category、tags、save path、content path、完成状态。
  - 一个 torrent hash 不能重复绑定到多个不同任务。

- `task_events`
  - 记录任务级详细日志。
  - 包括 MTeam 搜索数量、过滤原因、qB 推送结果、下载完成、校验结果、版本激活、trash 结果。

- `postprocess_settings`
  - 后处理设置。
  - 默认包括下载目录 `/study3`、输出目录 `/压制`、编码格式、qB category / tags、worker 自动运行开关、最大并发等。

核心任务流：

1. 用户在已入库卡片点“洗版”。
2. 选择 `中文洗版` 或 `4K 洗版`。
3. 系统创建洗版任务，不因当前 MTeam 没有合适资源而立刻失败。
4. 定时任务按 `postprocess_cron` 轮询。
5. MTeam 找到符合资源后推送 qB。
6. qB 下载完成后进入后处理保护校验。
7. 通过保护校验后进入转码 / 字幕 / 本地托管成品校验。
8. 校验通过后创建新 `media_versions`。
9. 如果存在旧 active version，且旧版本是 MovieMuse 在 `/压制` 托管生成的文件，则激活新版本并把旧版本移动到 trash。
10. 任务完成后显示在洗版完成 tab / 转码任务页 / 日志页。

## 5. MTeam 中字 / 4K 区分逻辑

中文洗版优先识别：

- 标题或标签里有中文 / 字幕 / Chinese / CHS / CHT / SUB 等相关标记。
- 缺少中文字幕标记会记录 `missing_chinese` 过滤原因。

4K 洗版优先识别：

- 标题或标签里有 UHD / 2160p / 4K 等标记。
- 缺少 UHD / 2160p / 4K 标记会记录 `missing_uhd` 过滤原因。

过滤审计：

- `mteam_filter_audit()` 会把候选资源和拒绝原因写进日志 / task events。
- 这解决了之前“只提示有资源但不符合条件，看不到为什么”的问题。

## 6. qBittorrent 保护规则

后处理不直接相信 qB 下载完成事件，会做保护：

- torrent hash 必须绑定到本系统任务。
- category 必须符合设置。
- tags 必须符合设置。
- save path / content path / 选中的主视频文件必须在设置的下载根目录内。
- 路径前缀做边界判断，避免 `/study33` 被误判成 `/study3`。
- 多文件 torrent 会选择主视频文件。
- 单文件 torrent 兼容 `content_path`。
- 文件必须存在、可读、大小稳定，并与 qB 报告大小接近。

实机注意：

- qB 容器最好把 `/mnt/user/study3` 映射成 `/study3`。
- qB 保存路径建议直接用 `/study3`。
- 如果 qB 返回路径和 MovieMuse 容器看到的路径不一致，后处理会停在保护失败或等待文件可见。

## 7. 版本替换与 trash 规则

本轮明确改成：

- 不移动 Jellyfin 当前媒体库里的旧原盘 / 老版本。
- 只移动 MovieMuse 自己生成并托管在 `/压制` 下的旧 active version。
- 旧版本移动前会校验：
  - 路径在 `/压制` 托管目录内。
  - 文件存在。
  - size / mtime 与数据库记录匹配。
  - 目标 trash 路径可用。

替换顺序：

1. 预检旧版本是否可 trash。
2. 激活新版本。
3. 将旧版本移动到 trash。
4. 移动失败不回滚新版本，任务会以 warning 完成并记录 `old_version_trash_failed`。

## 8. 转码 / 字幕 / 算力端逻辑

转码设置页支持：

- 是否启用自动转码。
- 是否启用自动字幕。
- 输出目录。
- 下载目录。
- 目标编码：默认 H.265。
- CRF / preset。
- qB category / tags。
- worker 自动运行。
- 最大并发。

算力端：

- Windows 运行 `start_windows_backend.bat`。
- 默认端口 `18181`。
- 默认 `COMPUTE_NODE_ONLY=1`，只作为算力 worker，不显示完整 Web UI。
- Whisper 默认 `large-v3`、`cuda`、`float16`。
- 转码默认调用系统 PATH 里的 `ffmpeg`。
- 默认 H.265 编码器是 `libx265`。
- 可通过 `TRANSCODE_H265_ENCODER=hevc_nvenc` 改成 NVIDIA 硬编。

Worker 队列：

- 如果需要 worker 但算力端离线，任务进入 `waiting_worker`。
- 算力端恢复后可转为 ready。
- 默认不会强制自动跑，除非 `worker_auto_run=true`。
- 用户可以在转码任务页手动执行队列或单任务。

字幕降级：

- 字幕失败不会让已通过的视频成品直接失败。
- 视频可以完成并激活，任务带 warning。
- 字幕相关错误会记录在 `error_code` / task events。

## 9. UI 改动

订阅页：

- 已入库卡片增加“洗版”按钮。
- 洗版弹窗只能选择：
  - 中文洗版
  - 4K 洗版
- 卡片显示洗版状态。
- 已入库增加洗版中文 / 洗版 4K 筛选。
- 增加洗版完成 tab。

订阅任务页：

- 定时任务增加洗版轮询时间。
- 洗版轮询和番号订阅轮询同一套调度体系。

设置页：

- 订阅管理增加洗版设置。
- 包括洗版过期期限，默认三个月左右，用于处理长期没有中文 / 4K 的番号。

左侧栏：

- 新增“转码”菜单。
- 二级菜单：
  - 转码任务
  - 转码设置

日志页：

- 可查看后处理事件。
- MTeam、qB、转码、字幕、版本激活、trash 等事件更细。

## 10. Unraid 部署交接

建议把整个项目复制到 Unraid，例如：

```bash
/mnt/user/appdata/media-toolbox/source
```

至少需要包含：

- `app/`
- `frontend/dist/`
- `deploy/unraid-frontend/`
- `.dockerignore`

不要只复制 `deploy/unraid-frontend/`，因为 Dockerfile 需要从项目根目录复制 `app` 和 `frontend/dist`。

Unraid 构建：

```bash
cd /mnt/user/appdata/media-toolbox/source/deploy/unraid-frontend
docker compose up -d --build
docker logs -f media-toolbox
```

关键挂载：

```yaml
- /mnt/user/media:/media
- /mnt/user/study3:/study3
- /mnt/user/media/压制:/压制
- /mnt/user/appdata/media-toolbox/data:/data
- /mnt:/unraid
```

关键环境变量：

```yaml
POSTPROCESS_DOWNLOAD_DIR: /study3
POSTPROCESS_OUTPUT_DIR: /压制
POSTPROCESS_DOWNLOAD_UNRAID_RELATIVE: study3
POSTPROCESS_OUTPUT_UNRAID_RELATIVE: media/压制
UNRAID_MOUNT_ROOT: /unraid
UNRAID_TRASH_RELATIVE: media/trash
```

## 11. Windows 算力端部署交接

在 Windows 算力机复制项目后运行：

```bat
start_windows_backend.bat
```

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:18181/health
```

Unraid WebUI 的转码设置中填：

```text
http://WINDOWS_IP:18181
```

路径映射示例，按实际 SMB 名称调整：

```text
/study3=\\UNRAID_IP\study3;/压制=\\UNRAID_IP\media\压制;/media=\\UNRAID_IP\media
```

如果使用 NVIDIA 硬编，可在启动前设置：

```powershell
$env:TRANSCODE_H265_ENCODER="hevc_nvenc"
```

## 12. 已验证内容

本地已执行：

```powershell
python -m unittest tests.test_postprocess_flow
python -m compileall app tests
```

结果：

- `tests.test_postprocess_flow`：34 个测试通过。
- `compileall`：通过。
- FastAPI TestClient 冒烟：
  - `/transcode`：200
  - `/transcode-settings`：200
  - `/api/postprocess/tasks`：200
  - `/api/postprocess/settings`：200
  - `/api/logs`：200

非阻塞 warning：

- FastAPI `on_event` deprecation。
- Starlette TestClient `httpx` deprecation。
- 少量 TestClient sqlite ResourceWarning。

这些不是当前功能阻塞项。

## 13. 尚未实机验证 / 主会话需重点看

以下必须在真实环境验证：

- MTeam 搜索真实返回字段是否覆盖所有中字 / 4K 标记。
- qB category / tags 是否按当前 qB 版本成功写入。
- qB 下载完成后的 `save_path` / `content_path` 是否和 MovieMuse 容器路径一致。
- qB 多文件 torrent 是否能正确选择主视频。
- Windows 算力端是否能通过 SMB 打开 `/study3` 和 `/压制` 映射后的路径。
- ffmpeg / ffprobe 是否在 Windows worker PATH 中可用。
- `hevc_nvenc` 如启用，显卡驱动和 ffmpeg 编译是否支持。
- Jellyfin 入库后的数据回流目前没有作为唯一完成信号；当前后处理完成主要由 qB 下载、文件校验、转码/字幕校验和版本激活驱动。
- Docker build 在本会话没有真实 Unraid 环境完整跑通。

## 14. 推荐实机测试顺序

第一轮：

1. 不开自动转码。
2. 不开自动字幕。
3. 只测试中文洗版。
4. 确认 MTeam 能找到资源。
5. 确认 qB 收到 torrent。
6. 确认下载完成后任务进入后处理。
7. 确认新版本能出现在 `/压制` 或被记录为 active version。
8. 看日志里的 task events。

第二轮：

1. 测 4K 洗版。
2. 用一个明确有 2160p / UHD 标记的番号。
3. 检查 MTeam 过滤原因是否合理。

第三轮：

1. 开自动转码。
2. 先用短视频或小文件。
3. 检查 worker 是否收到任务。
4. 检查 ffmpeg 是否产出。
5. 检查转码校验。

第四轮：

1. 开自动字幕。
2. 检查 Whisper 任务、字幕校验、失败降级逻辑。

第五轮：

1. 测旧 MovieMuse active version 替换。
2. 确认只移动 `/压制` 下旧版本。
3. 确认不会移动 Jellyfin 当前媒体库旧文件。

## 15. 当前工作树状态

未提交，当前分支：

```text
codex/feat-wash-reencode
```

当前改动文件：

```text
 M app/main.py
 M app/subscription_service.py
 M app/templates/logs.html
 M app/templates/partials/sidebar_nav.html
 M app/templates/settings.html
 M app/templates/subscription_tasks.html
 M app/templates/subscriptions.html
 M deploy/unraid-frontend/Dockerfile
 M deploy/unraid-frontend/Dockerfile.local-test
 M deploy/unraid-frontend/docker-compose.yml
?? app/postprocess_service.py
?? app/templates/transcode.html
?? tests/
?? handoff-wash-reencode-2026-06-04.md
```

## 16. 给主会话的建议目标模式文本

可以直接复制下面这段开启目标模式：

```text
开启目标模式：
接手分支 codex/feat-wash-reencode 上 MovieMuse 自动订阅 / 洗版 / 转码 / 字幕后处理闭环。
先阅读 handoff-wash-reencode-2026-06-04.md，确认本轮已完成代码、测试、Docker/Windows worker 部署方式和未实机验证项。
目标是协助完成 Unraid + qBittorrent + MTeam + Jellyfin + Windows 算力端实机验证，优先排查路径映射、qB category/tags、MTeam 中字/4K 过滤、worker 转码与字幕回调、版本激活和 trash 保护。
不要重写现有状态机，除非实机日志证明某个环节逻辑不闭环；修复时保持只操作 MovieMuse 托管的 /压制 版本，不直接移动 Jellyfin 当前库内旧文件。
```
