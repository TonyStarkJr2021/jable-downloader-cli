import unittest
from pathlib import Path


INSTALLER = (Path(__file__).parents[1] / "install.sh").read_text(encoding="utf-8")
UNINSTALLER = (Path(__file__).parents[1] / "uninstall.sh").read_text(encoding="utf-8")
README = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
UPDATER = (Path(__file__).parents[1] / "update.sh").read_text(encoding="utf-8")
SERVICE = (Path(__file__).parents[1] / "systemd" / "jable-downloader-web.service").read_text(encoding="utf-8")
REQUIREMENTS = (Path(__file__).parents[1] / "requirements.txt").read_text(encoding="utf-8")
PREVIEW = Path(__file__).parents[1] / "docs" / "preview.png"
RELEASE_WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "release.yml"
VERSION = (Path(__file__).parents[1] / "VERSION").read_text(encoding="utf-8").strip()
WEB_INIT = (Path(__file__).parents[1] / "jable_web" / "__init__.py").read_text(encoding="utf-8")
RELEASE_NOTES = Path(__file__).parents[1] / f"docs/RELEASE_NOTES_v{VERSION}.md"
ADBLOCK_RULES = Path(__file__).parents[1] / "rules" / "supjav-adblock.json"


class InstallerPlatformTests(unittest.TestCase):
    def test_supported_platform_branches_exist(self):
        for marker in (
            'debian|ubuntu)',
            'fedora)',
            'centos)',
            'rocky|almalinux|rhel|ol)',
            'arch|archarm)',
            'opensuse-tumbleweed|opensuse-leap)',
            'alpine)',
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, INSTALLER)

    def test_package_managers_exist(self):
        for command in ("apt-get install", "dnf install", "yum install", "pacman -Syu", "zypper --non-interactive", "apk add"):
            with self.subTest(command=command):
                self.assertIn(command, INSTALLER)

    def test_eol_repositories_require_explicit_flag_and_backup(self):
        self.assertIn("--enable-eol-repos", INSTALLER)
        self.assertIn("LEGACY_REPO_BACKUP", INSTALLER)
        self.assertIn("vault.centos.org/7.9.2009", INSTALLER)
        self.assertIn("vault.centos.org/8.5.2111", INSTALLER)
        self.assertIn("--restore-repos", UNINSTALLER)

    def test_musl_downloader_asset_is_selected(self):
        self.assertIn("linux-musl-x64", INSTALLER)
        self.assertIn("linux-musl-arm64", INSTALLER)

    def test_web_install_update_and_uninstall_are_integrated(self):
        self.assertIn("--web-port", INSTALLER)
        self.assertIn("jable_web.setup_config", INSTALLER)
        self.assertIn("jable-downloader-web.service", INSTALLER)
        self.assertIn("jable-downloader-web.service", UPDATER)
        self.assertIn("jable-downloader-web.service", UNINSTALLER)
        self.assertIn("防火墙未被修改", INSTALLER)
        self.assertIn("ReadWritePaths=/etc/jable-downloader", SERVICE)

    def test_multisource_files_and_config_are_installed_and_migrated(self):
        for script in (INSTALLER, UPDATER):
            self.assertIn("hls_proxy.py", script)
            self.assertIn('config.setdefault("missav_site"', script)
            self.assertIn('config.setdefault("missav_hls_relay"', script)
            self.assertIn('config.setdefault("supjav_site"', script)
            self.assertIn('config.setdefault("supjav_hls_relay"', script)
            self.assertIn('config.setdefault("supjav_proxy_url"', script)
            self.assertIn('config.setdefault("supjav_proxy_download"', script)
            self.assertIn('config.pop("supjav_ipv6_first", None)', script)
            self.assertIn('config.setdefault("supjav_adblock_enabled"', script)
            self.assertIn('config.setdefault("supjav_play_attempts"', script)
            self.assertIn("supjav_adblock.py", script)
            self.assertIn("rules/supjav-adblock.json", script)
            self.assertIn('config.setdefault("provider_probe_workers"', script)
            self.assertIn('config.setdefault("javbus_fallback_enabled"', script)
            self.assertIn('config.setdefault("javbus_site"', script)
            self.assertIn('config.setdefault("javbus_timeout_seconds"', script)
        self.assertIn("curl-cffi", REQUIREMENTS)
        self.assertIn("FC2-PPV-1234567", README)
        self.assertTrue(ADBLOCK_RULES.is_file())
        self.assertIn('"schema_version": 1', ADBLOCK_RULES.read_text(encoding="utf-8"))

    def test_one_command_auto_selects_raid_or_system_disk(self):
        self.assertIn('RAID_DATA_ROOT="/mnt/raid_hdd/AV"', INSTALLER)
        self.assertIn('SYSTEM_DATA_ROOT="/var/lib/jable-downloader-data"', INSTALLER)
        self.assertIn("is_mountpoint()", INSTALLER)
        self.assertIn("select_data_root()", INSTALLER)
        self.assertIn("自动使用 VPS 系统盘", INSTALLER)
        self.assertIn("DATA_ROOT_EXPLICIT=true", INSTALLER)

    def test_classified_media_directories_are_upgrade_safe(self):
        for script in (INSTALLER, UPDATER):
            self.assertIn('config.setdefault("jav_media_dir"', script)
            self.assertIn('config.setdefault("fc2_media_dir"', script)
            self.assertIn("migrate_media_layout.py", script)
        self.assertIn("现有媒体未移动", UPDATER)
        self.assertIn("JAV/FC2 分类目录", UNINSTALLER)

    def test_public_repository_urls_are_release_ready(self):
        expected = "TonyStarkJr2021/jable-downloader-cli"
        self.assertIn(expected, README)
        self.assertIn(expected, INSTALLER)
        self.assertNotIn("OWNER", README)
        self.assertNotIn("OWNER", INSTALLER)
        self.assertIn("bash <(curl -fsSL", README)
        self.assertNotIn("--repo https://github.com/TonyStarkJr2021", README)
        self.assertIn("/main/update.sh)", README)
        self.assertIn("/main/uninstall.sh)", README)
        self.assertNotIn("从本地成品发布到 GitHub", README)
        self.assertNotIn("gh release create", README)

    def test_update_never_prompts_for_public_git_credentials(self):
        for script in (INSTALLER, UPDATER):
            self.assertIn("GIT_TERMINAL_PROMPT=0 git clone", script)
            self.assertIn("archive/refs/heads/$REPO_BRANCH.tar.gz", script)
            self.assertIn("Git 克隆失败，改用 GitHub 官方源码压缩包", script)

    def test_repository_presentation_and_release_assets_are_present(self):
        self.assertIn("docs/preview.png", README)
        self.assertIn("actions/workflows/ci.yml/badge.svg", README)
        self.assertTrue(PREVIEW.is_file())
        self.assertTrue(PREVIEW.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("git archive --format=zip", workflow)
        self.assertIn("gh release upload", workflow)
        self.assertIn("supjav_adblock.py", workflow)

    def test_release_version_is_consistent(self):
        self.assertEqual(VERSION, "2.7.7")
        self.assertIn(f'__version__ = "{VERSION}"', WEB_INIT)
        self.assertIn(f"## v{VERSION}", README)
        self.assertTrue(RELEASE_NOTES.is_file())


if __name__ == "__main__":
    unittest.main()
