# u_backup：Unraid 启动盘备份

## 文件与数据归属

| 项目 | 路径 | 属性 |
| --- | --- | --- |
| 本地唯一源码 | `ops/unraid/user-scripts/u_backup/u_backup.sh` | 所有修改从这里开始 |
| Unraid User Scripts 副本 | `/boot/config/plugins/user.scripts/scripts/u_backup/script` | 只由本地源码同步覆盖 |
| 持久备份目录 | `/mnt/user/backup/u_pan/flash-backups/` | 启动盘 ZIP、SHA-256 旁车文件和人工审计清单 |
| User Scripts 调度 | `/boot/config/plugins/user.scripts/schedule.json` | 外部状态，不纳入 Git |
| 互斥锁 | `/var/lock/u_backup.lock` | 临时运行状态，重启后可重建 |

历史文件 `/mnt/user/backup/u_pan/u_pan_flash` 实际是一份没有 `.zip` 后缀的旧 ZIP，
不是目录。本脚本不会修改它，也不会修改同级的 2024 年旧备份。

## 安全行为

脚本使用 Unraid 自带的 `flash_backup`，但不相信命令返回即代表成功：

1. 使用 `flock -n` 防止重入。
2. 发现根目录已有 `*-boot-backup-*.zip` 时立即停止，避免失败后每天继续累积。
3. 同时检查 rootfs 可用容量和宿主机 `MemAvailable`；任一不足都在生成前停止，
   避免虚拟机或容器高占用时由备份触发宿主 OOM。
4. 生成后验证文件名、符号链接、实际来源目录和 ZIP 完整性。
5. 先复制到持久目录的唯一临时文件。
6. 比较源文件与临时副本 SHA-256，并再次验证 ZIP。
7. 原子发布 ZIP 和 `.sha256` 后，才删除 RAM 根目录源文件及对应符号链接。
8. 只清理持久目录中超过 15 天的同类 ZIP；不递归删除，且永远保护本轮新备份。

任何校验失败都会以非零状态退出，并保留源备份供人工检查。下一轮会因残留检测而
停止，因此不会形成每天新增一份的写入风暴。

## 部署与调度

先在本地运行语法和行为测试，再把 `u_backup.sh` 同步为：

```text
/boot/config/plugins/user.scripts/scripts/u_backup/script
```

同步后校验本地和远端 SHA-256。首次部署应保持调度为 `disabled`，手动成功运行一次、
确认根目录没有残留并核对持久 ZIP 后，才将 User Scripts 频率设回 `daily`。

## 验证

```bash
bash -n u_backup.sh
bash tests/test-u-backup.sh
```

实机成功条件：

- User Scripts 返回 0。
- `/` 没有 `*-boot-backup-*.zip`。
- `/usr/local/emhttp/` 没有本次备份的悬空链接。
- 持久目录新增一个有效 ZIP 和一个可通过 `sha256sum -c` 的旁车文件。
- `Shmem` 在运行期间短暂上升，完成后回到原基线附近。

## 回滚

先把 `u_backup` 调度设为 `disabled`。不要恢复已知有缺陷的旧脚本；如需回退代码，
回退本目录的独立 Git 提交、重新验证，再同步所选版本。停用该脚本不会影响
MovieMuse、Docker 或虚拟机。
