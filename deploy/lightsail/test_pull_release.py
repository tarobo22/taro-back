import hashlib
import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('pull_release', Path(__file__).with_name('pull_release.py'))
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class ReleaseSafetyTests(unittest.TestCase):
    def manifest(self):
        return dict(schema=1, component='web', repository='tarobo22/taro-raect',
                    sha='a' * 40, tag='web-' + 'a' * 40 + '-123-1',
                    asset='web.tar.gz', sha256='b' * 64, size=123)

    def test_another_repository_cannot_supply_the_live_site(self):
        item = self.manifest()
        item['repository'] = 'someone/another-site'
        with self.assertRaises(ValueError):
            deploy.validate_manifest(item, 'web')

    def test_release_tag_must_identify_the_exact_source_commit(self):
        item = self.manifest()
        item['sha'] = 'c' * 40
        with self.assertRaises(ValueError):
            deploy.validate_manifest(item, 'web')

    def test_expected_release_is_accepted(self):
        item = self.manifest()
        self.assertEqual(deploy.validate_manifest(item, 'web'), item)

    def test_corrupted_download_is_rejected_before_activation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'archive'
            path.write_bytes(b'changed')
            with self.assertRaises(ValueError):
                deploy.verify_archive(path, hashlib.sha256(b'original').hexdigest(), 8)

    def make_archive(self, path, name, kind=tarfile.REGTYPE):
        with tarfile.open(path, 'w:gz') as archive:
            entry = tarfile.TarInfo(name)
            entry.type = kind
            if kind == tarfile.SYMTYPE:
                entry.linkname = '/etc/yeoun/api.env'
                archive.addfile(entry)
            else:
                entry.size = 5
                archive.addfile(entry, io.BytesIO(b'hello'))

    def test_archive_cannot_escape_its_release_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / 'web.tar.gz'
            self.make_archive(archive, '../outside')
            with self.assertRaises(ValueError):
                deploy.extract_web_archive(archive, Path(tmp) / 'release')
            self.assertFalse((Path(tmp) / 'outside').exists())

    def test_archive_cannot_expose_server_secrets_through_a_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / 'web.tar.gz'
            self.make_archive(archive, 'index.html', tarfile.SYMTYPE)
            with self.assertRaises(ValueError):
                deploy.extract_web_archive(archive, Path(tmp) / 'release')

    def test_web_release_requires_its_entry_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / 'web.tar.gz'
            self.make_archive(archive, 'other.html')
            with self.assertRaises(ValueError):
                deploy.extract_web_archive(archive, Path(tmp) / 'release')

    def test_valid_site_is_extracted_without_tar_ownership(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / 'web.tar.gz'
            self.make_archive(archive, './index.html')
            output = Path(tmp) / 'release'
            deploy.extract_web_archive(archive, output)
            self.assertEqual((output / 'index.html').read_text(), 'hello')


if __name__ == '__main__':
    unittest.main()
