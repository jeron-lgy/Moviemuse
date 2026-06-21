# MovieMuse

Unraid 媒体整理控制台 + Windows 字幕算力端。

MovieMuse 用于扫描 NAS 媒体目录中的重复版本，预览并移动低优先级文件，同时把需要字幕的视频任务发送到 Windows GPU 主机，使用 `faster-whisper` 生成字幕并按配置翻译。

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

## 主要功能

- 扫描指定媒体目录，按版本分组查看重复视频。
- 手动选择或按策略命中待移动文件，移动前提供预览。
- 回收站移动支持 Unraid 同盘路径处理，避免无意义的大文件复制。
- 从扫描页提交视频到字幕任务队列。
- Windows 算力端使用 `faster-whisper` 与 CUDA 转写视频。
- 字幕翻译支持 Google 免费翻译、DeepL API、DeepSeek API 和本地 Ollama。
- 控制台集中配置 Windows 算力端地址、模型设置、翻译服务和路径映射。

## 首次部署

### 1. Unraid 控制台

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

RTX 5090 推荐先使用：

```text
large-v3 / cuda / float16 / 并发 1
```

模型下载参考：

- [faster-whisper 项目](https://github.com/SYSTRAN/faster-whisper)
- [faster-whisper-large-v3 模型](https://huggingface.co/Systran/faster-whisper-large-v3)
- [faster-whisper-large-v3-turbo 模型](https://huggingface.co/Systran/faster-whisper-large-v3-turbo)

下载后的完整模型文件夹放入 `data\local-backend\whisper-models`，然后在控制台选择对应模型目录或模型名称。

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
