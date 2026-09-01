# Jable Downloader v2.0.0

v2.0.0 在经过验证的 CLI 下载核心之上新增带身份验证的 Web 控制台，并继续保留全局命令 `n`。

## 新增

- 通过 `服务器 IP:端口` 使用的响应式 Web 控制台
- 安装时随机生成可用高位端口、用户名和强密码，也可手动指定
- scrypt 密码哈希、服务端会话、CSRF 防护和登录限速
- 单任务互斥、实时状态和日志、媒体历史与 ffprobe 信息
- 登录后浏览器下载，支持 HTTP Range/续传
- systemd 服务及 Web 配置的安装、升级保留和卸载清理

## 保持不变

- 成品始终保留在服务器 `media` 目录，本地下载只复制
- Playwright + headed Chromium + Xvfb + 持久 profile
- 优先捕获 `mushroomtrack.com` M3U8
- N_m3u8DL-RE、`--use-ffmpeg-concat-demuxer` 与 `ulimit -n 65535`
- `n` CLI、番号标准化、查重、清理和 RAID 默认路径
- 不修改 qBittorrent、MoviePilot、Docker、挂载点或防火墙

## 升级

运行 README 中的一键更新命令即可。v1 配置、Chromium profile 和媒体会保留；首次升级会生成 Web 登录信息并在终端显示一次。

默认 Web 协议为 HTTP。直接暴露到公网前，建议限制来源 IP 或配置 HTTPS 反向代理。
