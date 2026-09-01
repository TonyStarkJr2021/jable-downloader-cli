import unittest
from pathlib import Path


INSTALLER = (Path(__file__).parents[1] / "install.sh").read_text(encoding="utf-8")
UNINSTALLER = (Path(__file__).parents[1] / "uninstall.sh").read_text(encoding="utf-8")
README = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")


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


if __name__ == "__main__":
    unittest.main()

