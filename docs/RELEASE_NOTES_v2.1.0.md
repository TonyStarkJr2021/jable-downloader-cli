# Jable + MissAV Downloader v2.1.0

新增 Jable 与 MissAV 自动识别，并为 FC2 资源加入完整下载链路。

## 新功能

- FC2 番号自动使用 MissAV，普通番号优先使用 Jable、未找到时转到 MissAV
- 支持 `FC2-PPV-1234567`、`fc2ppv1234567`、`FC2 PPV 1234567` 等输入形式
- 支持直接粘贴 Jable 或 MissAV 详情页链接
- MissAV 优先安全解包公开播放器中的 HLS 信息，失败时自动使用 headed Chromium、Xvfb 和持久 profile 捕获 M3U8
- MissAV/surrit 流通过仅监听 `127.0.0.1` 的临时 HLS 转发层交给 N_m3u8DL-RE
- Web 首页显示自动识别规则并扩展输入提示

## 兼容性

- 保留现有 Jable 搜索、查重、下载、RAID 归档、清理和 ffprobe 摘要逻辑
- 一键更新自动补齐 MissAV 配置，不覆盖路径、Web 账号、端口或 Chromium profile
- 继续使用 `--use-ffmpeg-concat-demuxer`、`ulimit -n 65535` 和全局命令 `n`
- 不处理 DRM、付费墙、验证码或其他访问控制
