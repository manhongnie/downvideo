from __future__ import annotations

import base64
import json
from urllib.parse import quote

import pytest

from video_scout.adapters.http import HtmlDiscovery
from video_scout.domain.models import FetchedPage, ScanConfig

PAGE_URL = "https://site.test/index.php/vod/play/id/42/sid/1/nid/1.html"
MEDIA_URL = "https://cdn.test/videos/clip.m3u8?signature=a%2Fb&quality=hd"


def player_page(value: str, encryption: int) -> FetchedPage:
    player = json.dumps({"encrypt": encryption, "url": value, "from": "dplayer"})
    body = f"<html><title>Clip</title><script>var player_aaaa={player}</script></html>"
    return FetchedPage(PAGE_URL, body.encode())


@pytest.mark.parametrize("encryption,value", [
    (0, MEDIA_URL),
    (1, quote(MEDIA_URL, safe="")),
    (2, base64.b64encode(quote(MEDIA_URL, safe="").encode()).decode()),
])
def test_maccms_player_url_becomes_media_candidate_with_play_page_referer(encryption, value):
    candidates = HtmlDiscovery().extract(player_page(value, encryption), ScanConfig(PAGE_URL))
    assert len(candidates) == 1
    assert candidates[0].kind == "media"
    assert candidates[0].url == MEDIA_URL
    assert candidates[0].source_url == PAGE_URL
    assert candidates[0].title == "Clip"


@pytest.mark.parametrize("encryption,value", [
    (0, "javascript:alert(1)"),
    (0, "//cdn.test/clip.m3u8"),
    (2, "not base64!"),
    (3, MEDIA_URL),
    (0, "https://user:pass@cdn.test/clip.m3u8"),
])
def test_maccms_rejects_invalid_or_unsupported_urls(encryption, value):
    candidates = HtmlDiscovery().extract(player_page(value, encryption), ScanConfig(PAGE_URL))
    assert all(candidate.kind != "media" for candidate in candidates)


def test_maccms_malformed_player_json_does_not_break_other_media_discovery():
    body = b'''<script>var player_aaaa={bad json}</script>
        <video src="https://cdn.test/good.mp4"></video>'''
    candidates = HtmlDiscovery().extract(FetchedPage(PAGE_URL, body), ScanConfig(PAGE_URL))
    assert [candidate.url for candidate in candidates] == ["https://cdn.test/good.mp4"]
