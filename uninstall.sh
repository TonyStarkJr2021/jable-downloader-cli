#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="/opt/jable-downloader"
CONFIG_DIR="/etc/jable-downloader"
STATE_DIR="/var/lib/jable-downloader"
COMMAND_PATH="/usr/local/bin/n"
N_M3U8DL_PATH="/usr/local/bin/N_m3u8DL-RE"
WEB_SERVICE="/etc/systemd/system/jable-downloader-web.service"
PURGE=false
RESTORE_REPOS=false

while (($#)); do
  case "$1" in
    --purge) PURGE=true ;;
    --restore-repos) RESTORE_REPOS=true ;;
    *) echo "用法：sudo ./uninstall.sh [--purge] [--restore-repos]" >&2; exit 2 ;;
  esac
  shift
done

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "请用 sudo 或 root 运行 uninstall.sh。" >&2
  exit 1
fi

N_M3U8DL_MANAGED=false
COMMAND_BACKUP=""
LEGACY_REPO_BACKUP=""
if [[ -r "$CONFIG_DIR/source.env" ]]; then
  # shellcheck disable=SC1090,SC1091
  source "$CONFIG_DIR/source.env"
  N_M3U8DL_MANAGED="${JABLE_N_M3U8DL_MANAGED:-false}"
  COMMAND_BACKUP="${JABLE_COMMAND_BACKUP:-}"
  LEGACY_REPO_BACKUP="${JABLE_LEGACY_REPO_BACKUP:-}"
fi

if [[ -d /run/systemd/system ]]; then
  systemctl disable --now jable-downloader-web.service >/dev/null 2>&1 || true
fi
rm -f -- "$WEB_SERVICE"
if [[ -d /run/systemd/system ]]; then
  systemctl daemon-reload
fi

if [[ -f "$COMMAND_PATH" ]] && grep -q "Managed by jable-downloader" "$COMMAND_PATH"; then
  rm -f -- "$COMMAND_PATH"
  if [[ -n "$COMMAND_BACKUP" && -f "$COMMAND_BACKUP" ]]; then
    mv -- "$COMMAND_BACKUP" "$COMMAND_PATH"
    echo "已恢复安装前的 n 命令：$COMMAND_PATH"
  fi
fi
rm -rf -- "$APP_DIR"

if [[ "$N_M3U8DL_MANAGED" == true && -f "$N_M3U8DL_PATH" ]]; then
  rm -f -- "$N_M3U8DL_PATH"
fi

if [[ "$RESTORE_REPOS" == true ]]; then
  for disabled in /etc/yum.repos.d/*.jable-eol-disabled; do
    [[ -e "$disabled" ]] || continue
    mv -f -- "$disabled" "${disabled%.jable-eol-disabled}"
  done
  rm -f -- \
    /etc/yum.repos.d/jable-centos-vault.repo \
    /etc/yum.repos.d/jable-epel-archive.repo
  echo "已恢复安装前停用的 CentOS/EPEL repo 文件。"
fi

if [[ "$PURGE" == true ]]; then
  rm -rf -- "$CONFIG_DIR" "$STATE_DIR"
  echo "✅ 已卸载并清除配置与 Chromium profile。"
else
  echo "✅ 已卸载程序。"
  echo "已保留配置：$CONFIG_DIR"
  echo "已保留 Chromium profile：$STATE_DIR"
fi

echo "媒体、downloads 和 work 目录从未删除。"
echo "qBittorrent、MoviePilot、Docker、挂载点和防火墙均未改动。"
if [[ -n "$LEGACY_REPO_BACKUP" && -d "$LEGACY_REPO_BACKUP" ]]; then
  echo "CentOS EOL repo 备份仍保留在：$LEGACY_REPO_BACKUP"
fi
