# Unraid UI 分支联调实例

这份配置用于在现有正式实例旁边验证 UI 分支，不复用正式任务数据。

## 首次字幕联调

将本分支的 `app/`、`frontend/dist/`、`deploy/unraid-frontend/` 按原目录结构拷贝到：

```text
/mnt/user/appdata/moviemuse-ui-test
```

在 Unraid 执行：

```bash
cd /mnt/user/appdata/moviemuse-ui-test
mkdir -p /mnt/user/appdata/moviemuse-ui-test/data
chown -R nobody:users /mnt/user/appdata/moviemuse-ui-test/data
chmod -R u+rwX,g+rwX /mnt/user/appdata/moviemuse-ui-test/data
docker compose -f deploy/unraid-frontend/docker-compose.ui-test.yml up -d --build
```

打开：

```text
http://UNRAID-IP:18182/subtitles
```

默认配置使用占位符 `http://WINDOWS-IP:18181` 和 `/media=\\NAS\media`。首次联调前，按你的 Windows 算力端地址和 SMB 共享路径修改 `docker-compose.ui-test.yml` 对应两项。

首次联调保持媒体挂载为只读。控制台可以扫描并提交字幕任务，Windows 算力端仍然通过 SMB 共享路径写入生成的 SRT。

## 验证移动功能

仅在准备好专门的测试目录后，将 `volumes` 中以下两行末尾的 `:ro` 去掉，再重新启动测试实例：

```yaml
- /mnt/user/media:/media
- /mnt:/unraid
```

## 停止测试实例

```bash
docker compose -f deploy/unraid-frontend/docker-compose.ui-test.yml down
```
