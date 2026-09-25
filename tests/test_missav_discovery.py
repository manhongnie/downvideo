from __future__ import annotations

from video_scout.adapters.http import HtmlDiscovery
from video_scout.domain.models import FetchedPage, PageTask, ScanConfig

CATEGORY = "https://missav.ws/dm278/chinese-subtitle"
DETAIL = "https://missav.ws/dldss-533-chinese-subtitle"
MANIFEST = "https://cdn.test/seg1-seg2-seg3-seg4-token/playlist.m3u8"
PACKED_PLAYER = r"""eval(function(p,a,c,k,e,d){return p}('e=\'8://7.6/5-4-3-2-1/d.0\';',15,15,'m3u8|token|seg4|seg3|seg2|seg1|test|cdn|https|video|720p|source1280|source842|playlist|source'.split('|'),0,{}))"""


def test_missav_category_prioritizes_card_details_and_ignores_preview_and_ad_media():
    body = b"""<title>Chinese subtitle listing</title>
        <a href='/noise/1'>noise</a><a href='/noise/2'>noise</a>
        <a href='/dldss-533-chinese-subtitle'>
          <video class='preview hidden' src='https://fourhoi.com/preview.mp4'
                 data-src='https://fourhoi.com/dldss-533/preview.mp4'></video>
        </a>"""
    page = FetchedPage(CATEGORY, body, media_urls=(
        "https://cdn.test/ad.mp4", "https://fourhoi.com/dldss-533/preview.mp4"))
    discovery = HtmlDiscovery()
    config = ScanConfig(CATEGORY, max_queue=1)

    assert discovery.extract(page, config) == []
    links = discovery.discover(page, PageTask(CATEGORY), config)
    assert links[0].url == DETAIL
    assert links[0].kind == "detail"
    assert links[0].depth == 1


def test_missav_detail_extracts_only_main_packed_manifest():
    body = f"""<title>Generic MissAV site title</title><h1>Target film</h1>
        <a href='/other'><video class='preview hidden' src='https://fourhoi.com/other/preview.mp4'></video></a>
        <video class='player' src='blob:https://missav.ws/player'></video>
        <script>{PACKED_PLAYER}</script>""".encode()
    page = FetchedPage(DETAIL, body, media_urls=("https://cdn.test/ad.mp4",))
    candidates = HtmlDiscovery().extract(page, ScanConfig(DETAIL))

    assert len(candidates) == 1
    assert candidates[0].url == MANIFEST
    assert candidates[0].source_url == DETAIL
    assert candidates[0].title == "Target film"
    assert candidates[0].kind == "media"


def test_missav_packed_hint_requires_a_main_player():
    page = FetchedPage(CATEGORY, f"<video class='preview hidden'></video><script>{PACKED_PLAYER}</script>".encode())
    assert HtmlDiscovery().extract(page, ScanConfig(CATEGORY)) == []
