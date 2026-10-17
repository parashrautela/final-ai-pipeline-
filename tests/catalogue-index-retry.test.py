import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
import numpy as np
from PIL import Image
from app.services import catalogue_search as search
from app.services.catalogue_search import CatalogueIndex, InvalidPhoto, cache_key


class Encoder:
    """Stands in for CLIP: photos that are b'unusable' cannot be isolated."""
    def embed(self, photo):
        if photo == b'unusable':
            raise InvalidPhoto('Couldn’t isolate the jewellery.')
        vector = np.ones(512, dtype=np.float32)
        return vector / np.linalg.norm(vector)


class FailedImagesAreNotRetriedEveryCycle(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        subject = patch('app.services.catalogue_search.subject.isolate', side_effect=lambda photo: (photo, {'target': 'test'}))
        subject.start(); self.addCleanup(subject.stop)
        data = io.BytesIO(); Image.new('RGB', (40, 40), 'white').save(data, 'JPEG')
        self.good = data.getvalue()
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.rows = [
            dict(id='ok', is_published=True, image_url='https://catalogue.test/ok.jpg'),
            dict(id='bad', is_published=True, image_url='https://catalogue.test/bad.jpg'),
        ]
        self.index = CatalogueIndex(Encoder(), lambda: self.rows, {'catalogue.test'}, Path(folder.name) / 'index.json')
        self.downloads = []
        self.outage = False

        async def download(client, url):
            self.downloads.append(url)
            if self.outage:
                raise httpx.ConnectError('network down')
            return b'unusable' if url.endswith('bad.jpg') or url.endswith('bad2.jpg') else self.good
        self.index.download = download

    def count(self, name):
        return sum(1 for url in self.downloads if url.endswith(name))

    async def test_unusable_photo_is_downloaded_once_until_its_url_changes(self):
        for _ in range(5):
            await self.index.refresh()
        self.assertEqual(self.count('bad.jpg'), 1)
        self.assertEqual(self.count('ok.jpg'), 1)
        self.assertEqual(self.index.failed, {cache_key(self.rows[1])})
        self.assertEqual(len(self.index.vectors), 1)

        self.rows[1] = dict(id='bad', is_published=True, image_url='https://catalogue.test/bad2.jpg')
        await self.index.refresh()
        await self.index.refresh()
        self.assertEqual(self.count('bad2.jpg'), 1)
        self.assertEqual(self.count('bad.jpg'), 1)
        self.assertEqual(self.index.failed, {cache_key(self.rows[1])})
        self.assertNotIn(cache_key(dict(id='bad', image_url='https://catalogue.test/bad.jpg')), self.index.retry_at)

    async def test_transient_failure_waits_for_the_backoff_then_recovers(self):
        self.outage = True
        clock = [1000.0]
        # Replace only the module's clock; asyncio keeps using the real one.
        with patch.object(search, 'time', SimpleNamespace(monotonic=lambda: clock[0])):
            await self.index.refresh()
            first = len(self.downloads)
            self.assertEqual(first, 2)
            self.assertEqual(len(self.index.failed), 2)

            clock[0] += search.FAILED_RETRY_SECONDS - 1
            await self.index.refresh()
            self.assertEqual(len(self.downloads), first)

            self.outage = False
            clock[0] += 2
            await self.index.refresh()
        self.assertEqual(self.count('ok.jpg'), 2)
        self.assertEqual(self.count('bad.jpg'), 2)
        self.assertEqual(self.index.failed, {cache_key(self.rows[1])})
        self.assertIn(cache_key(self.rows[0]), self.index.vectors)
        self.assertNotIn(cache_key(self.rows[0]), self.index.retry_at)


if __name__ == '__main__':
    unittest.main()
