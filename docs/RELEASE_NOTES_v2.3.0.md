# v2.3.0

## JavBus 磁链回退

- 普通番号在 Jable 与 MissAV 均未找到直链时，Web 页面自动查询 JavBus 并展示候选磁链。
- 推荐顺序依次为“高清中文字幕、高清、中文字幕、其他”；同一组内优先分享日期较新、文件较大的资源。
- 第一条候选资源突出显示为“推荐”，所有候选项都可直接复制磁力链接。
- 只提供链接，不内置 BT 下载器、不要求 qBittorrent，也不会自动创建下载任务。

## 查询与展示安全

- 仅允许 JavBus 官方 HTTPS 域名，并校验详情页与磁力列表的最终跳转地址。
- 只保留匹配当前番号、包含有效 BTIH 的磁链，过滤重复、无关或异常超长链接。
- 最多展示 30 条结果；网络异常或页面结构变化不会误触发直链下载。
- 页面提示保持单行显示，窄屏下可横向查看，不挤压候选资源卡片。

## 安全升级与兼容性

- 一键安装和一键更新自动补齐 `javbus_fallback_enabled`、`javbus_site` 与 `javbus_timeout_seconds`，并保留用户已有配置。
- 继续沿用 v2.2.1 的 `media/JAV/番号` 与 `media/FC2/番号` 独立目录结构。
- 不移动或删除现有媒体，不修改 Web 账号、端口、Chromium profile、Jellyfin、MetaTube、PostgreSQL、Docker、挂载点或外部 `update-media` 命令。
- CLI、Jable/MissAV 直链下载与旧媒体递归查重保持兼容。
