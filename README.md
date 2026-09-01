# Jable Downloader CLI

输入番号，自动搜索 Jable、捕获主视频 M3U8，并使用 N_m3u8DL-RE 下载和合并。安装完成后，全局命令为 `n`。

核心流程经过 1972、2295 分片长视频实测，固定启用 `ulimit -n 65535` 与 `--use-ffmpeg-concat-demuxer`。

> 仅用于你有权访问和下载的内容。本项目不绕过 DRM、付费墙或访问控制。

## 功能

- 支持 `n`、`n IPX-850`、`n ipx850`、`n "IPX 850"`
- 自动统一为 `IPX-850` 格式并检查重复文件
- 自动搜索作品并解析详情页
- headed Chromium + Xvfb + Playwright + 持久浏览器 profile
- 优先捕获 `mushroomtrack.com` 主视频 M3U8
- N_m3u8DL-RE 下载，FFmpeg 合并
- RAID `work → downloads → media` 目录分层
- 成功后清理临时分片，失败或中断时保留现场
- ffprobe 显示编码、分辨率、时长、大小、总耗时和成品路径
- 配置、浏览器 profile 与媒体文件在更新时保持不变

## 支持的系统

| 系统 | 支持状态 |
|---|---|
| Debian 12/13、Ubuntu 22.04/24.04 | 正式支持 |
| Fedora、Arch Linux、openSUSE Tumbleweed | 正式支持 |
| CentOS Stream 9/10、Rocky Linux、AlmaLinux、RHEL 8–10 | 尽力支持 |
| openSUSE Leap | 尽力支持 |
| CentOS Linux 7/8 | EOL 兼容支持，需要额外参数 |
| Alpine Linux | 实验支持 |

支持 `x86_64` 和 `aarch64/arm64`。安装器会自动识别系统及 CPU 架构，并使用 `apt`、`dnf/yum`、`pacman`、`zypper` 或 `apk` 安装依赖。

## 一键安装

root 用户：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/TonyStarkJr2021/jable-downloader-cli/main/install.sh)
```

普通用户：

```bash
curl -fsSL https://raw.githubusercontent.com/TonyStarkJr2021/jable-downloader-cli/main/install.sh | sudo bash
```

默认数据目录为 `/mnt/raid_hdd/AV`。指定其他目录：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/TonyStarkJr2021/jable-downloader-cli/main/install.sh) --data-root /data/AV
```

CentOS 7/8 已停止维护，必须明确允许使用 Vault 归档软件源：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/TonyStarkJr2021/jable-downloader-cli/main/install.sh) --enable-eol-repos
```

Alpine 最小系统以 root 执行：

```bash
apk add --no-cache bash curl git && curl -fsSL https://raw.githubusercontent.com/TonyStarkJr2021/jable-downloader-cli/main/install.sh | bash
```

## 使用

```bash
n IPX-850
n ipx850
n "IPX 850"
n
```

单独运行 `n` 时会提示输入番号。如果 `downloads` 或 `media` 已存在同番号成品，程序会在打开浏览器前退出，避免重复下载。

## 一键更新

root 用户：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/TonyStarkJr2021/jable-downloader-cli/main/update.sh)
```

普通用户或直接使用已安装脚本：

```bash
sudo /opt/jable-downloader/update.sh
```

更新不会覆盖 `/etc/jable-downloader/config.json` 和 Chromium profile。旧程序会备份到 `/opt/jable-downloader/backups/`。

## 一键卸载

保留配置、Chromium profile 和全部媒体文件：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/TonyStarkJr2021/jable-downloader-cli/main/uninstall.sh)
```

同时删除配置和 Chromium profile：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/TonyStarkJr2021/jable-downloader-cli/main/uninstall.sh) --purge
```

CentOS 7/8 如需恢复安装前的 repo 文件：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/TonyStarkJr2021/jable-downloader-cli/main/uninstall.sh) --restore-repos
```

卸载始终保留 `work`、`downloads`、`media` 和其中的媒体文件。

## 配置

```bash
sudo nano /etc/jable-downloader/config.json
```

| 字段 | 说明 |
|---|---|
| `work_dir` | 临时分片目录 |
| `download_dir` | 合并后的待归档目录 |
| `media_dir` | 最终媒体目录 |
| `browser_profile` | Chromium 持久 profile |
| `chromium` | Chromium 可执行文件 |
| `n_m3u8dl_re` | N_m3u8DL-RE 可执行文件 |
| `site` | Jable 站点地址 |
| `m3u8_preferred_domain` | 首选主视频 CDN |
| `allow_m3u8_fallback` | 是否允许备用 M3U8，默认关闭 |
| `page_timeout_ms` | 页面导航超时 |
| `search_wait_ms` | 搜索页等待时间 |
| `capture_timeout_ms` | M3U8 捕获等待时间 |
| `n_m3u8dl_extra_args` | 额外下载参数 |

默认配置参考 [`config.example.json`](config.example.json)。

## 默认路径

```text
/opt/jable-downloader/                 程序和管理脚本
/etc/jable-downloader/config.json      用户配置
/var/lib/jable-downloader/             Chromium profile
/usr/local/bin/n                       全局命令
/usr/local/bin/N_m3u8DL-RE             下载器
/mnt/raid_hdd/AV/work                  临时分片
/mnt/raid_hdd/AV/downloads             待归档文件
/mnt/raid_hdd/AV/media                 正式成品
```

## 系统影响

安装器只安装运行依赖并写入上述项目目录。它不会修改 Docker、端口、防火墙、挂载点、qBittorrent、MoviePilot 或现有媒体库。

如果 `/usr/local/bin/n` 已存在，安装器会先备份，卸载时恢复。系统已有的 N_m3u8DL-RE 会被复用，卸载时不会删除。

## 常见问题

### 搜索返回 403 或停在验证页

请保留 `/var/lib/jable-downloader/chromium-profile` 并重试。站点策略可能变化，本项目不提供绕过验证码或访问控制的功能。

### 没有捕获到主视频 M3U8

先重试或适当增加 `capture_timeout_ms`。确认站点确实更换主 CDN 后，再修改 `m3u8_preferred_domain`。

### 出现 `Too many open files`

请通过 `n` 启动，不要绕过全局入口直接调用下载器。

### 下载失败后仍有临时分片

这是用于恢复下载的预期行为。确认不再需要后，只删除该番号对应的子目录。

## 上游项目与许可证

- [N_m3u8DL-RE](https://github.com/nilaoda/N_m3u8DL-RE)
- [Playwright for Python](https://playwright.dev/python/)
- [FFmpeg](https://ffmpeg.org/)

本仓库代码使用 [MIT License](LICENSE)。第三方程序遵循各自许可证，详见 [NOTICE](NOTICE.md)。

