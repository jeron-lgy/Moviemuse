# Unraid 宿主机运维源码

这个目录保存 Unraid 自身维护脚本的本地唯一源码。它不是 MovieMuse Web、Windows
算力端或稳定性监控中的任何一个运行单元，也不会进入 MovieMuse 容器。

## 目录约定

```text
ops/unraid/
  user-scripts/
    <脚本名>/
      <脚本名>.sh   本地唯一源码
      README.md     现场路径、数据、调度、验证和回滚
      tests/        不接触正式数据的行为测试
```

当前脚本：

| 名称 | 功能 | 本地源码 | Unraid 现场副本 |
| --- | --- | --- | --- |
| `u_backup` | 生成、校验和保留 Unraid 启动盘备份 | `user-scripts/u_backup/u_backup.sh` | `/boot/config/plugins/user.scripts/scripts/u_backup/script` |

## 管理边界

- 先修改和验证本地文件，再同步到 Unraid；禁止把现场手改内容当成最新源码。
- 脚本同步、User Scripts 调度变更和脚本实际执行是三项独立外部变更。
- 每个脚本只操作 README 明确列出的目标，不借机管理 MovieMuse、Docker、虚拟机或监控。
- 运维脚本使用独立 Git 提交，不与业务或监控改动混合。
