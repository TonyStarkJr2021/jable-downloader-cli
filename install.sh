#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="/opt/jable-downloader"
CONFIG_DIR="/etc/jable-downloader"
STATE_DIR="/var/lib/jable-downloader"
COMMAND_PATH="/usr/local/bin/n"
N_M3U8DL_PATH="/usr/local/bin/N_m3u8DL-RE"
RAID_DATA_ROOT="/mnt/raid_hdd/AV"
SYSTEM_DATA_ROOT="/var/lib/jable-downloader-data"
DATA_ROOT="${JABLE_DATA_ROOT:-}"
[[ -n "$DATA_ROOT" ]] && DATA_ROOT_EXPLICIT=true || DATA_ROOT_EXPLICIT=false
DEFAULT_REPO_URL="https://github.com/TonyStarkJr2021/jable-downloader-cli.git"
REPO_URL="${JABLE_REPO_URL:-}"
REPO_BRANCH="${JABLE_REPO_BRANCH:-main}"
if [[ ${JABLE_REPO_BRANCH+x} ]]; then
  BRANCH_EXPLICIT=true
else
  BRANCH_EXPLICIT=false
fi
SOURCE_DIR=""
TEMP_DIR=""
INSTALL_N_M3U8DL=false
COMMAND_BACKUP=""
PLATFORM=""
OS_ID=""
OS_VERSION_ID=""
OS_MAJOR=""
ENABLE_EOL_REPOS=false
LEGACY_REPO_BACKUP=""
WEB_ENABLED=true
WEB_HOST=""
WEB_PORT=""
WEB_USER=""
WEB_PASSWORD=""
WEB_HOST_EXPLICIT=false
WEB_PORT_EXPLICIT=false
WEB_USER_EXPLICIT=false
WEB_PASSWORD_EXPLICIT=false
WEB_SERVICE="/etc/systemd/system/jable-downloader-web.service"

usage() {
  cat <<'EOF'
用法：sudo ./install.sh [选项]

  --data-root PATH   指定数据根目录（默认自动选择挂载硬盘或系统盘）
  --repo URL         从该 Git 仓库安装；用于 curl | bash 场景
  --branch NAME      仓库分支（默认 main）
  --web-host IP      Web 监听地址（默认 0.0.0.0）
  --web-port PORT    指定 Web 端口（默认随机可用高位端口）
  --web-user USER    指定 Web 用户名（默认随机生成）
  --web-password PW  指定 Web 密码（默认随机生成，至少 12 位）
  --no-web           只安装 CLI，不安装 Web 服务
  --enable-eol-repos 允许 CentOS 7/8 备份现有 repo 后切换到归档源
  -h, --help         显示帮助

也可使用 JABLE_DATA_ROOT、JABLE_REPO_URL、JABLE_REPO_BRANCH 环境变量。
EOF
}

while (($#)); do
  case "$1" in
    --data-root) DATA_ROOT="${2:?--data-root 需要路径}"; DATA_ROOT_EXPLICIT=true; shift 2 ;;
    --repo) REPO_URL="${2:?--repo 需要 URL}"; shift 2 ;;
    --branch) REPO_BRANCH="${2:?--branch 需要名称}"; BRANCH_EXPLICIT=true; shift 2 ;;
    --web-host) WEB_HOST="${2:?--web-host 需要 IP}"; WEB_HOST_EXPLICIT=true; shift 2 ;;
    --web-port) WEB_PORT="${2:?--web-port 需要端口}"; WEB_PORT_EXPLICIT=true; shift 2 ;;
    --web-user) WEB_USER="${2:?--web-user 需要用户名}"; WEB_USER_EXPLICIT=true; shift 2 ;;
    --web-password) WEB_PASSWORD="${2:?--web-password 需要密码}"; WEB_PASSWORD_EXPLICIT=true; shift 2 ;;
    --no-web) WEB_ENABLED=false; shift ;;
    --enable-eol-repos) ENABLE_EOL_REPOS=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知选项：$1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "请用 sudo 或 root 运行 install.sh。" >&2
  exit 1
fi

is_mountpoint() {
  local path="$1"
  [[ -d "$path" ]] || return 1
  if command -v mountpoint >/dev/null 2>&1; then
    mountpoint -q -- "$path"
    return
  fi
  [[ -r /proc/self/mountinfo ]] || return 1
  awk -v target="$path" '$5 == target { found = 1 } END { exit !found }' \
    /proc/self/mountinfo
}

select_data_root() {
  if [[ "$DATA_ROOT_EXPLICIT" == true ]]; then
    [[ "$DATA_ROOT" == /* ]] || {
      echo "--data-root 和 JABLE_DATA_ROOT 必须使用绝对路径。" >&2
      exit 2
    }
    echo "使用指定的数据目录：$DATA_ROOT"
    return
  fi
  if is_mountpoint /mnt/raid_hdd || is_mountpoint /mnt/raid_hdd/AV; then
    DATA_ROOT="$RAID_DATA_ROOT"
    echo "检测到已挂载硬盘，数据目录：$DATA_ROOT"
  else
    DATA_ROOT="$SYSTEM_DATA_ROOT"
    echo "未检测到 /mnt/raid_hdd 挂载，自动使用 VPS 系统盘：$DATA_ROOT"
    echo "⚠️ 视频会占用系统盘空间，请留意剩余容量。"
  fi
}

select_data_root

detect_platform() {
  if [[ ! -r /etc/os-release ]]; then
    echo "无法识别系统：缺少 /etc/os-release" >&2
    exit 1
  fi
  # shellcheck disable=SC1091
  source /etc/os-release
  OS_ID="${ID:-unknown}"
  OS_VERSION_ID="${VERSION_ID:-unknown}"
  OS_MAJOR="${OS_VERSION_ID%%.*}"
  case "$OS_ID" in
    debian|ubuntu)
      PLATFORM="debian"
      ;;
    fedora)
      PLATFORM="fedora"
      ;;
    centos)
      case "$OS_MAJOR" in
        7|8)
          if [[ "$ENABLE_EOL_REPOS" != true ]]; then
            echo "检测到已停止维护的 ${PRETTY_NAME:-CentOS $OS_VERSION_ID}。" >&2
            echo "如确认接受归档软件源和旧版 Chromium 风险，请加 --enable-eol-repos。" >&2
            exit 1
          fi
          if [[ "$OS_MAJOR" == "7" && "$(uname -m)" != "x86_64" ]]; then
            echo "CentOS 7 兼容模式仅支持 x86_64。" >&2
            exit 1
          fi
          PLATFORM="centos-legacy"
          ;;
        9|10) PLATFORM="enterprise-linux" ;;
        *) echo "不支持的 CentOS 主版本：$OS_MAJOR" >&2; exit 1 ;;
      esac
      ;;
    rocky|almalinux|rhel|ol)
      case "$OS_MAJOR" in
        8|9|10) PLATFORM="enterprise-linux" ;;
        *) echo "暂不支持的 Enterprise Linux 主版本：$OS_MAJOR" >&2; exit 1 ;;
      esac
      ;;
    arch|archarm)
      PLATFORM="arch"
      ;;
    opensuse-tumbleweed|opensuse-leap)
      PLATFORM="opensuse"
      ;;
    alpine)
      PLATFORM="alpine"
      ;;
    *)
      echo "暂不支持的发行版：${PRETTY_NAME:-$OS_ID $OS_VERSION_ID}" >&2
      echo "当前支持 Debian/Ubuntu、Fedora、Arch、openSUSE、Alpine 及 Enterprise Linux 系。" >&2
      exit 1
      ;;
  esac
  echo "检测到系统：${PRETTY_NAME:-$OS_ID $OS_VERSION_ID}"
}

configure_centos_vault() {
  LEGACY_REPO_BACKUP="/etc/yum.repos.d/jable-backup-$(date +%Y%m%d%H%M%S)"
  install -d -m 0700 "$LEGACY_REPO_BACKUP"
  cp -a /etc/yum.repos.d/. "$LEGACY_REPO_BACKUP/"
  find /etc/yum.repos.d -maxdepth 1 -type f -name 'CentOS-*.repo' \
    -exec mv -f {} {}.jable-eol-disabled \;

  if [[ "$OS_MAJOR" == "7" ]]; then
    cat > /etc/yum.repos.d/jable-centos-vault.repo <<'EOF'
[jable-centos-base]
name=CentOS 7.9.2009 Vault - Base
baseurl=https://vault.centos.org/7.9.2009/os/$basearch/
enabled=1
gpgcheck=1
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-CentOS-7

[jable-centos-updates]
name=CentOS 7.9.2009 Vault - Updates
baseurl=https://vault.centos.org/7.9.2009/updates/$basearch/
enabled=1
gpgcheck=1
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-CentOS-7

[jable-centos-extras]
name=CentOS 7.9.2009 Vault - Extras
baseurl=https://vault.centos.org/7.9.2009/extras/$basearch/
enabled=1
gpgcheck=1
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-CentOS-7
EOF
    yum clean all
    yum makecache
  else
    cat > /etc/yum.repos.d/jable-centos-vault.repo <<'EOF'
[jable-centos-baseos]
name=CentOS 8.5.2111 Vault - BaseOS
baseurl=https://vault.centos.org/8.5.2111/BaseOS/$basearch/os/
enabled=1
gpgcheck=1
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-centosofficial

[jable-centos-appstream]
name=CentOS 8.5.2111 Vault - AppStream
baseurl=https://vault.centos.org/8.5.2111/AppStream/$basearch/os/
enabled=1
gpgcheck=1
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-centosofficial

[jable-centos-powertools]
name=CentOS 8.5.2111 Vault - PowerTools
baseurl=https://vault.centos.org/8.5.2111/PowerTools/$basearch/os/
enabled=1
gpgcheck=1
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-centosofficial

[jable-centos-extras]
name=CentOS 8.5.2111 Vault - Extras
baseurl=https://vault.centos.org/8.5.2111/extras/$basearch/os/
enabled=1
gpgcheck=1
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-centosofficial
EOF
    dnf clean all
    dnf makecache
  fi
  echo "旧 repo 配置备份：$LEGACY_REPO_BACKUP"
}

bootstrap_git() {
  command -v git >/dev/null 2>&1 && return 0
  case "$PLATFORM" in
    debian)
      export DEBIAN_FRONTEND=noninteractive
      apt-get update
      apt-get install -y --no-install-recommends ca-certificates git
      ;;
    fedora|enterprise-linux)
      dnf install -y ca-certificates git
      ;;
    centos-legacy)
      [[ -f /etc/yum.repos.d/jable-centos-vault.repo ]] || configure_centos_vault
      if [[ "$OS_MAJOR" == "7" ]]; then
        yum install -y ca-certificates git
      else
        dnf install -y ca-certificates git
      fi
      ;;
    arch) pacman -Sy --noconfirm --needed ca-certificates git ;;
    opensuse) zypper --non-interactive install ca-certificates git ;;
    alpine) apk add --no-cache ca-certificates git ;;
  esac
}

install_system_dependencies() {
  case "$PLATFORM" in
    debian)
      export DEBIAN_FRONTEND=noninteractive
      apt-get update
      if apt-cache show chromium >/dev/null 2>&1; then
        CHROMIUM_PACKAGE="chromium"
      elif apt-cache show chromium-browser >/dev/null 2>&1; then
        CHROMIUM_PACKAGE="chromium-browser"
      else
        echo "当前软件源中找不到 Chromium 软件包。" >&2
        exit 1
      fi
      apt-get install -y --no-install-recommends \
        ca-certificates curl ffmpeg git python3 python3-venv tar util-linux xvfb \
        "$CHROMIUM_PACKAGE"
      ;;
    fedora)
      dnf install -y \
        ca-certificates chromium curl ffmpeg-free git python3 python3-pip tar \
        util-linux xorg-x11-server-Xvfb
      ;;
    enterprise-linux)
      dnf install -y dnf-plugins-core
      case "$OS_ID:$OS_MAJOR" in
        centos:8|rocky:8|almalinux:8) dnf config-manager --set-enabled powertools || true ;;
        centos:*|rocky:*|almalinux:*) dnf config-manager --set-enabled crb || true ;;
        rhel:*)
          dnf config-manager --set-enabled \
            "codeready-builder-for-rhel-${OS_MAJOR}-$(uname -m)-rpms" || true
          ;;
      esac
      dnf install -y \
        "https://dl.fedoraproject.org/pub/epel/epel-release-latest-${OS_MAJOR}.noarch.rpm"
      if [[ "$OS_ID" == "centos" ]]; then
        dnf install -y epel-next-release || true
      fi
      dnf install -y \
        "https://mirrors.rpmfusion.org/free/el/rpmfusion-free-release-${OS_MAJOR}.noarch.rpm"
      dnf install -y \
        ca-certificates chromium curl ffmpeg git python3 python3-pip tar \
        util-linux xorg-x11-server-Xvfb
      ;;
    centos-legacy)
      [[ -f /etc/yum.repos.d/jable-centos-vault.repo ]] || configure_centos_vault
      if [[ "$OS_MAJOR" == "7" ]]; then
        yum install -y epel-release
        find /etc/yum.repos.d -maxdepth 1 -type f -name 'epel*.repo' \
          -exec mv -f {} {}.jable-eol-disabled \;
        cat > /etc/yum.repos.d/jable-epel-archive.repo <<'EOF'
[jable-epel-archive]
name=EPEL 7.9 Archive
baseurl=https://archives.fedoraproject.org/pub/archive/epel/7.9/$basearch/
enabled=1
gpgcheck=1
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-EPEL-7
EOF
        yum install -y \
          "https://archive.rpmfusion.org/Mirrors/rpmfusion.org/free/el/rpmfusion-free-release-7.noarch.rpm"
        yum install -y \
          ca-certificates chromium curl ffmpeg git python3 python3-pip tar \
          util-linux xorg-x11-server-Xvfb
      else
        dnf install -y \
          "https://dl.fedoraproject.org/pub/epel/epel-release-latest-8.noarch.rpm"
        dnf install -y \
          "https://archive.rpmfusion.org/Mirrors/rpmfusion.org/free/el/rpmfusion-free-release-8.noarch.rpm"
        dnf install -y \
          ca-certificates chromium curl ffmpeg git python3 python3-pip tar \
          util-linux xorg-x11-server-Xvfb
      fi
      ;;
    arch)
      pacman -Syu --noconfirm --needed \
        ca-certificates chromium curl ffmpeg git python python-pip tar \
        util-linux xorg-server-xvfb
      ;;
    opensuse)
      zypper --non-interactive refresh
      zypper --non-interactive install \
        ca-certificates chromium curl git python3 python3-pip tar util-linux xvfb-run
      if ! zypper --non-interactive install ffmpeg; then
        zypper --non-interactive install ffmpeg-7
      fi
      ;;
    alpine)
      apk add --no-cache \
        bash ca-certificates chromium curl ffmpeg git python3 py3-pip tar \
        util-linux xvfb-run
      ;;
  esac

  CHROMIUM_PATH="$(command -v chromium || command -v chromium-browser || true)"
  [[ -n "$CHROMIUM_PATH" ]] || {
    echo "Chromium 安装后仍未找到可执行文件。" >&2
    exit 1
  }
}

create_python_environment() {
  if python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' \
      >/dev/null 2>&1; then
    if python3 -m venv "$APP_DIR/venv" >/dev/null 2>&1; then
      "$APP_DIR/venv/bin/python" -m pip install --disable-pip-version-check --upgrade pip
      "$APP_DIR/venv/bin/python" -m pip install \
        --disable-pip-version-check -r "$APP_DIR/requirements.txt"
      return
    fi
  fi

  echo "系统 Python 过旧或缺少 venv，安装项目私有 Python 3.12..."
  install -d -m 0755 "$APP_DIR/uv-bin" "$APP_DIR/python"
  curl -LsSf https://astral.sh/uv/install.sh | \
    env UV_INSTALL_DIR="$APP_DIR/uv-bin" UV_NO_MODIFY_PATH=1 sh
  UV_PYTHON_INSTALL_DIR="$APP_DIR/python" \
    "$APP_DIR/uv-bin/uv" venv --seed --python 3.12 \
      --python-preference only-managed "$APP_DIR/venv"
  "$APP_DIR/venv/bin/python" -m pip install \
    --disable-pip-version-check -r "$APP_DIR/requirements.txt"
}

detect_platform

# Preserve ownership/backup metadata across an idempotent reinstall.
if [[ -r "$CONFIG_DIR/source.env" ]]; then
  SAVED_REPO_URL="$REPO_URL"
  SAVED_REPO_BRANCH="$REPO_BRANCH"
  # shellcheck disable=SC1090,SC1091
  source "$CONFIG_DIR/source.env"
  if [[ -n "$SAVED_REPO_URL" ]]; then
    REPO_URL="$SAVED_REPO_URL"
  else
    REPO_URL="${JABLE_REPO_URL:-}"
  fi
  if [[ "$BRANCH_EXPLICIT" == true ]]; then
    REPO_BRANCH="$SAVED_REPO_BRANCH"
  else
    REPO_BRANCH="${JABLE_REPO_BRANCH:-main}"
  fi
  [[ "${JABLE_N_M3U8DL_MANAGED:-false}" == true ]] && INSTALL_N_M3U8DL=true
  COMMAND_BACKUP="${JABLE_COMMAND_BACKUP:-}"
  LEGACY_REPO_BACKUP="${JABLE_LEGACY_REPO_BACKUP:-}"
fi

cleanup() {
  if [[ -n "$TEMP_DIR" && -d "$TEMP_DIR" ]]; then
    rm -rf -- "$TEMP_DIR"
  fi
}
trap cleanup EXIT

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$SCRIPT_DIR/jable_downloader.py" ]]; then
  SOURCE_DIR="$SCRIPT_DIR"
  if [[ -z "$REPO_URL" ]] && command -v git >/dev/null 2>&1; then
    REPO_URL="$(git -C "$SOURCE_DIR" config --get remote.origin.url 2>/dev/null || true)"
  fi
else
  if [[ -z "$REPO_URL" ]]; then
    REPO_URL="$DEFAULT_REPO_URL"
  fi
  bootstrap_git
  TEMP_DIR="$(mktemp -d)"
  git clone --depth 1 --branch "$REPO_BRANCH" "$REPO_URL" "$TEMP_DIR/source"
  SOURCE_DIR="$TEMP_DIR/source"
fi

for required in jable_downloader.py migrate_media_layout.py hls_proxy.py supjav_adblock.py rules/supjav-adblock.json config.example.json web.example.json requirements.txt bin/n update.sh uninstall.sh VERSION systemd/jable-downloader-web.service; do
  if [[ ! -f "$SOURCE_DIR/$required" ]]; then
    echo "安装源不完整，缺少：$required" >&2
    exit 1
  fi
done

echo "[1/7] 安装系统依赖..."
install_system_dependencies

echo "[2/7] 安装 N_m3u8DL-RE..."
if [[ -x "$N_M3U8DL_PATH" ]]; then
  echo "已存在，保留现有程序：$N_M3U8DL_PATH"
else
  case "$(uname -m)" in
    x86_64|amd64)
      [[ "$PLATFORM" == "alpine" ]] && ASSET_KEYS="linux-musl-x64" || ASSET_KEYS="linux-x64"
      ;;
    aarch64|arm64)
      [[ "$PLATFORM" == "alpine" ]] \
        && ASSET_KEYS="linux-musl-arm64,linux-arm64" \
        || ASSET_KEYS="linux-arm64"
      ;;
    *) echo "暂不支持的 CPU 架构：$(uname -m)" >&2; exit 1 ;;
  esac
  TEMP_DIR="${TEMP_DIR:-$(mktemp -d)}"
  RELEASE_JSON="$TEMP_DIR/n-m3u8dl-release.json"
  curl -fL --retry 3 \
    -H "Accept: application/vnd.github+json" \
    -o "$RELEASE_JSON" \
    https://api.github.com/repos/nilaoda/N_m3u8DL-RE/releases/latest
  ASSET_URL="$(python3 - "$RELEASE_JSON" "$ASSET_KEYS" <<'PY'
import json, sys
release = json.load(open(sys.argv[1], encoding="utf-8"))
keys = sys.argv[2].split(",")
for key in keys:
    urls = [
        asset["browser_download_url"]
        for asset in release.get("assets", [])
        if key in asset.get("name", "") and asset.get("name", "").endswith(".tar.gz")
    ]
    if urls:
        print(urls[0])
        break
else:
    raise SystemExit("找不到 " + "/".join(keys) + " 的发布文件")
PY
)"
  ARCHIVE="$TEMP_DIR/n-m3u8dl.tar.gz"
  EXTRACT_DIR="$TEMP_DIR/n-m3u8dl"
  mkdir -p "$EXTRACT_DIR"
  curl -fL --retry 3 -o "$ARCHIVE" "$ASSET_URL"
  tar -xzf "$ARCHIVE" -C "$EXTRACT_DIR"
  BINARY="$(find "$EXTRACT_DIR" -type f -name N_m3u8DL-RE -print -quit)"
  [[ -n "$BINARY" ]] || { echo "发布包内没有 N_m3u8DL-RE" >&2; exit 1; }
  install -m 0755 "$BINARY" "$N_M3U8DL_PATH"
  INSTALL_N_M3U8DL=true
fi

echo "[3/7] 安装应用与 Python 环境..."
install -d -m 0755 "$APP_DIR" "$CONFIG_DIR" "$STATE_DIR"
install -m 0755 "$SOURCE_DIR/jable_downloader.py" "$APP_DIR/jable_downloader.py"
install -m 0755 "$SOURCE_DIR/migrate_media_layout.py" "$APP_DIR/migrate_media_layout.py"
install -m 0644 "$SOURCE_DIR/hls_proxy.py" "$APP_DIR/hls_proxy.py"
install -m 0644 "$SOURCE_DIR/supjav_adblock.py" "$APP_DIR/supjav_adblock.py"
install -d -m 0755 "$APP_DIR/rules"
install -m 0644 "$SOURCE_DIR/rules/supjav-adblock.json" "$APP_DIR/rules/supjav-adblock.json"
install -m 0644 "$SOURCE_DIR/requirements.txt" "$APP_DIR/requirements.txt"
install -m 0644 "$SOURCE_DIR/config.example.json" "$APP_DIR/config.example.json"
install -m 0644 "$SOURCE_DIR/web.example.json" "$APP_DIR/web.example.json"
install -m 0644 "$SOURCE_DIR/VERSION" "$APP_DIR/VERSION"
install -m 0755 "$SOURCE_DIR/update.sh" "$APP_DIR/update.sh"
install -m 0755 "$SOURCE_DIR/uninstall.sh" "$APP_DIR/uninstall.sh"
if [[ -d /run/systemd/system && -f "$WEB_SERVICE" ]]; then
  systemctl stop jable-downloader-web.service >/dev/null 2>&1 || true
fi
rm -rf -- "$APP_DIR/jable_web"
cp -a "$SOURCE_DIR/jable_web" "$APP_DIR/jable_web"
find "$APP_DIR/jable_web" -type d -exec chmod 0755 {} +
find "$APP_DIR/jable_web" -type f -exec chmod 0644 {} +
create_python_environment

echo "[4/7] 创建或迁移配置..."
CONFIG_FILE="$CONFIG_DIR/config.json"
LEGACY_CONFIG="$APP_DIR/config.json"
if [[ ! -f "$CONFIG_FILE" && -f "$LEGACY_CONFIG" ]]; then
  cp -p "$LEGACY_CONFIG" "$CONFIG_FILE"
  echo "已迁移旧配置：$LEGACY_CONFIG"
fi
if [[ ! -f "$CONFIG_FILE" ]]; then
  python3 - "$SOURCE_DIR/config.example.json" "$CONFIG_FILE" "$DATA_ROOT" "$STATE_DIR" "$N_M3U8DL_PATH" "$CHROMIUM_PATH" <<'PY'
import json, os, sys
source, target, root, state, downloader, chromium = sys.argv[1:]
config = json.load(open(source, encoding="utf-8"))
config.update({
    "work_dir": os.path.join(root, "work"),
    "download_dir": os.path.join(root, "downloads"),
    "media_dir": os.path.join(root, "media"),
    "jav_media_dir": os.path.join(root, "media", "JAV"),
    "fc2_media_dir": os.path.join(root, "media", "FC2"),
    "browser_profile": os.path.join(state, "chromium-profile"),
    "n_m3u8dl_re": downloader,
    "chromium": chromium,
})
with open(target, "w", encoding="utf-8") as handle:
    json.dump(config, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
PY
else
  # Add work_dir to installations created by the verified legacy version.
  python3 - "$CONFIG_FILE" "$DATA_ROOT" <<'PY'
import json, os, sys
path, root = sys.argv[1:]
config = json.load(open(path, encoding="utf-8"))
data_parent = os.path.dirname(config.get("download_dir", "")) or root
config.setdefault("work_dir", os.path.join(data_parent, "work"))
config.setdefault("jable_site", config.get("site", "https://jable.tv"))
config.setdefault("jable_m3u8_preferred_domains", [config.get("m3u8_preferred_domain", "mushroomtrack.com")])
config.setdefault("missav_site", "https://missav.ai")
config.setdefault("missav_language", "en")
config.setdefault("missav_m3u8_preferred_domains", ["surrit.com"])
config.setdefault("missav_allow_m3u8_fallback", True)
config.setdefault("missav_hls_relay", True)
config.setdefault("supjav_site", "https://supjav.com")
config.setdefault("supjav_language", "")
config.setdefault("supjav_m3u8_preferred_domains", [])
config.setdefault("supjav_allow_m3u8_fallback", True)
config.setdefault("supjav_hls_relay", True)
config.setdefault("supjav_min_duration_seconds", 600)
config.setdefault("supjav_proxy_url", "")
config.setdefault("supjav_proxy_download", False)
config.setdefault("supjav_adblock_enabled", True)
config.setdefault("supjav_play_attempts", 10)
config.setdefault("provider_probe_workers", 3)
config.setdefault("stream_probe_timeout_seconds", 12)
config.setdefault("javbus_fallback_enabled", True)
config.setdefault("javbus_site", "https://www.javbus.com")
config.setdefault("javbus_timeout_seconds", 15)
media_root = config.get("media_dir", os.path.join(root, "media"))
config.setdefault("jav_media_dir", os.path.join(media_root, "JAV"))
config.setdefault("fc2_media_dir", os.path.join(media_root, "FC2"))
with open(path, "w", encoding="utf-8") as handle:
    json.dump(config, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
PY
fi
chmod 0600 "$CONFIG_FILE"

mapfile -t DATA_DIRS < <(python3 - "$CONFIG_FILE" <<'PY'
import json, sys
config = json.load(open(sys.argv[1], encoding="utf-8"))
for key in ("work_dir", "download_dir", "media_dir", "jav_media_dir", "fc2_media_dir", "browser_profile"):
    print(config[key])
PY
)
for directory in "${DATA_DIRS[@]}"; do
  install -d -m 0755 "$directory"
done

echo "[5/7] 创建全局命令 n..."
if [[ -e "$COMMAND_PATH" ]] && ! grep -q "Managed by jable-downloader" "$COMMAND_PATH" 2>/dev/null; then
  BACKUP="${COMMAND_PATH}.pre-jable.$(date +%Y%m%d%H%M%S)"
  cp -p "$COMMAND_PATH" "$BACKUP"
  COMMAND_BACKUP="$BACKUP"
  echo "已有 n 命令已备份到：$BACKUP"
fi
install -m 0755 "$SOURCE_DIR/bin/n" "$COMMAND_PATH"

cat > "$CONFIG_DIR/source.env" <<EOF
JABLE_REPO_URL=$(printf '%q' "$REPO_URL")
JABLE_REPO_BRANCH=$(printf '%q' "$REPO_BRANCH")
JABLE_DATA_ROOT=$(printf '%q' "$DATA_ROOT")
JABLE_N_M3U8DL_MANAGED=$INSTALL_N_M3U8DL
JABLE_COMMAND_BACKUP=$(printf '%q' "$COMMAND_BACKUP")
JABLE_LEGACY_REPO_BACKUP=$(printf '%q' "$LEGACY_REPO_BACKUP")
EOF
chmod 0600 "$CONFIG_DIR/source.env"

echo "[6/7] 配置 Web 服务..."
WEB_CONFIG="$CONFIG_DIR/web.json"
WEB_PASSWORD_DISPLAY=""
if [[ "$WEB_ENABLED" == true && ! -d /run/systemd/system ]]; then
  echo "⚠️ 当前环境未运行 systemd，Web 服务已跳过；CLI 命令 n 仍可使用。"
  WEB_ENABLED=false
fi
if [[ "$WEB_ENABLED" == false && -d /run/systemd/system && -f "$WEB_SERVICE" ]]; then
  systemctl disable jable-downloader-web.service >/dev/null 2>&1 || true
  rm -f -- "$WEB_SERVICE"
  systemctl daemon-reload
fi
if [[ "$WEB_ENABLED" == true ]]; then
  systemctl stop jable-downloader-web.service >/dev/null 2>&1 || true
  WEB_ARGS=(--output "$WEB_CONFIG")
  [[ "$WEB_HOST_EXPLICIT" == true ]] && WEB_ARGS+=(--host "$WEB_HOST")
  [[ "$WEB_PORT_EXPLICIT" == true ]] && WEB_ARGS+=(--port "$WEB_PORT")
  [[ "$WEB_USER_EXPLICIT" == true ]] && WEB_ARGS+=(--username "$WEB_USER")
  [[ "$WEB_PASSWORD_EXPLICIT" == true ]] && WEB_ARGS+=(--password "$WEB_PASSWORD")
  WEB_OUTPUT="$(
    PYTHONPATH="$APP_DIR" "$APP_DIR/venv/bin/python" -m jable_web.setup_config "${WEB_ARGS[@]}"
  )"
  mapfile -t WEB_RESULT <<< "$WEB_OUTPUT"
  for line in "${WEB_RESULT[@]}"; do
    case "$line" in
      JABLE_WEB_HOST=*) WEB_HOST="${line#*=}" ;;
      JABLE_WEB_PORT=*) WEB_PORT="${line#*=}" ;;
      JABLE_WEB_USER=*) WEB_USER="${line#*=}" ;;
      JABLE_WEB_PASSWORD=*) WEB_PASSWORD_DISPLAY="${line#*=}" ;;
    esac
  done
  chmod 0600 "$WEB_CONFIG"
  install -m 0644 "$SOURCE_DIR/systemd/jable-downloader-web.service" "$WEB_SERVICE"
  systemctl daemon-reload
  systemctl enable --now jable-downloader-web.service
fi

echo "[7/7] 验证安装..."
"$APP_DIR/venv/bin/python" -m py_compile "$APP_DIR/jable_downloader.py" "$APP_DIR/migrate_media_layout.py" "$APP_DIR/hls_proxy.py" "$APP_DIR/supjav_adblock.py" "$APP_DIR"/jable_web/*.py
test -x "$CHROMIUM_PATH"
command -v ffprobe >/dev/null
command -v xvfb-run >/dev/null
"$APP_DIR/venv/bin/python" -c "from bs4 import BeautifulSoup; from playwright.sync_api import sync_playwright"
test -x "$N_M3U8DL_PATH"
if [[ "$WEB_ENABLED" == true ]]; then
  systemctl is-active --quiet jable-downloader-web.service
fi

echo
echo "✅ 安装完成"
echo "配置文件：$CONFIG_FILE"
echo "数据根目录：$DATA_ROOT"
echo "媒体目录：${DATA_DIRS[2]}"
echo "现在可运行：n IPX-850、n 300MIUM-1483、n FC2-PPV-1234567，或直接粘贴 Jable/MissAV 详情页链接"
if [[ "$WEB_ENABLED" == true ]]; then
  ACCESS_HOST="$WEB_HOST"
  [[ "$ACCESS_HOST" == "0.0.0.0" ]] && ACCESS_HOST="服务器IP"
  echo
  echo "🌐 Web 地址：http://${ACCESS_HOST}:${WEB_PORT}"
  echo "👤 用户名：$WEB_USER"
  if [[ "$WEB_PASSWORD_DISPLAY" == "__PRESERVED__" ]]; then
    echo "🔑 密码：保持原密码（系统不保存明文）"
  else
    echo "🔑 首次密码：$WEB_PASSWORD_DISPLAY"
    echo "请立即妥善保存；该明文不会写入配置文件。"
  fi
  echo "防火墙未被修改；如无法访问，请手动放行 TCP ${WEB_PORT}。"
  echo "当前为 HTTP。公网使用建议在前方配置 HTTPS 反向代理。"
fi
