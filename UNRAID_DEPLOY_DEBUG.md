# Unraid 实机部署和调试手册

本文用于反复对照部署 MovieMuse 到 Unraid，并让 Windows 本地 GPU 算力端处理字幕任务。

## 1. 部署结构

整体分两部分：

```text
Unraid Docker 控制台
  - 扫描媒体库
  - 管理去重、回收站、订阅、后处理
  - 把字幕任务转发给 Windows 算力端

Windows 算力端
  - 使用本机 GPU 跑 faster-whisper
  - 读取 Unraid SMB 共享路径里的视频
  - 生成字幕并写回媒体目录
```

默认端口：

```text
Unraid 控制台容器内部端口: 18180
Unraid 对外访问端口: 18188
Windows 算力端端口: 18181
```

## 2. 复制到 Unraid 的文件

推荐复制到：

```text
/mnt/user/appdata/moviemuse
```

必须带过去：

```text
app/
frontend/
deploy/unraid-frontend/
.dockerignore
requirements.txt
```

也可以直接复制整个项目，但必须排除这些本地临时目录：

```text
data/
trash/
tmp-tests/
docker-data/
tmp-docker-backup-*/
tmp-docker-backup-latest/
.tmp-*/
frontend/node_modules/
frontend/dist/
.git/
sample-media/
```

说明：

```text
data/                  本地开发缓存和数据库，不要带到 Unraid
tmp-tests/             本地浏览器联调截图和 Edge profile，不要带
docker-data/           本地 Docker 测试数据，不要带
tmp-docker-backup-*/   之前 Docker 临时备份，不要带
frontend/dist/         Dockerfile 会在构建镜像时重新生成
```

## 3. Unraid docker-compose 检查

配置文件：

```text
deploy/unraid-frontend/docker-compose.yml
```

重点检查端口：

```yaml
ports:
  - "18188:18180"
```

左边 `18188` 是 Unraid 对外访问端口，右边 `18180` 是容器内部端口。端口冲突时只改左边。

访问地址：

```text
http://UNRAID-IP:18188
```

重点检查挂载：

```yaml
volumes:
  - /mnt/user/media:/media
  - /mnt/user/study3:/study3
  - /mnt/user/media/压制:/压制
  - /mnt/user/appdata/moviemuse/data:/data
  - /mnt:/unraid
```

挂载含义：

```text
/mnt/user/media                  -> /media     媒体库，正式读写
/mnt/user/study3                 -> /study3    下载/洗版后处理输入目录
/mnt/user/media/压制             -> /压制      后处理输出目录
/mnt/user/appdata/moviemuse/data -> /data  程序配置、缓存、任务队列
/mnt                             -> /unraid    识别 Unraid 实际磁盘路径，用于同盘快速移动
```

如果你的真实媒体库不是 `/mnt/user/media`，只改冒号左边：

```yaml
- /mnt/user/你的媒体目录:/media
```

容器内路径 `/media` 建议保持不变，前端、字幕路径映射、扫描逻辑都按这个路径工作。

## 4. Unraid 首次启动

进入 Unraid 终端：

```bash
cd /mnt/user/appdata/moviemuse
```

初始化数据目录权限：

```bash
mkdir -p /mnt/user/appdata/moviemuse/data
chown -R 99:100 /mnt/user/appdata/moviemuse/data
chmod -R u+rwX,g+rwX /mnt/user/appdata/moviemuse/data
```

启动：

```bash
docker compose -f deploy/unraid-frontend/docker-compose.yml up -d --build
```

查看日志：

```bash
docker logs -f moviemuse
```

停止：

```bash
docker compose -f deploy/unraid-frontend/docker-compose.yml down
```

重建：

```bash
docker compose -f deploy/unraid-frontend/docker-compose.yml up -d --build
```

## 5. Windows 算力端启动

在 Windows 项目根目录运行：

```text
start_windows_backend.bat
```

等价脚本：

```text
deploy\windows-backend\start_backend.bat
```

脚本会设置：

```text
WHISPER_MODEL=large-v3
WHISPER_MODEL_DIR=data\local-backend\whisper-models
WHISPER_DEVICE=cuda
WHISPER_COMPUTE_TYPE=float16
HOST=0.0.0.0
PORT=18181
COMPUTE_NODE_ONLY=1
```

模型目录：

```text
data\local-backend\whisper-models
```

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:18181/health
```

局域网检查，把 `WINDOWS-IP` 换成 Windows 机器 IP：

```powershell
Invoke-RestMethod http://WINDOWS-IP:18181/health
```

如果 Unraid 连不到 Windows，先检查 Windows 防火墙。项目里有辅助脚本：

```text
allow_windows_backend_firewall.bat
```

## 6. Unraid 控制台里填写算力端

打开：

```text
http://UNRAID-IP:18188
```

在字幕任务/算力端设置里填写：

```text
字幕算力端地址: http://WINDOWS-IP:18181
Unraid 回调地址: http://UNRAID-IP:18188
Token: 首次测试可留空
```

方向不要填反：字幕算力端地址是 Unraid -> Windows；Unraid 回调地址是 Windows -> Unraid，
转码完成后算力端会 POST 这个地址。

路径映射建议：

```text
/media=\\UNRAID-IP\media
```

示例：

```text
/media=\\192.168.2.9\media
```

含义：

```text
容器看到的视频路径: /media/study3/movie.mp4
Windows 实际读取路径: \\192.168.2.9\media\study3\movie.mp4
```

关键要求：

```text
Windows 资源管理器必须能打开 \\UNRAID-IP\media
Windows 用户必须有读取视频、写入字幕的权限
```

## 7. 首次联调顺序

1. Windows 先启动 `start_windows_backend.bat`。
2. Windows 本机执行健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:18181/health
```

3. 另一台机器或 Unraid 测试访问：

```text
http://WINDOWS-IP:18181/health
```

4. Unraid 启动控制台：

```bash
docker compose -f deploy/unraid-frontend/docker-compose.yml up -d --build
```

5. 打开：

```text
http://UNRAID-IP:18188
```

6. 进入字幕任务设置，填写 Windows 算力端地址和路径映射。
7. 在 Windows 资源管理器确认能打开：

```text
\\UNRAID-IP\media
```

8. 在控制台选择一个小视频提交字幕任务。
9. 看 Windows 算力端窗口是否收到任务。
10. 看视频同目录是否生成 `.srt`。

## 8. 常见问题

### Unraid 页面打不开

检查容器是否运行：

```bash
docker ps | grep moviemuse
```

检查日志：

```bash
docker logs --tail=200 moviemuse
```

确认访问端口是 compose 左边端口：

```text
18188:18180 -> 访问 http://UNRAID-IP:18188
```

### 容器不能保存设置

通常是 `/data` 权限问题：

```bash
chown -R 99:100 /mnt/user/appdata/moviemuse/data
chmod -R u+rwX,g+rwX /mnt/user/appdata/moviemuse/data
```

### 扫描不到媒体

进容器检查挂载：

```bash
docker exec -it moviemuse sh
ls -lah /media
```

如果 `/media` 是空的，检查 compose 左边路径：

```yaml
- /mnt/user/media:/media
```

### 字幕任务提交了但 Windows 读不到视频

优先检查路径映射：

```text
/media=\\UNRAID-IP\media
```

再手工把容器路径换算成 Windows 路径：

```text
/media/study3/movie.mp4
\\UNRAID-IP\media\study3\movie.mp4
```

在 Windows 资源管理器里打开这个文件。如果打不开，就是 SMB 共享或权限问题。

### Unraid 连不到 Windows 算力端

检查 Windows 后端是否监听局域网：

```text
HOST=0.0.0.0
PORT=18181
```

Windows 本机检查：

```powershell
Invoke-RestMethod http://127.0.0.1:18181/health
```

局域网检查：

```powershell
Invoke-RestMethod http://WINDOWS-IP:18181/health
```

如果本机通、局域网不通，通常是 Windows 防火墙。

### 后处理目录不对

检查 compose：

```yaml
POSTPROCESS_DOWNLOAD_DIR: /study3
POSTPROCESS_OUTPUT_DIR: /压制
POSTPROCESS_DOWNLOAD_UNRAID_RELATIVE: study3
POSTPROCESS_OUTPUT_UNRAID_RELATIVE: media/压制
```

对应挂载：

```yaml
- /mnt/user/study3:/study3
- /mnt/user/media/压制:/压制
```

如果你的下载目录或输出目录不同，只改宿主机左边路径和相对路径配置。

## 9. 每次更新代码后的部署

从 Windows 重新复制这些到 Unraid：

```text
app/
frontend/
deploy/unraid-frontend/
.dockerignore
requirements.txt
```

然后 Unraid 执行：

```bash
cd /mnt/user/appdata/moviemuse
docker compose -f deploy/unraid-frontend/docker-compose.yml up -d --build
docker logs -f moviemuse
```

不需要复制：

```text
data/
tmp-tests/
docker-data/
tmp-docker-backup-*/
frontend/node_modules/
frontend/dist/
```

## 10. 当前项目里的本地临时目录提示

这些目录属于本地测试/缓存，不要复制到 Unraid：

```text
data/
trash/
tmp-tests/
docker-data/
.tmp-javlibrary-probe/
.tmp-mdc-ng/
.tmp-mdc-ng-full/
```

之前 Docker 临时备份已经移到：

```text
C:\Users\xianka\Documents\codex\去重插件-subscription-local-archive
```
