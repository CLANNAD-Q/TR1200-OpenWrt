import gzip
import pathlib
import tempfile
import unittest
from unittest import mock

import tr1200_flasher as flasher


class ReleaseValidationTests(unittest.TestCase):
    def test_accepts_release_version(self) -> None:
        self.assertEqual(flasher.validate_release("24.10.5"), "24.10.5")

    def test_rejects_invalid_release_versions(self) -> None:
        for release in ("", "24.10", "../24.10.5", "24.10.5/../../tmp", "v24.10.5"):
            with self.subTest(release=release), self.assertRaises(flasher.FlasherError):
                flasher.validate_release(release)

    def test_parses_only_exact_device_images(self) -> None:
        filename = flasher.image_info("24.10.5", "sysupgrade").filename
        self.assertEqual(
            flasher.parse_release_from_image_filename(filename, "sysupgrade"),
            "24.10.5",
        )
        with self.assertRaises(flasher.FlasherError):
            flasher.parse_release_from_image_filename(
                filename.replace("cudy_tr1200-v1", "cudy_tr1200-v2"),
                "sysupgrade",
            )


class ChecksumTests(unittest.TestCase):
    def test_checksum_match_requires_exact_filename(self) -> None:
        digest = "a" * 64
        manifest = f"{digest}  firmware.bin\n"
        self.assertEqual(flasher.expected_checksum(manifest, "firmware.bin"), digest)
        with self.assertRaises(flasher.FlasherError):
            flasher.expected_checksum(manifest, "path/firmware.bin")

    def test_package_metadata_uses_requested_release(self) -> None:
        digest = "b" * 64
        metadata = (
            "Package: luci-i18n-base-zh-cn\n"
            "Filename: luci-i18n-base-zh-cn_1_all.ipk\n"
            f"SHA256sum: {digest}\n\n"
        )
        with mock.patch.object(flasher, "fetch", return_value=gzip.compress(metadata.encode())) as fetch:
            info = flasher.release_package_info("24.10.5", flasher.ZH_PACKAGE)
        self.assertEqual(info.sha256, digest)
        self.assertIn("/24.10.5/", info.url)
        fetch.assert_called_once_with(
            "https://downloads.openwrt.org/releases/24.10.5/packages/mipsel_24kc/luci/Packages.gz"
        )

    def test_package_metadata_rejects_shell_metacharacters(self) -> None:
        digest = "b" * 64
        metadata = (
            "Package: luci-i18n-base-zh-cn\n"
            "Filename: luci-i18n-base-zh-cn_1;reboot_all.ipk\n"
            f"SHA256sum: {digest}\n\n"
        )
        with mock.patch.object(flasher, "fetch", return_value=gzip.compress(metadata.encode())):
            with self.assertRaises(flasher.FlasherError):
                flasher.release_package_info("24.10.5", flasher.ZH_PACKAGE)

    def test_package_metadata_accepts_realistic_openwrt_version(self) -> None:
        digest = "c" * 64
        metadata = (
            "Package: luci-i18n-base-zh-cn\n"
            "Filename: luci-i18n-base-zh-cn_git-26.228.65014~8e278ba_all.ipk\n"
            f"SHA256sum: {digest}\n\n"
        )
        with mock.patch.object(flasher, "fetch", return_value=gzip.compress(metadata.encode())):
            info = flasher.release_package_info("24.10.5", flasher.ZH_PACKAGE)
        self.assertEqual(info.sha256, digest)

    def test_hidden_fields_are_independent_of_attribute_order(self) -> None:
        html = (
            '<input name="first" value="one" type="hidden">'
            '<input type="text" name="ignored" value="two">'
            '<input value="three" name="second" type="hidden">'
        )
        self.assertEqual(flasher._hidden_fields(html), {"first": "one", "second": "three"})

    def test_download_verified_is_atomic_and_checked(self) -> None:
        info = flasher.image_info("24.10.5", "sysupgrade")
        image = b"verified image"
        digest = flasher.hashlib.sha256(image).hexdigest()
        manifest = f"{digest}  {info.filename}\n".encode()

        def fake_fetch(url: str) -> bytes:
            return manifest if url.endswith("/sha256sums") else image

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            flasher, "fetch", side_effect=fake_fetch
        ):
            path = flasher.download_verified(info, pathlib.Path(directory))
            self.assertEqual(path.read_bytes(), image)
            self.assertEqual(list(path.parent.glob("*.part")), [])


if __name__ == "__main__":
    unittest.main()
