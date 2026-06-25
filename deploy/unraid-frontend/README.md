# Unraid Docker 部署

这个目录包含 MovieMuse 控制台的 Docker 部署文件。

MovieMuse 控制台不在容器内运行 Whisper。字幕和转码任务会转发到 Windows 算力端。

## Compose 文件

| 文件 | 场景 | 镜像来源 | 说明 |
| --- | --- | --- | --- |
| `docker-compose.release.yml` | 正式发布 | `ghcr.io/jeron-lgy/moviemuse` | 普通用户部署首选，不需要源码构建。 |
| `docker-compose.yml` | 本地源码构建 | 本地 `docker build` | 开发者或发布前验证使用。 |
| `docker-compose.ui-test.yml` | 联调测试 | 本地 `docker build` | 默认只读挂载媒体，避免误移动。 |

## 正式发布部署

在 Unraid 上准备目录：

Unraid创建以下几个固定目录

```bash
/mnt/user/appdata/moviemuse
/mnt/user/media
/mnt/user/media/trash
/mnt/user/media/study3
/mnt/user/media/study_h265
```
这些是我的固定目录，懂目录映射的就随便写就好了，不懂的就按我这个一模一样做

复制粘贴以下docker compose：

```bash
name: moviemuse
services:
  moviemuse:
    image: ghcr.io/jeron-lgy/moviemuse:${MOVIEMUSE_IMAGE_TAG:-latest}
    container_name: moviemuse
    user: "99:100"
    mem_limit: 2g
    memswap_limit: 2g

    ports:
      # 左边是 Unraid 对外 WebUI 端口，冲突时只改左边。
      - "${MOVIEMUSE_HTTP_PORT:-18188}:18180"

    environment:
      MEDIA_DIRS: /media
      TRASH_DIR: /media/trash
      APP_DATA_DIR: /data
      UNRAID_MOUNT_ROOT: /unraid
      UNRAID_TRASH_RELATIVE: media/trash
      MOVIEMUSE_TIMEZONE: Asia/Shanghai
      WHISPER_MODEL: large-v3-turbo
      NO_PROXY: localhost,127.0.0.1,192.168.0.0/16,10.0.0.0/8,172.16.0.0/12
      no_proxy: localhost,127.0.0.1,192.168.0.0/16,10.0.0.0/8,172.16.0.0/12

    volumes:
      # 左边换成你的 Unraid 媒体真实目录。
      # /mnt/user/media:/media
      - ${MOVIEMUSE_MEDIA_DIR:-/mnt/user/media}:/media
      - ${MOVIEMUSE_DATA_DIR:-/mnt/user/appdata/moviemuse/data}:/data
      # 同盘快速移动需要保留；回收站默认在 /mnt/user/media/trash。
      - /mnt:/unraid

    command: >
      sh -c "umask 022 && uvicorn app.main:app --host 0.0.0.0 --port 18180"

    restart: unless-stopped
```
如果你是按照我上面目录创建的话
最终目录映射就是
```bash
    volumes:
      - /mnt/user/media:/media
      - /mnt/user/appdata/moviemuse:/data
      - /mnt:/unraid
```

首次安装初始化数据目录权限：

```bash
mkdir -p /mnt/user/appdata/moviemuse/data
chown -R 99:100 /mnt/user/appdata/moviemuse/data
chmod -R u+rwX,g+rwX /mnt/user/appdata/moviemuse/data
```

编辑 `docker-compose.release.yml`，通常只需要确认：

| 参数 | 说明 |
| --- | --- |
| `MOVIEMUSE_MEDIA_DIR` | Unraid 媒体目录，例如 `/mnt/user/media`。 |
| `MOVIEMUSE_DATA_DIR` | 配置和任务数据目录，例如 `/mnt/user/appdata/moviemuse/data`。 |
| `MOVIEMUSE_HTTP_PORT` | WebUI 对外端口，默认 `18188`。 |

Windows 算力端地址、Unraid 回调地址、路径映射、翻译 API 和后处理目录都在 WebUI 里配置，不需要写进正式 yml。

启动：

```bash
docker compose -f docker-compose.release.yml up -d
```

访问：

```text
http://UNRAID-IP:18188
```

## 本地源码构建

从项目根目录运行：

```bash
docker compose -f deploy/unraid-frontend/docker-compose.yml up -d --build
```

## 联调测试

测试 yml 默认只读挂载媒体目录：

```bash
docker compose -f deploy/unraid-frontend/docker-compose.ui-test.yml up -d --build
```

如需验证移动/回收站行为，再按测试范围去掉媒体挂载的 `:ro`。
