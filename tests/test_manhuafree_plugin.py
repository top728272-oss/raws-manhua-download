import pytest

from app.config import AppConfig
from app.core.decoder import decode_images
from app.core.fetcher import HttpFetcher
from app.plugins.manhuafree import ManhuaFreePlugin

TEST_PAYLOAD = "J7r092CrJCNqKf40QaHpAeBbI7h0MrH0BfJqBTkmHX9mgWLaxa5oxXgbO6Kr5jMazrB0LWgmB_w0kD92NqKf4rCCJ0QaHpI7h0MeAbBrH0BfkmHX9qJTBmgWLaxXgbOaxo56Kr5jB0MWgaMrzmB_wleW4skM2Cr4fQ0JCNqKaHpAeM0HrBbI7h0BfJq9XgmBTkmHWLaxaObK65oxXgr5jMagWBmzrB0K_wnQ"


def test_decoder_payload():
    urls = decode_images(TEST_PAYLOAD)
    assert len(urls) == 3
    first_item = urls[0]["url"] if isinstance(urls[0], dict) else urls[0]
    assert first_item.endswith("0.webp")


@pytest.mark.asyncio
async def test_manhuafree_plugin_registration(tmp_path):
    cfg = AppConfig(output_dir=str(tmp_path))
    fetcher = HttpFetcher(cfg)
    plugin = ManhuaFreePlugin(cfg, fetcher)

    assert plugin.id == "manhuafree"
    assert plugin.name == "ManhuaFree (CloudMe)"
    assert plugin.base_url == "https://manhuafree.com"
    await fetcher.aclose()


@pytest.mark.asyncio
async def test_get_pages_is_zero_based_and_keeps_payload_order():
    """Page indices must start at 0 and follow the payload's own order.

    The index used to be `order + 1`, so the first page was index 1 and the
    writer saved it as 002.webp — every filename shifted by one.
    """
    from unittest.mock import AsyncMock

    from app.core.models import Chapter

    fetcher = AsyncMock()
    fetcher.get_json.return_value = {"data": {"info": {"images": {"images": TEST_PAYLOAD}}}}
    plugin = ManhuaFreePlugin(AppConfig(), fetcher)

    chapter = Chapter(
        source_id="manhuafree",
        manga_ref="12345",
        title="第1话",
        ref="chapter1",
        extra={"mid": "12345", "chapter_id": "chapter1"},
    )
    pages = await plugin.get_pages(chapter)

    assert [p.index for p in pages] == [0, 1, 2]
    # The CDN numbers its own files from 0, which is what the index must match.
    assert pages[0].image_url.endswith("/0.webp")
    assert pages[2].image_url.endswith("/2.webp")
