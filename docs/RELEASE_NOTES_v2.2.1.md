# v2.2.1

## 独立番号目录

- 普通 JAV 新成品归档为 `media/JAV/番号/番号.mp4`。
- FC2 新成品归档为 `media/FC2/番号/番号.mp4`。
- 遵循 Jellyfin 电影库的一片一目录结构，避免多个 FC2 被误合并为同一条目的不同版本。
- Web 媒体库继续递归显示和下载新结构中的成品。

## 安全升级与迁移

- v2.2.0 及更早版本的平铺成品仍可被递归查重，不会重复下载。
- 一键更新不会擅自移动、覆盖或删除现有媒体。
- 新增 `migrate_media_layout.py`：默认只预览迁移计划，传入 `--apply` 后才执行。
- 迁移工具同时支持旧 `media` 根目录和 `media/JAV`、`media/FC2` 平铺文件，并将同番号视频、封面、字幕和 NFO 归入同一目录。
- 任意目标冲突都会在移动前整体停止，避免部分迁移或覆盖已有文件。

## 部署兼容性

- 保留现有配置、Web 账号、端口、Chromium profile、下载缓存和全部媒体。
- Jellyfin Docker 映射仍保持 `/mnt/raid_hdd/AV/media:/media`；JAV 与 FC2 媒体库路径仍分别使用 `/media/JAV` 和 `/media/FC2`。
- 不修改 Jellyfin、MetaTube、PostgreSQL、Docker、qBittorrent、MoviePilot、挂载点、防火墙或外部 `update-media` 命令。
