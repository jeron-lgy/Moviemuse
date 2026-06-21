# MovieMuse

MovieMuse 是一个面向 NAS / Unraid 媒体库的整理控制台，重点解决重复视频清理、洗版后处理、字幕生成和字幕翻译这些日常维护问题。

它由两部分组成：Unraid 上运行的 Web 控制台，以及可选的 Windows GPU 算力端。控制台负责扫描媒体库、管理任务和保存配置；Windows 算力端负责使用 `faster-whisper` 生成字幕、执行转码/后处理任务，并把结果回写到媒体目录。

## 项目能做什么

- 扫描 NAS 媒体目录，按番号/标题识别重复视频和不同版本。
- 在移动文件前预览目标，降低误删或误移动风险。
- 把低优先级版本移动到回收站，而不是直接删除。
- 支持 Unraid 同盘快速移动，减少大文件跨盘复制。
- 管理番号订阅、下载状态、洗版任务和后处理队列。
- 对接 qBittorrent、MTeam、Jellyfin 等媒体工作流。
- 把字幕任务发送到 Windows GPU 主机，用 Whisper / faster-whisper 生成字幕。
- 支持 Google 免费翻译、DeepL API、DeepSeek API、本地 Ollama 等字幕翻译后端。
- 通过浏览器扩展辅助 Jellyfin 页面里的字幕和转码操作。

## 需要准备什么

| 项目 | 用途 | 是否必须 |
| --- | --- | --- |
| Unraid 或 Linux Docker 环境 | 运行 MovieMuse Web 控制台 | 必须 |
| 媒体目录 | 例如 `/mnt/user/media`，容器内挂载为 `/media` | 必须 |
| 数据目录 | 保存设置、任务记录和数据库，例如 `/mnt/user/appdata/moviemuse/data` | 必须 |
| Windows GPU 主机 | 运行字幕/转码算力端 | 字幕和转码功能需要 |
| NVIDIA 显卡驱动 | Windows 算力端使用 CUDA 推理 | GPU 字幕需要 |
| Whisper 模型 | 例如 `large-v3-turbo` | 字幕功能需要 |
| qBittorrent / MTeam / Jellyfin | 下载、洗版、媒体库联动 | 按需 |

推荐目录结构：

```text
/mnt/user/media/
  study3/
  study3_h265/
  trash/

/mnt/user/appdata/moviemuse/
  data/
```

如果 `study3`、`study3_h265`、`trash` 都在同一个媒体根目录下，只需要在正式 yml 里挂载媒体根目录即可：

```yaml
MOVIEMUSE_MEDIA_DIR: /mnt/user/media
```

容器内会自动对应为：

```text
/media/study3
/media/study3_h265
/media/trash
```

## 部署结构

| 端 | 运行位置 | 职责 | 默认端口 |
| --- | --- | --- | --- |
| 控制台 | Unraid Docker | 扫描媒体库、选择移动文件、管理字幕任务和算力端配置 | `18180` |
| 算力端 | Windows GPU 主机 | Whisper 转写、翻译、写入字幕文件 | `18181` |

推荐链路：

```text
Unraid /media 下的视频
    -> MovieMuse 控制台选择任务
    -> Windows Worker 通过共享路径读取视频
    -> 生成同目录 .srt 字幕文件
```

## Docker 部署

### 镜像状态

当前仓库已经加入 GitHub Container Registry 发布流程，后续发布 GitHub Release 时会自动构建并推送：

```text
ghcr.io/jeron-lgy/moviemuse
```

正式部署使用 `deploy/unraid-frontend/docker-compose.release.yml`，从 GHCR 拉取镜像，不需要在 Unraid 上保留完整源码。

本地测试和源码构建使用 `deploy/unraid-frontend/docker-compose.yml` 或 `deploy/unraid-frontend/docker-compose.ui-test.yml`，会在本机执行 `docker build`。

| 场景 | Compose 文件 | 镜像来源 | 媒体挂载 | 用途 |
| --- | --- | --- | --- | --- |
| 正式发布 | `deploy/unraid-frontend/docker-compose.release.yml` | `ghcr.io/jeron-lgy/moviemuse` | 默认读写 | 普通用户一键部署 |
| 本地源码构建 | `deploy/unraid-frontend/docker-compose.yml` | 本地 `docker build` | 默认读写 | 开发者从源码构建 |
| 本地/Unraid 联调 | `deploy/unraid-frontend/docker-compose.ui-test.yml` | 本地 `docker build` | 默认只读 | 测试扫描、字幕联调，避免误移动 |

### 正式发布部署

首次安装只需要准备一个目录保存 compose 文件和数据目录，例如：

```bash
mkdir -p /mnt/user/appdata/moviemuse
cd /mnt/user/appdata/moviemuse
```

下载正式发布 compose：

```bash
curl -L -o docker-compose.release.yml \
  https://raw.githubusercontent.com/jeron-lgy/Moviemuse/main/deploy/unraid-frontend/docker-compose.release.yml
```

初始化配置目录权限：

```bash
mkdir -p /mnt/user/appdata/moviemuse/data
chown -R 99:100 /mnt/user/appdata/moviemuse/data
chmod -R u+rwX,g+rwX /mnt/user/appdata/moviemuse/data
```

编辑 `docker-compose.release.yml`，通常只需要确认媒体目录和 WebUI 端口，然后启动：

```bash
docker compose -f docker-compose.release.yml up -d
```

访问控制台：

```text
http://UNRAID-IP:18188
```

### 正式 yml 关键参数

| 参数 | 默认值 | 必填 | 说明 |
| --- | --- | --- | --- |
| `MOVIEMUSE_IMAGE_TAG` | `latest` | 否 | 拉取的镜像标签。正式版本建议固定为 `v0.1.0` 这类版本号。 |
| `MOVIEMUSE_HTTP_PORT` | `18188` | 否 | Unraid 对外 WebUI 端口；容器内固定为 `18180`。 |
| `MOVIEMUSE_MEDIA_DIR` | `/mnt/user/media` | 是 | Unraid 上真实媒体库目录，挂载到容器 `/media`。 |
| `MOVIEMUSE_DATA_DIR` | `/mnt/user/appdata/moviemuse/data` | 是 | 保存设置、任务状态和 SQLite 数据；不要放进媒体目录。 |

Windows 算力端地址、回调地址、路径映射、翻译 API、后处理目录等都在 WebUI 的“字幕任务 / 设置”里维护，不建议写进正式 yml。

### 本地源码构建

需要复制到 Unraid 的主要目录：

```text
app/
frontend/dist/
deploy/unraid-frontend/
requirements.txt
```

首次使用前检查 [deploy/unraid-frontend/docker-compose.yml](deploy/unraid-frontend/docker-compose.yml) 的媒体目录映射：

```yaml
volumes:
  - /mnt/user/media:/media
  - /mnt/user/appdata/moviemuse/data:/data
  - /mnt:/unraid
```

- `/mnt/user/media:/media`：左侧改为你的真实媒体库目录。
- `/mnt/user/appdata/moviemuse/data:/data`：保存设置和任务记录。
- `/mnt:/unraid`：用于识别 Unraid 实际磁盘位置，实现同盘快速移动。
- 回收站默认位于媒体目录下的 `/mnt/user/media/trash`。

**Unraid 首次安装必须执行：初始化配置目录权限**

控制台容器以 `99:100` 用户运行。以下目录用于保存算力端地址、翻译后端设置和任务数据；如果不可写，会出现“测试联通成功，但保存失败”。

```bash
mkdir -p /mnt/user/appdata/moviemuse/data
chown -R 99:100 /mnt/user/appdata/moviemuse/data
chmod -R u+rwX,g+rwX /mnt/user/appdata/moviemuse/data
```

完成目录映射与权限初始化后启动：

```bash
docker compose -f deploy/unraid-frontend/docker-compose.yml up -d --build
```

访问控制台：

```text
http://UNRAID-IP:18180
```

### 2. Windows 算力端

Windows 主机运行：

```text
start_windows_backend.bat
```

默认监听：

```text
http://WINDOWS-IP:18181
```

Windows Worker 只负责提供算力；地址、模型、翻译后端和路径映射在 Unraid 控制台的“字幕任务”页面统一管理。

## 路径映射

算力端必须能够通过 Windows 可访问路径读取 Unraid 视频。例如：

```text
控制台路径：/media/study3/movie.mp4
Windows 路径：\\NAS\media\study3\movie.mp4
```

在控制台的路径映射设置中填写：

```text
/media=\\NAS\media
```

## Whisper 模型

Windows 算力端的模型目录：

```text
data\local-backend\whisper-models
```

推荐先使用：

```text
large-v3-turbo / cuda / float16 / 并发 1
```

追求最高识别质量时可改用：

```text
large-v3 / cuda / float16 / 并发 1
```

低显存或需要兜底时可改用：

```text
medium / cuda / int8_float16 / 并发 1
```

### 新电脑下载模型

新电脑只需要先安装 Python 和 Hugging Face CLI，不需要先安装 MovieMuse 或 CUDA 才能下载模型。

```powershell
winget install Python.Python.3.12
python -m pip install -U huggingface_hub
```

新版 `huggingface_hub` 使用 `hf download`。旧命令 `huggingface-cli download` 已废弃，可能只显示提示但不会下载。

只下载推荐的 turbo 模型：

```powershell
mkdir C:\MoviemuseModels -Force
cd C:\MoviemuseModels

$root = "whisper-models"
mkdir $root -Force

hf download h2oai/faster-whisper-large-v3-turbo `
  --local-dir "$root\large-v3-turbo"
```

一次下载常用三个模型：

```powershell
mkdir C:\MoviemuseModels -Force
cd C:\MoviemuseModels

$root = "whisper-models"
mkdir $root -Force

hf download h2oai/faster-whisper-large-v3-turbo `
  --local-dir "$root\large-v3-turbo"

hf download Systran/faster-whisper-large-v3 `
  --local-dir "$root\large-v3"

hf download Systran/faster-whisper-medium `
  --local-dir "$root\medium"
```

下载完成后，模型目录应包含 `model.bin`、`config.json`、`tokenizer.json`、词表文件等。例如：

```text
whisper-models\
  large-v3-turbo\
    config.json
    model.bin
    tokenizer.json
    vocabulary.json
  large-v3\
    config.json
    model.bin
    tokenizer.json
    vocabulary.json
  medium\
    config.json
    model.bin
    tokenizer.json
    vocabulary.txt
```

把整个 `whisper-models` 文件夹复制到 Windows 算力端的：

```text
data\local-backend\whisper-models
```

然后在控制台“字幕任务”页面填写：

```text
模型目录：data\local-backend\whisper-models
模型名：large-v3-turbo
```

模型链接参考：

- [faster-whisper 项目](https://github.com/SYSTRAN/faster-whisper)
- [faster-whisper-large-v3 模型](https://huggingface.co/Systran/faster-whisper-large-v3)
- [faster-whisper-large-v3-turbo 模型](https://huggingface.co/h2oai/faster-whisper-large-v3-turbo)
- [faster-whisper-medium 模型](https://huggingface.co/Systran/faster-whisper-medium)

## 本机开发

在 Windows 上同时运行控制台和本地算力端：

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\start_local_dev.bat
```

开发访问地址：

```text
控制台：http://127.0.0.1:18180
算力端：http://127.0.0.1:18181
```

## 安全提示

- 建议将 GitHub 仓库设置为私有仓库。
- 不要提交实际的 API Key、模型文件、任务数据库或媒体文件。
- 局域网以外使用 API 时，应设置 Token 并做好网络访问限制。
