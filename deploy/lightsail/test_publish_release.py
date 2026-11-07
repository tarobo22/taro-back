import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest
from unittest.mock import patch


class PublishContractTests(unittest.TestCase):
    def test_superseded_build_never_becomes_a_public_release(self):
        for revisions, create_error in [(['b' * 40], False),
                                        (['a' * 40, 'b' * 40], False),
                                        (['a' * 40, 'b' * 40], True)]:
            with self.subTest(revisions=revisions, create_error=create_error), tempfile.TemporaryDirectory() as tmp:
                archive = Path(tmp) / 'web.tar.gz'
                archive.write_bytes(b'a verified build')
                output = Path(tmp) / 'outputs'
                env = dict(GITHUB_REPOSITORY='tarobo22/taro-raect', GITHUB_SHA='a' * 40,
                           GITHUB_RUN_ID='123', GITHUB_RUN_ATTEMPT='1',
                           GITHUB_REF='refs/heads/main', GITHUB_OUTPUT=str(output),
                           GITHUB_STEP_SUMMARY=str(Path(tmp) / 'summary'))
                error = subprocess.CalledProcessError(1, 'gh release create') if create_error else None
                with patch.dict(os.environ, env), patch('sys.argv', ['publish_release.py', 'web', str(archive)]), \
                        patch('subprocess.run', side_effect=error) as command, \
                        patch('subprocess.check_output', side_effect=revisions), self.assertRaises(SystemExit) as stopped:
                    runpy.run_path(str(Path(__file__).with_name('publish_release.py')), run_name='__main__')
                self.assertEqual(stopped.exception.code, 0)
                self.assertFalse(output.exists())
                self.assertFalse(any('edit' in call.args[0] for call in command.call_args_list))
                self.assertEqual(command.call_count, 0 if len(revisions) == 1 else 1)

    def test_release_failure_on_current_main_stays_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / 'web.tar.gz'
            archive.write_bytes(b'a verified build')
            env = dict(GITHUB_REPOSITORY='tarobo22/taro-raect', GITHUB_SHA='a' * 40,
                       GITHUB_RUN_ID='123', GITHUB_RUN_ATTEMPT='1', GITHUB_REF='refs/heads/main')
            with patch.dict(os.environ, env), patch('sys.argv', ['publish_release.py', 'web', str(archive)]), \
                    patch('subprocess.run', side_effect=subprocess.CalledProcessError(1, 'gh')) as command, \
                    patch('subprocess.check_output', return_value='a' * 40), self.assertRaises(subprocess.CalledProcessError):
                runpy.run_path(str(Path(__file__).with_name('publish_release.py')), run_name='__main__')
            self.assertEqual(command.call_count, 1)

    def test_manifest_describes_the_actual_uploaded_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / 'web.tar.gz'
            archive.write_bytes(b'a verified build')
            output = Path(tmp) / 'outputs'
            env = dict(GITHUB_REPOSITORY='tarobo22/taro-raect', GITHUB_SHA='a' * 40,
                       GITHUB_RUN_ID='123', GITHUB_RUN_ATTEMPT='1',
                       GITHUB_REF='refs/heads/main', GITHUB_OUTPUT=str(output))
            with patch.dict(os.environ, env), patch('sys.argv', ['publish_release.py', 'web', str(archive)]), \
                    patch('subprocess.run'), patch('subprocess.check_output', return_value='a' * 40):
                runpy.run_path(str(Path(__file__).with_name('publish_release.py')), run_name='__main__')
            item = json.loads((Path(tmp) / 'deploy-manifest.json').read_text())
            self.assertEqual(item['size'], archive.stat().st_size)
            self.assertEqual(item['sha256'], hashlib.sha256(archive.read_bytes()).hexdigest())
            self.assertEqual(output.read_text().strip(), 'tag=' + item['tag'])


if __name__ == '__main__':
    unittest.main()
