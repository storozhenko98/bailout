#!/usr/bin/env python3
"""Validate share metadata and its actual PNG assets; --live also checks crawlers."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path
import struct
import subprocess
import tempfile
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = 'https://bailout.dev'


class Head(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.meta, self.links = {}, []
        self.feed(html.split('</head>', 1)[0])

    def handle_starttag(self, tag, attributes):
        values = dict(attributes)
        if tag == 'meta':
            key = values.get('property', values.get('name'))
            if key:
                assert key not in self.meta, f'Duplicate metadata: {key}'
                self.meta[key] = values.get('content', '')
        if tag == 'link':
            self.links.append(values)


def png_size(data):
    assert data[:8] == b'\x89PNG\r\n\x1a\n' and data[12:16] == b'IHDR', 'Not a PNG'
    return struct.unpack('>II', data[16:24])


def local_check():
    assets = set()
    for page in ['/', '/docs/', '/demo/']:
        raw = (ROOT / 'site' / page.lstrip('/') / 'index.html').read_text()
        assert len(raw.encode()) < 1_000_000
        head = Head(raw)
        for key in ['og:title', 'og:description', 'og:site_name', 'og:image:alt',
                    'twitter:title', 'twitter:description', 'twitter:image:alt']:
            assert head.meta[key].strip(), f'{page}: missing {key}'
        assert head.meta['twitter:card'] == 'summary_large_image'
        assert head.meta['og:type'] == 'website'
        assert head.meta['og:url'] == ORIGIN + page
        assert next(x['href'] for x in head.links if x.get('rel') == 'canonical') == ORIGIN + page
        assert 'noindex' not in head.meta.get('robots', '')
        for key, size in [('og:image', (1200, 630)), ('twitter:image', (1200, 600))]:
            url = urlparse(head.meta[key])
            assert url.scheme == 'https' and url.netloc == 'bailout.dev' and not url.query
            path = ROOT / 'site' / url.path.lstrip('/')
            data = path.read_bytes()
            assert png_size(data) == size and len(data) < 400_000
            assets.add(url.path)
        assert (int(head.meta['og:image:width']), int(head.meta['og:image:height'])) == (1200, 630)
        assert head.meta['og:image:type'] == 'image/png'
        icon = next(x for x in head.links if x.get('rel') == 'apple-touch-icon')
        assert png_size((ROOT / 'site' / icon['href'].lstrip('/')).read_bytes()) == (180, 180)
        assets.add(icon['href'])
        assert any(x.get('type') == 'image/png' and x.get('sizes') == '512x512' for x in head.links)
    assert 'Disallow: /\n' not in (ROOT / 'site/robots.txt').read_text()
    for page in ['/', '/docs/', '/demo/']:
        assert ORIGIN + page in (ROOT / 'site/sitemap.xml').read_text()
    print('PASS: homepage, docs and demo have static OG/X cards, canonical URLs, crawlable PNG assets and Apple icons')
    return sorted(assets | {'/icon-512.png', '/favicon-32.png'})


def fetch(url, agent):
    with tempfile.TemporaryDirectory() as directory:
        headers, body = Path(directory) / 'headers', Path(directory) / 'body'
        result = subprocess.run(['curl', '-q', '-fsSL', '--max-time', '25', '-A', agent,
                                 '-D', str(headers), '-o', str(body), '-w', '%{http_code}', url],
                                capture_output=True, text=True, check=True)
        assert result.stdout == '200', f'{url}: {result.stdout}'
        return headers.read_text().lower(), body.read_bytes()


def live_check(assets):
    agents = ['Twitterbot/1.0', 'facebookexternalhit/1.1', 'LinkedInBot/1.0',
              'Slackbot-LinkExpanding 1.0', 'Discordbot/2.0', 'WhatsApp/2.0', 'Applebot/0.1']

    def page_check(item):
        page, agent = item
        headers, data = fetch(ORIGIN + page, agent)
        assert 'content-type: text/html' in headers
        assert 'noindex' not in headers
        head = Head(data.decode())
        expected = Head((ROOT / 'site' / page.lstrip('/') / 'index.html').read_text())
        assert head.meta == expected.meta, f'{agent}: live metadata differs on {page}'
        return f'{agent}: {page} 200, metadata matches'

    with ThreadPoolExecutor(max_workers=4) as pool:
        for result in pool.map(page_check, [(page, agent) for page in ['/', '/docs/', '/demo/'] for agent in agents]):
            print(result)
    for asset in assets:
        headers, data = fetch(ORIGIN + asset, 'Twitterbot/1.0')
        assert 'content-type: image/png' in headers
        assert data == (ROOT / 'site' / asset.lstrip('/')).read_bytes(), f'Unexpected image: {asset}'
        assert 'x-robots-tag: noindex' not in headers
        if asset.startswith('/social/'):
            assert 'immutable' in headers
        print(f'{asset}: PNG {png_size(data)}, {len(data):,} bytes')
    print('PASS: public crawler responses and all deployed share assets')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    assets = local_check()
    if args.live:
        live_check(assets)
