# Jable Downloader CLI

一个面向主流 Linux VPS/NAS 的命令行下载工作流：输入番号后，自动规范化番号、检查重复文件、搜索 Jable、用 headed Chromium 加载详情页并捕获主视频 M3U8，再交给 N_m3u8DL-RE 下载、FFmpeg 合并并归档到媒体目录。

核心流程来自实际验证通过的稳定版本：`ulimit -n 65535` 与 `--use-ffmpeg-concat-demuxer` 已用于 1972、2295 分片的长视频合并场景。

> 仅用于你有权访问和下载的内容。项目不绕过 DRM、付费墙或访问控制。请遵守所在地法律、站点条款和内容版权。

## 功能

- 支持 `n` 交互输入，以及 `n IPX-850`、`n ipx850`、`n "IPX 850"`
- 统一规范化为 `IPX-850`
- 在访问网站之前检查 `downloads` 和 `media`，避免重复下载
- Jable 搜索页解析，只选择 `/videos/` 作品详情页
- Xvfb + headed Chromium + Playwright + 持久 Chromium profile
- 优先且默认只使用 `mushroomtrack.com` 的 M3U8，避免误抓页面其他 HLS 资源
- N_m3u8DL-RE 自动选流、下载、解密可正常访问的 HLS、FFmpeg 合并
- 固定启用 `--use-ffmpeg-concat-demuxer`，入口同时设置 `ulimit -n 65535`
- `work → downloads → media` 分层；成功后清理分片，失败/中断时保留分片用于恢复
- 成功后用 ffprobe 显示视频/音频编码、分辨率、时长、大小、总耗时和成品路径
- 全局互斥锁，避免两个任务共用同一 Chromium profile
- 路径、超时、站点、首选 CDN 和额外下载参数均可配置

## 系统影响边界

安装脚本只会：

1. 安装 Chromium、Xvfb、FFmpeg、Python venv、Git、curl 等系统依赖；
2. 写入 `/opt/jable-downloader`、`/etc/jable-downloader`、`/var/lib/jable-downloader`；
3. 安装 `/usr/local/bin/n` 和（系统尚不存在时）`/usr/local/bin/N_m3u8DL-RE`；
4. 创建配置中的 `work`、`downloads`、`media` 目录。

它不会修改 Docker、端口、防火墙、挂载点、qBittorrent、MoviePilot 或它们的配置/媒体库。若系统原本已有 `/usr/local/bin/n`，安装器会先备份，卸载时恢复。若系统原本已有 N_m3u8DL-RE，则复用且卸载时不删除。

在 Enterprise Linux 系上，安装 FFmpeg 与 Chromium 可能需要启用 EPEL、CRB/PowerTools 和 RPM Fusion Free。安装器会自动完成适用步骤；这些软件源在普通卸载时保留，不会擅自移除系统仓库配置。

CentOS 7/8 的官方镜像已经归档。只有显式传入 `--enable-eol-repos` 时，安装器才会先完整备份 `/etc/yum.repos.d`，再将 CentOS 基础仓库切换到官方 Vault。归档仓库没有新的安全更新，旧版 Chromium 也可能无法通过站点后来升级的浏览器检查，因此归类为兼容支持而不是正式支持。

如果系统 Python 低于 3.9 或无法创建 venv（CentOS 7/8 常见），安装器使用 uv 在 `/opt/jable-downloader` 内安装项目私有 Python 3.12，不替换系统 Python。

## 一键安装

安装器读取 `/etc/os-release` 并自动选择 `apt`、`dnf/yum`、`pacman`、`zypper` 或 `apk`：

| 系统 | 状态 | 依赖来源 |
|---|---|---|
| Debian 12/13、Ubuntu 22.04/24.04 | 正式支持 | `apt` |
| Fedora（受维护版本） | 正式支持 | `dnf` |
| Arch Linux | 正式支持 | `pacman` |
| openSUSE Tumbleweed | 正式支持 | `zypper` |
| CentOS Stream 9/10、Rocky/AlmaLinux/RHEL 8–10 | 尽力支持 | `dnf` + EPEL/RPM Fusion；RHEL 需有效订阅 |
| openSUSE Leap | 尽力支持 | Chromium/FFmpeg 可用性随版本变化 |
| CentOS Linux 7/8 | EOL 兼容支持 | `yum/dnf` + CentOS Vault；必须显式授权 |
| Alpine Linux | 实验支持 | `apk` + musl 版 N_m3u8DL-RE；需预装 Bash |

安装需要 root 权限。VPS 已登录 root 时直接使用下面的主命令；普通用户使用后面的 `sudo` 版本。

### 一键安装（推荐）

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/TonyStarkJr2021/jable-downloader-cli/main/install.sh)
```

普通用户（非 root）：

```bash
curl -fsSL https://raw.githubusercontent.com/TonyStarkJr2021/jable-downloader-cli/main/install.sh | sudo bash
```

默认数据根目录为 `/mnt/raid_hdd/AV`。root 用户换路径：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/TonyStarkJr2021/jable-downloader-cli/main/install.sh) --data-root /data/AV
```

### 从克隆的仓库安装

```bash
git clone https://github.com/TonyStarkJr2021/jable-downloader-cli.git
cd jable-downloader-cli
sudo bash install.sh
```

上面的一键安装命令适用于 Debian/Ubuntu、Fedora、Arch、openSUSE 以及受维护的 Enterprise Linux 系。

CentOS 7/8 必须显式接受 EOL 风险与 Vault repo 修改：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/TonyStarkJr2021/jable-downloader-cli/main/install.sh) --enable-eol-repos
```

Alpine 最小系统通常没有 Bash；以 root 执行：

```bash
apk add --no-cache bash curl git && curl -fsSL https://raw.githubusercontent.com/TonyStarkJr2021/jable-downloader-cli/main/install.sh | bash
```

安装器会从 [N_m3u8DL-RE 官方 Releases](https://github.com/nilaoda/N_m3u8DL-RE/releases) 自动选择当前 CPU 架构的最新 Linux 版本；不在本仓库中重新分发第三方二进制。

## 使用

```bash
n IPX-850
n ipx850
n "IPX 850"
n
```

单独运行 `n` 时会提示输入番号。建议第一次运行后不要删除持久 profile；页面产生的正常 Cookie 和本地存储会保存在 `/var/lib/jable-downloader/chromium-profile`。

如果已存在 `/mnt/raid_hdd/AV/media/IPX-850.mp4`（扩展名和大小写可不同），程序会在启动浏览器之前退出。

## 目录结构

### GitHub 仓库

```text
jable-downloader-cli/
├── .github/workflows/ci.yml
├── bin/n
├── docs/RELEASE_NOTES_v1.0.0.md
├── tests/test_core.py
├── tests/test_installer_layout.py
├── config.example.json
├── install.sh
├── update.sh
├── uninstall.sh
├── jable_downloader.py
├── requirements.txt
├── VERSION
├── LICENSE
├── NOTICE.md
└── README.md
```

### 安装后

```text
/opt/jable-downloader/                 程序、venv、更新/卸载脚本
/etc/jable-downloader/config.json      用户配置（更新不会覆盖）
/etc/jable-downloader/source.env       更新源和安装所有权记录
/var/lib/jable-downloader/             持久 Chromium profile
/usr/local/bin/n                       全局入口
/usr/local/bin/N_m3u8DL-RE             下载器（仅在原本不存在时安装）
/mnt/raid_hdd/AV/work                  TS/M4S 临时分片工作区
/mnt/raid_hdd/AV/downloads             合并后的待归档文件
/mnt/raid_hdd/AV/media                 正式成品
```

## 配置

编辑：

```bash
sudo nano /etc/jable-downloader/config.json
```

默认配置见 [`config.example.json`](config.example.json)：

| 字段 | 作用 |
|---|---|
| `work_dir` | N_m3u8DL-RE 临时分片所在的 RAID 工作目录 |
| `download_dir` | 下载器合并后的输出目录 |
| `media_dir` | 最终媒体库；下载成功后移动到这里 |
| `browser_profile` | Chromium 持久 profile，更新/普通卸载不会删除 |
| `chromium` | 系统 Chromium 可执行文件 |
| `n_m3u8dl_re` | N_m3u8DL-RE 可执行文件 |
| `site` | Jable 站点根地址 |
| `m3u8_preferred_domain` | 主视频 CDN 域名，默认 `mushroomtrack.com` |
| `allow_m3u8_fallback` | 是否允许在未捕获首选域名时使用其他 M3U8；默认关闭 |
| `page_timeout_ms` | 页面导航超时 |
| `search_wait_ms` | 搜索页渲染等待时间 |
| `capture_timeout_ms` | 详情页 M3U8 捕获等待时间 |
| `n_m3u8dl_extra_args` | 追加给 N_m3u8DL-RE 的字符串参数数组 |

路径修改后手动创建并确认权限，例如：

```bash
sudo mkdir -p /data/AV/{work,downloads,media}
```

不要把并发参数盲目拉满；真实速度通常受 CDN 单 IP/连接限制影响。

## 更新

在仓库目录中：

```bash
git pull --ff-only
sudo bash update.sh
```

若安装时记录了远程仓库，也可在任意目录执行：

```bash
sudo /opt/jable-downloader/update.sh
```

更新只替换主程序、依赖清单、配置模板和管理脚本；不会覆盖 `/etc/jable-downloader/config.json` 或 Chromium profile。旧程序会备份到 `/opt/jable-downloader/backups/时间戳/`。

## 卸载

保留配置和 Chromium profile：

```bash
sudo /opt/jable-downloader/uninstall.sh
```

同时删除配置和 profile：

```bash
sudo /opt/jable-downloader/uninstall.sh --purge
```

CentOS 7/8 如需同时恢复安装前被停用的 repo 文件：

```bash
sudo /opt/jable-downloader/uninstall.sh --restore-repos
```

也可以组合使用 `--purge --restore-repos`。恢复的是安装器改名保留的原文件；时间戳备份目录仍保留，避免自动删除系统级备份。

两种方式都不会删除 `work`、`downloads`、`media` 以及任何媒体文件，也不会卸载系统依赖或删除软件源。只有由本项目安装的 N_m3u8DL-RE 才会被删除。CentOS 7/8 的原 repo 备份路径会在安装和卸载输出中显示，脚本不会自动恢复失效的旧镜像配置。

## 从本地成品发布到 GitHub

下面是一套把本目录发布到 `TonyStarkJr2021/jable-downloader-cli` 并创建 Release 的完整流程。

### 1. 本地验收

Linux/macOS/WSL：

```bash
python3 -m py_compile jable_downloader.py
python3 -m unittest discover -s tests -v
bash -n install.sh update.sh uninstall.sh bin/n
git diff --check
```

至少在干净的 Debian/Ubuntu、Fedora 和 CentOS Stream 9 VPS 上做安装冒烟测试，并确认 `/etc/jable-downloader/config.json` 的路径指向测试目录。CentOS 7/8 与 Alpine 应分别验证兼容/实验路径。先用不存在的测试番号验证规范化和搜索失败，再用你有权下载的新番号做一次真实端到端测试。不要把浏览器 profile、Cookie、真实媒体或带签名的 M3U8 URL 提交到 GitHub。

### 2. 创建本地 Git 历史

```bash
git init
git add .
git update-index --chmod=+x install.sh update.sh uninstall.sh bin/n
git commit -m "Initial public release"
git branch -M main
```

### 3. 创建 GitHub 仓库并推送

网页方式：在 GitHub 账号 `TonyStarkJr2021` 下新建一个空仓库 `jable-downloader-cli`，不要额外生成 README、License 或 `.gitignore`，然后：

```bash
git remote add origin https://github.com/TonyStarkJr2021/jable-downloader-cli.git
git push -u origin main
```

如果已安装 GitHub CLI：

```bash
gh repo create TonyStarkJr2021/jable-downloader-cli --public --source=. --remote=origin --push
```

推送后打开仓库的 Actions 页，确认 `CI` 工作流通过。README 会由 GitHub 自动作为仓库首页展示，无需单独“发布 README”。建议在仓库 About 中加入描述、主题 `python`、`playwright`、`m3u8`、`debian`，并按需开启 Issues。

### 4. 验证一键安装

仓库推送完成后，从 README 复制一键安装命令，在干净 VPS 上实际执行一次。确认 raw 文件已经公开可访问，并验证安装、`n` 命令、更新和卸载流程。

### 5. 创建 v1.0.0 Release

确保 [`VERSION`](VERSION) 为 `1.0.0`，工作树干净且 CI 已通过：

```bash
git tag -a v1.0.0 -m "Jable Downloader CLI v1.0.0"
git push origin v1.0.0
```

网页方式：GitHub → Releases → Draft a new release → 选择 `v1.0.0` → Generate release notes → 标题填写 `Jable Downloader CLI v1.0.0` → Publish release。

GitHub CLI 方式：

```bash
gh release create v1.0.0 --title "Jable Downloader CLI v1.0.0" \
  --notes-file docs/RELEASE_NOTES_v1.0.0.md
```

GitHub 会自动提供 Source code 压缩包，因此不必手工上传包含重复文件的 zip。Release 说明建议明确写出：支持的系统/架构、默认路径、经过验证的分片规模、升级方法，以及“不会改动 qBittorrent/MoviePilot”的边界。

### 6. 后续版本

每次发布：修改代码和测试 → 更新 `VERSION` → 提交并推送 → 等待 CI → 创建新 tag/Release。遵循语义化版本，例如修复为 `v1.0.1`，兼容功能为 `v1.1.0`，不兼容变更为 `v2.0.0`。

## 故障排查

### 搜索返回 403 或停在验证页

不要反复删除 profile。确认程序确实由 `n` 启动（headed Chromium + Xvfb），并保留 `/var/lib/jable-downloader/chromium-profile`。站点风控策略可能变化；本项目不提供绕过验证码或访问控制的功能。

### 没捕获到 mushroomtrack M3U8

先重试并适当增加 `capture_timeout_ms`。如果站点确实更换了主 CDN，确认新域名属于实际主视频后再修改 `m3u8_preferred_domain`。不要直接开启 `allow_m3u8_fallback` 来碰运气，否则可能抓到广告或低清 HLS。

### Too many open files

务必通过 `n` 启动。入口会设置 `ulimit -n 65535`，主程序直接运行时也会尝试提升 soft limit；下载命令固定包含 `--use-ffmpeg-concat-demuxer`。

### 下载失败后 work 目录还有分片

这是有意保留，便于恢复且避免重新下载。成功任务会自动清理。确认不再需要后，只删除该番号对应的精确子目录，不要清空整个 RAID 或媒体库。

### 调试当前配置

```bash
sudo cat /etc/jable-downloader/config.json
command -v chromium ffmpeg ffprobe xvfb-run N_m3u8DL-RE n
```

## 上游项目

- [N_m3u8DL-RE](https://github.com/nilaoda/N_m3u8DL-RE)
- [Playwright for Python](https://playwright.dev/python/)
- [FFmpeg](https://ffmpeg.org/)

本仓库代码使用 [MIT License](LICENSE)。第三方程序遵循各自许可证，详见 [NOTICE](NOTICE.md)。

