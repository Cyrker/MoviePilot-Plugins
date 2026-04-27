# SSD 卸载到 HDD（SsdOffload）

> MoviePilot V2 插件 · 监听 `TransferComplete`，调用 qBittorrent `setLocation` 把 SSD 缓存盘上的种子数据搬到机械盘。

## 适用场景

```
qb 下载到 SSD  →  MP 整理 rclone_copy 到 115 (hash 在 SSD 上算，速度快)
              →  TransferComplete 事件触发本插件
              →  qb setLocation 把数据从 SSD 搬到 HDD（qb 后台异步执行，不掉种）
              →  自动转移做种插件下次轮询时，把任务交给 tr，save_path 已经是 HDD
              →  qb 任务删除（数据保留给 tr 继续做种）
```

效果：媒体库永久保存在 115，HDD 上是给 tr 长期保种用的副本，SSD 始终保持空闲作为下载缓冲。

## 工作原理

整理完成后 MoviePilot 会广播 `EventType.TransferComplete` 事件，事件数据中包含 `download_hash`、`fileitem`、`downloader`。本插件捕获该事件，当源文件位于配置的 SSD 前缀下时：

1. 用 hash 在 qb 中找到对应种子
2. 按所选策略（**剩余空间最多** / 轮询）挑一个 HDD 前缀
3. 用前缀替换的方式保留子目录结构（`/media/disk4-150G/电影` → `/media/diskN-XXT/电影`）
4. 调用 qb 的 `torrents_set_location()`，qb 自己处理跨盘搬运（copy + delete + 自动恢复做种）

整个过程对 qb 是原子的：搬运期间种子状态变成 `moving`，搬完自动恢复 `seeding`，做种连续不断。

## 关键前提

1. **MP / qb / tr 三个容器对所有盘的容器内路径必须完全一致**。本插件通过路径前缀替换计算目标路径，路径不一致会导致 qb 看不到对应目录。
2. **qb 容器必须能同时访问 SSD 和所有 HDD**。
3. **tr 容器只需访问 HDD**（SSD 不用挂）。
4. 「自动转移做种」插件的轮询周期建议 ≥ 30 分钟，给 qb 跨盘搬运留时间，否则可能在数据没搬完时就把任务交给 tr 导致校验失败。

## 容器路径示例

三个容器都这样挂：

```yaml
volumes:
  - /mnt/hgst-VGJ4PDUG:/media/disk1-8T
  - /mnt/wdc-2BJJ17ZP:/media/disk2-16T
  - /mnt/wdc-2PHWS0PJ:/media/disk3-16T
  - /mnt/data:/media/disk4-150G          # SSD
```

## 配置参考

| 项 | 值 |
| --- | --- |
| 启用插件 | 开 |
| **仅日志（不实际搬）** | **首次开启先打开**，跑两次确认日志正常再关掉 |
| SSD 前缀 | `/media/disk4-150G` |
| HDD 前缀（每行一个） | `/media/disk1-8T`<br>`/media/disk2-16T`<br>`/media/disk3-16T` |
| 选盘策略 | 剩余空间最多 |
| 下载器名称 | 留空（自动挑第一个 qb） |
| 种子标签过滤 | 留空（也可以填 `已整理`） |
| 搬运延时 | 5 |

## 常见问题

**Q: 第一次部署怎么验证？**

按上面配置后开启「仅日志」，手动整理一个测试种子，看 MP 日志：

```
【SsdOffload】[DRY RUN] 将搬运 xxx.mkv (5.23 GB): /media/disk4-150G/电影 -> /media/disk2-16T/电影
```

源、目标、大小都对了，关掉「仅日志」再正式跑。

**Q: setLocation 失败怎么办？**

看日志里 qb 返回的具体错误。常见原因：目标路径权限问题（容器 PUID/PGID 不一致）、目标盘空间不足（插件已带 10GB 余量保护）。

**Q: 搬运过程会重新校验吗？**

不会。qb 的 setLocation 只搬数据并更新内部记录，不触发 hash 校验。

**Q: 为什么不直接 mv 文件？**

MP 容器去搬 qb 的文件，qb 完全不知情，状态会乱（红种）。setLocation 是 qb 自己搬的，期间种子状态 `moving`，搬完自动恢复 `seeding`，做种不掉。

**Q: 跨盘搬运多久？**

按真实 IO 速度，10GB 大概 1-3 分钟，50GB 蓝光原盘十几分钟。

**Q: 自动转移做种插件会不会和这个冲突？**

不会，但要注意时序。本插件搬运是异步的，自动转移做种插件如果在搬完前轮询，看到的还是旧的 SSD 路径。把自动转移做种轮询周期调到 ≥ 30 分钟即可。

## 参与

- 反馈问题：在仓库根目录提 Issue
- 改进代码：欢迎 PR
