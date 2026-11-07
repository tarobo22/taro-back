import hashlib
import json
import os
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch


class PublishContractTests(unittest.TestCase):
    def test_manifest_describes_the_actual_uploaded_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / 'web.tar.gz'
            archive.write_bytes(b'a verified build')
            output = Path(tmp) / 'outputs'
            env = dict(GITHUB_REPOSITORY='tarobo22/taro-raect', GITHUB_SHA='a' * 40,
                       GITHUB_RUN_ID='123', GITHUB_RUN_ATTEMPT='1',
                       GITHUB_REF='refs/heads/main', GITHUB_OUTPUT=str(output))
            with patch.dict(os.environ, env), patch('sys.argv', ['publish_release.py', 'web', str(archive)]), \
                    patch('subprocess.run'):
                runpy.run_path(str(Path(__file__).with_name('publish_release.py')), run_name='__main__')
            item = json.loads((Path(tmp) / 'deploy-manifest.json').read_text())
            self.assertEqual(item['size'], archive.stat().st_size)
            self.assertEqual(item['sha256'], hashlib.sha256(archive.read_bytes()).hexdigest())
            self.assertEqual(output.read_text().strip(), 'tag=' + item['tag'])


if __name__ == '__main__':
    unittest.main()
