# Jable Downloader CLI v1.0.0

首个公开稳定版。

## 主要功能

- `n`、`n IPX-850`、`n ipx850`、`n "IPX 850"` 四种入口
- 番号标准化与下载前查重
- Jable 搜索、详情页识别、headed Chromium + Xvfb + 持久 profile
- 优先捕获 `mushroomtrack.com` 主视频 M3U8
- N_m3u8DL-RE 下载与 FFmpeg 合并
- `ulimit -n 65535` + `--use-ffmpeg-concat-demuxer`
- RAID `work/downloads/media` 分层与成功后自动清理
- ffprobe 成品编码、分辨率、时长、大小和总耗时摘要
- 可保留配置/profile 的更新和卸载流程
- 同一安装入口自动适配 apt、dnf/yum、pacman、zypper 与 apk 系发行版
- CentOS 7/8 提供需显式授权的 Vault 兼容安装模式

## 验证情况

核心方案已在 1972、2295 分片的长视频场景完成实际验证。CI 检查 Python 语法、核心单元测试和所有 shell 入口语法。

## 安装

请按仓库 README 的“一键安装”部分操作。安装器会自动识别 apt、dnf/yum、pacman、zypper 或 apk。默认数据根目录为 `/mnt/raid_hdd/AV`，可在安装时或配置文件中修改。

## 系统边界

安装器不修改 qBittorrent、MoviePilot、Docker、端口、防火墙或挂载点。卸载不会删除媒体、downloads 或 work 目录。

仅用于你有权访问和下载的内容。本项目不绕过 DRM、付费墙或访问控制。
