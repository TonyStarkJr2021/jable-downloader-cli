#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="/opt/jable-downloader"
CONFIG_DIR="/etc/jable-downloader"
COMMAND_PATH="/usr/local/bin/n"
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
  # shellcheck disable=SC1090
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

for required in jable_downloader.py config.example.json requirements.txt bin/n update.sh uninstall.sh VERSION; do
  [[ -f "$SOURCE_DIR/$required" ]] || { echo "更新源缺少：$required" >&2; exit 1; }
done
[[ -d "$APP_DIR/venv" ]] || { echo "尚未安装，请先运行 install.sh。" >&2; exit 1; }

BACKUP_DIR="$APP_DIR/backups/$(date +%Y%m%d%H%M%S)"
install -d -m 0700 "$BACKUP_DIR"
for file in jable_downloader.py requirements.txt VERSION; do
  [[ -f "$APP_DIR/$file" ]] && cp -p "$APP_DIR/$file" "$BACKUP_DIR/$file"
done
if [[ -f "$COMMAND_PATH" ]] && grep -q "Managed by jable-downloader" "$COMMAND_PATH"; then
  cp -p "$COMMAND_PATH" "$BACKUP_DIR/n"
fi

install -m 0755 "$SOURCE_DIR/jable_downloader.py" "$APP_DIR/jable_downloader.py"
install -m 0644 "$SOURCE_DIR/requirements.txt" "$APP_DIR/requirements.txt"
install -m 0644 "$SOURCE_DIR/config.example.json" "$APP_DIR/config.example.json"
install -m 0644 "$SOURCE_DIR/VERSION" "$APP_DIR/VERSION"
install -m 0755 "$SOURCE_DIR/update.sh" "$APP_DIR/update.sh"
install -m 0755 "$SOURCE_DIR/uninstall.sh" "$APP_DIR/uninstall.sh"
if [[ ! -e "$COMMAND_PATH" ]] || grep -q "Managed by jable-downloader" "$COMMAND_PATH" 2>/dev/null; then
  install -m 0755 "$SOURCE_DIR/bin/n" "$COMMAND_PATH"
else
  echo "⚠️ 当前 n 命令不由本项目管理，未覆盖：$COMMAND_PATH"
fi
"$APP_DIR/venv/bin/python" -m pip install --disable-pip-version-check -r "$APP_DIR/requirements.txt"
"$APP_DIR/venv/bin/python" -m py_compile "$APP_DIR/jable_downloader.py"

echo "✅ 更新完成：v$(cat "$APP_DIR/VERSION")"
echo "配置与 Chromium profile 均未改动。回滚备份：$BACKUP_DIR"
