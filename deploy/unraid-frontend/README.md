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

```bash
mkdir -p /mnt/user/appdata/moviemuse
cd /mnt/user/appdata/moviemuse
```

下载正式 compose：

```bash
curl -L -o docker-compose.release.yml \
  https://raw.githubusercontent.com/jeron-lgy/Moviemuse/main/deploy/unraid-frontend/docker-compose.release.yml
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
