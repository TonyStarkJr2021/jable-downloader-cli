#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="/opt/jable-downloader"
CONFIG_DIR="/etc/jable-downloader"
COMMAND_PATH="/usr/local/bin/n"
WEB_SERVICE="/etc/systemd/system/jable-downloader-web.service"
SOURCE_DIR=""
TEMP_DIR=""

usage() {
  cat <<'EOF'
用法：sudo ./update.sh [--source 本地项目目录]

不指定 --source 时：仓库内执行会使用当前仓库；已安装目录内执行会从
/etc/jable-downloader/source.env 记录的 Git 仓库获取最新版。
EOF
}

while (($#)); do
  case "$1" in
    --source) SOURCE_DIR="${2:?--source 需要目录}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知选项：$1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "请用 sudo 或 root 运行 update.sh。" >&2
  exit 1
fi

cleanup() {
  [[ -n "$TEMP_DIR" && -d "$TEMP_DIR" ]] && rm -rf -- "$TEMP_DIR"
}
trap cleanup EXIT

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -z "$SOURCE_DIR" && -d "$SCRIPT_DIR/.git" ]]; then
  SOURCE_DIR="$SCRIPT_DIR"
fi
if [[ -z "$SOURCE_DIR" ]]; then
  SOURCE_ENV="$CONFIG_DIR/source.env"
  [[ -r "$SOURCE_ENV" ]] || { echo "缺少更新源记录：$SOURCE_ENV" >&2; exit 1; }
  # shellcheck disable=SC1090,SC1091
  source "$SOURCE_ENV"
  [[ -n "${JABLE_REPO_URL:-}" ]] || {
    echo "安装时未记录远程仓库。请在项目目录运行 sudo ./update.sh。" >&2
    exit 1
  }
  TEMP_DIR="$(mktemp -d)"
  git clone --depth 1 --branch "${JABLE_REPO_BRANCH:-main}" \
    "$JABLE_REPO_URL" "$TEMP_DIR/source"
  SOURCE_DIR="$TEMP_DIR/source"
fi

for required in jable_downloader.py hls_proxy.py config.example.json web.example.json requirements.txt bin/n update.sh uninstall.sh VERSION systemd/jable-downloader-web.service; do
  [[ -f "$SOURCE_DIR/$required" ]] || { echo "更新源缺少：$required" >&2; exit 1; }
done
[[ -d "$APP_DIR/venv" ]] || { echo "尚未安装，请先运行 install.sh。" >&2; exit 1; }

BACKUP_DIR="$APP_DIR/backups/$(date +%Y%m%d%H%M%S)"
install -d -m 0700 "$BACKUP_DIR"
for file in jable_downloader.py hls_proxy.py requirements.txt VERSION; do
  [[ -f "$APP_DIR/$file" ]] && cp -p "$APP_DIR/$file" "$BACKUP_DIR/$file"
done
for file in config.json web.json source.env; do
  [[ -f "$CONFIG_DIR/$file" ]] && cp -p "$CONFIG_DIR/$file" "$BACKUP_DIR/$file"
done
[[ -d "$APP_DIR/jable_web" ]] && cp -a "$APP_DIR/jable_web" "$BACKUP_DIR/jable_web"
if [[ -f "$COMMAND_PATH" ]] && grep -q "Managed by jable-downloader" "$COMMAND_PATH"; then
  cp -p "$COMMAND_PATH" "$BACKUP_DIR/n"
fi

install -m 0755 "$SOURCE_DIR/jable_downloader.py" "$APP_DIR/jable_downloader.py"
install -m 0644 "$SOURCE_DIR/hls_proxy.py" "$APP_DIR/hls_proxy.py"
install -m 0644 "$SOURCE_DIR/requirements.txt" "$APP_DIR/requirements.txt"
install -m 0644 "$SOURCE_DIR/config.example.json" "$APP_DIR/config.example.json"
install -m 0644 "$SOURCE_DIR/web.example.json" "$APP_DIR/web.example.json"
install -m 0644 "$SOURCE_DIR/VERSION" "$APP_DIR/VERSION"
install -m 0755 "$SOURCE_DIR/update.sh" "$APP_DIR/update.sh"
install -m 0755 "$SOURCE_DIR/uninstall.sh" "$APP_DIR/uninstall.sh"
if [[ -d /run/systemd/system ]]; then
  systemctl stop jable-downloader-web.service >/dev/null 2>&1 || true
fi
rm -rf -- "$APP_DIR/jable_web"
cp -a "$SOURCE_DIR/jable_web" "$APP_DIR/jable_web"
find "$APP_DIR/jable_web" -type d -exec chmod 0755 {} +
find "$APP_DIR/jable_web" -type f -exec chmod 0644 {} +
if [[ ! -e "$COMMAND_PATH" ]] || grep -q "Managed by jable-downloader" "$COMMAND_PATH" 2>/dev/null; then
  install -m 0755 "$SOURCE_DIR/bin/n" "$COMMAND_PATH"
else
  echo "⚠️ 当前 n 命令不由本项目管理，未覆盖：$COMMAND_PATH"
fi
"$APP_DIR/venv/bin/python" -m pip install --disable-pip-version-check -r "$APP_DIR/requirements.txt"
"$APP_DIR/venv/bin/python" - "$CONFIG_DIR/config.json" <<'PY'
import json, os, sys
path = sys.argv[1]
with open(path, encoding="utf-8") as handle:
    config = json.load(handle)
config.setdefault("jable_site", config.get("site", "https://jable.tv"))
config.setdefault("jable_m3u8_preferred_domains", [config.get("m3u8_preferred_domain", "mushroomtrack.com")])
config.setdefault("missav_site", "https://missav.ai")
config.setdefault("missav_language", "en")
config.setdefault("missav_m3u8_preferred_domains", ["surrit.com"])
config.setdefault("missav_allow_m3u8_fallback", True)
config.setdefault("missav_hls_relay", True)
media_root = config.get("media_dir", "/mnt/raid_hdd/AV/media")
config.setdefault("jav_media_dir", os.path.join(media_root, "JAV"))
config.setdefault("fc2_media_dir", os.path.join(media_root, "FC2"))
temporary = path + ".tmp"
with open(temporary, "w", encoding="utf-8") as handle:
    json.dump(config, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
os.replace(temporary, path)
PY
chmod 0600 "$CONFIG_DIR/config.json"
mapfile -t MEDIA_DIRS < <("$APP_DIR/venv/bin/python" - "$CONFIG_DIR/config.json" <<'PY'
import json, sys
config = json.load(open(sys.argv[1], encoding="utf-8"))
for key in ("media_dir", "jav_media_dir", "fc2_media_dir"):
    print(config[key])
PY
)
for directory in "${MEDIA_DIRS[@]}"; do
  install -d -m 0755 "$directory"
done
"$APP_DIR/venv/bin/python" -m py_compile "$APP_DIR/jable_downloader.py" "$APP_DIR/hls_proxy.py" "$APP_DIR"/jable_web/*.py

WEB_PASSWORD_DISPLAY=""
if [[ -d /run/systemd/system ]]; then
  WEB_OUTPUT="$(PYTHONPATH="$APP_DIR" "$APP_DIR/venv/bin/python" \
    -m jable_web.setup_config --output "$CONFIG_DIR/web.json")"
  while IFS= read -r line; do
    case "$line" in
      JABLE_WEB_HOST=*) WEB_HOST="${line#*=}" ;;
      JABLE_WEB_PORT=*) WEB_PORT="${line#*=}" ;;
      JABLE_WEB_USER=*) WEB_USER="${line#*=}" ;;
      JABLE_WEB_PASSWORD=*) WEB_PASSWORD_DISPLAY="${line#*=}" ;;
    esac
  done <<< "$WEB_OUTPUT"
  install -m 0644 "$SOURCE_DIR/systemd/jable-downloader-web.service" "$WEB_SERVICE"
  systemctl daemon-reload
  systemctl enable --now jable-downloader-web.service
  systemctl is-active --quiet jable-downloader-web.service
fi

echo "✅ 更新完成：v$(cat "$APP_DIR/VERSION")"
echo "现有媒体未移动；新下载会自动归档到 JAV/FC2 分类目录。"
echo "配置、账号与 Chromium profile 已保留。回滚备份：$BACKUP_DIR"
echo "JAV 目录：${MEDIA_DIRS[1]}"
echo "FC2 目录：${MEDIA_DIRS[2]}"
if [[ -n "${WEB_PORT:-}" ]]; then
  ACCESS_HOST="${WEB_HOST:-服务器IP}"
  [[ "$ACCESS_HOST" == "0.0.0.0" ]] && ACCESS_HOST="服务器IP"
  echo "Web 地址：http://${ACCESS_HOST}:${WEB_PORT}"
  echo "Web 用户名：$WEB_USER"
  if [[ "$WEB_PASSWORD_DISPLAY" != "__PRESERVED__" ]]; then
    echo "首次 Web 密码：$WEB_PASSWORD_DISPLAY"
    echo "请立即保存；该明文不会写入配置文件。"
  fi
fi
