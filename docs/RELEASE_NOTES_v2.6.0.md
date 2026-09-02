# v2.6.0

本版本为下载来源与可靠性升级：加入 SupJav，并把自动来源探测改为 Jable、MissAV、SupJav 三站并行。程序会汇总所有可用 HLS，按实际画质排序；首选直链失败后会自动尝试下一条，不会因为单个站点或服务器失效直接终止任务。

## 新增

- SupJav 公开 HLS 解析，支持普通番号及 `FC2-PPV-*` 番号。
- Jable、MissAV、SupJav 并行探测。
- 根据分辨率、码率和时长对跨站候选资源排序。
- SupJav 静态解析失败后的 Chromium 捕获回退。
- SupJav 多服务器候选与短预览过滤。

## 下载可靠性

- 下载失败时自动切换到下一条已解析直链。
- 每个候选使用独立临时名称，失败分片不会混入其他来源。
- 下载成功后统一恢复为标准 `番号.扩展名`。
- 验证 HLS 清单确实包含媒体或变体，不再接受 404、空清单外壳等假候选。
- SupJav 通过本机临时 HLS 转发层保留 Referer、Cookie 与 User-Agent。
- 仅在确认连续 MPEG-TS 同步字节后移除 SupJav 分片前的伪装数据。

## 升级兼容

- `install.sh`、`update.sh` 和 `config.example.json` 已加入 SupJav 默认配置。
- 更新现有部署时保留数据目录、媒体、Web 账号、端口、Jellyfin、MetaTube 与 JavBus 配置。
- 未加入 ROU.Video 或 AVJOY。

## 验证

- 71 项自动化测试全部通过，覆盖核心解析、并发排序、下载回退、HLS 转发、安装升级、Web 与媒体管理。
- 使用 SupJav 当前公开资源完成真实解析验证：同时得到 1080p 与 480p 候选，并成功读取 1080p HLS 的媒体清单和首个 MPEG-TS 分片。

> 仅用于你有权访问和下载的内容。本项目不绕过 DRM、付费墙、验证码或访问控制。
