from __future__ import annotations

import ast
import csv
import json
from pathlib import Path

import pytest

from video_scout.domain.models import MediaVariant, ScanConfig, ScoutError, VideoItem
from video_scout.domain.urls import in_scope, normalize_url, redact, safe_text, validate_url
from video_scout.services.export import export_items


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'ftp://site/a', 'blob:https://site/x',
                                  'https://user:pass@site/', 'https://site/\x1b[0m', 'https://site:bad/'])
def test_reject_non_http_and_embedded_credentials(url):
    with pytest.raises(ScoutError):
        validate_url(url)


def test_conservative_url_scope_and_signed_queries():
    url = 'https://Example.COM/a?page=2&sig=a%2Bb&x=1&x=2#/route?q=3'
    assert normalize_url(url) == url.replace('Example.COM', 'example.com')
    config = ScanConfig(url, allowed_hosts=('cdn.example.com',), allowed_paths=('/a', '/videos'))
    assert in_scope('https://example.com/a?page=3', config)
    assert in_scope('https://cdn.example.com/videos/1', config)
    assert not in_scope('https://example.com/another', config)
    assert not in_scope('https://untrusted.example/videos', config)


def test_untrusted_text_and_credentials_never_reach_logs():
    message = '\x1b[31m[bold]hi[/bold]\x1b[0m\u202e https://x/a?Signature=secret#token'
    assert '\x1b' not in safe_text(message)
    assert '\u202e' not in safe_text(message)
    assert 'secret' not in redact(message)
    assert 'secret' not in redact('Authorization: Bearer secret\nCookie=secret')
    assert '[bold]' in safe_text(message)  # presentation must use Text, not markup


@pytest.mark.parametrize('format', ['txt', 'csv', 'json'])
async def test_exports_media_and_preserve_signatures_but_exclude_context(tmp_path, format):
    url = 'https://cdn.example/video.mp4?sig=keep&expires=123'
    item = VideoItem.create(title='中文, 标题', source_url='https://example/page',
                            page_url='https://example/watch',
                            variants=[MediaVariant(url, ext='mp4', headers={
                                'cdn.example': {'Authorization': 'secret', 'Cookie': 'secret'}})])
    path = await export_items([item], tmp_path / f'中文 导出.{format}', format)
    raw = path.read_text()
    assert url in raw
    assert 'Authorization' not in raw and 'secret' not in raw and 'Cookie' not in raw
    if format == 'txt':
        assert raw == url + '\n'
        assert item.source_url not in raw
    elif format == 'json':
        data = json.loads(raw)
        assert data[0]['source_url'] == item.source_url
        assert data[0]['variants'][0]['url'] == url
    else:
        with path.open() as stream:
            row = next(csv.DictReader(stream))
        assert row['media_url'] == url
        assert row['source_page'] == item.source_url
        assert row['duration'] == '未知'
    with pytest.raises(ScoutError):
        await export_items([item], path, format)


def test_architecture_dependencies_and_no_shell_execution():
    root = Path(__file__).parents[1] / 'src/video_scout'
    forbidden = {'httpx', 'bs4', 'yt_dlp', 'sqlite3', 'textual', 'playwright'}
    for layer in ['domain', 'services']:
        for path in (root / layer).glob('*.py'):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    imports = [node.module or '']
                else:
                    continue
                assert not any(name.split('.')[0] in forbidden or name.startswith('video_scout.adapters')
                               or name.startswith('video_scout.tui') for name in imports), path
    for path in root.rglob('*.py'):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call):
                assert not any(keyword.arg == 'shell' and isinstance(keyword.value, ast.Constant)
                               and keyword.value.value is True for keyword in node.keywords), path
