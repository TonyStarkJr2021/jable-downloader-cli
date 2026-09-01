# v2.2.0

## 新增

- 普通 JAV 自动归档到 `media/JAV`。
- `FC2-PPV-*` 自动归档到 `media/FC2`。
- 新增可覆盖配置 `jav_media_dir` 与 `fc2_media_dir`。
- Web 媒体列表支持递归浏览两个分类目录，并显示所属分类。

## 升级兼容

- v2.1.1 及更早配置会自动从原 `media_dir` 派生两个分类目录。
- 更新器只创建目录，不自动移动、覆盖或删除现有媒体。
- 查重与 Web 列表继续递归识别 `media` 根目录中的旧版成品。
- 更新前的应用文件与配置一并备份到 `/opt/jable-downloader/backups/`。
- 卸载器继续保留媒体根目录、JAV/FC2 分类目录、downloads 和 work。

## 部署说明

Jellyfin 的 Docker 映射保持 `/mnt/raid_hdd/AV/media:/media`，仅将 JAV 媒体库路径调整为 `/media/JAV`，并新增 `/media/FC2` 库。完整迁移流程见 README。
