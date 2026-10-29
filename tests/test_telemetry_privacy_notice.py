"""The collector's privacy notice (deploy/telemetry/privacy.html): one self-contained page with no scripts and no
external resources, whose inline stylesheet the Caddyfile's Content-Security-Policy allows by hash."""
import base64
import hashlib
import re
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / 'deploy/telemetry/privacy.html'
CADDYFILE = ROOT / 'deploy/telemetry/Caddyfile'


class Elements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.attributes = [], []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attributes += [(tag, name, value or '') for name, value in attrs]


def parsed():
    elements = Elements()
    elements.feed(PAGE.read_text())
    return elements


def test_no_scripts_and_no_external_resources():
    text = PAGE.read_text()
    elements = parsed()
    assert not {'script', 'link', 'img', 'iframe', 'object', 'embed', 'base', 'form', 'svg', 'video', 'audio'} & set(
        elements.tags)
    for tag, name, value in elements.attributes:
        assert not name.startswith('on'), (tag, name)  # no inline event handlers
        assert name not in ('src', 'srcset', 'action', 'style', 'formaction', 'background'), (tag, name)
        if name == 'href':
            assert value.startswith('mailto:') or value.startswith('#'), (tag, value)  # nothing to fetch
    stylesheet = re.search(r'<style>(.*?)</style>', text, re.S).group(1)
    assert 'url(' not in stylesheet and '@import' not in stylesheet
    assert re.search(r'https?://', stylesheet) is None


def test_the_csp_allows_exactly_this_stylesheet():
    stylesheet = re.search(r'<style>(.*?)</style>', PAGE.read_text(), re.S).group(1)
    digest = base64.b64encode(hashlib.sha256(stylesheet.encode()).digest()).decode()
    policy = re.search(r'Content-Security-Policy "([^"]+)"', CADDYFILE.read_text()).group(1)
    assert f"'sha256-{digest}'" in policy, 'update the Caddyfile CSP hash after editing the stylesheet'
    assert "'unsafe-inline'" not in policy and "default-src 'none'" in policy
    assert len(re.findall(r'<style>', PAGE.read_text())) == 1  # one hashed stylesheet, nothing else inline


def test_it_says_what_the_brief_requires():
    text = PAGE.read_text()
    for phrase in ('Pre-release staging service', 'all of it\n  is deleted at launch', 'already apply now', 'Access and portability.', 'Rectification.', 'Objection and restriction.',
                   'No decision with legal or similar effect', '13 months after last use',
                   'release or release-candidate number', 'exit code', 'Also delete previously sent data when turning a choice off',
                   'Joshua Fulton, the Rynhult project maintainer', 'privacy@rynhult.dev', 'Legal basis: consent',
                   'withdrawn at any time', 'Data already\nsent', '90 days after receipt', '13 calendar months',
                   '180 days', '45 days', 'at least ten installations', 'separate\n    keys', 'every 90 days',
                   'keeps no access logs', 'Deletion.', 'supervisory authority',
                   'Frankfurt, Germany', "isn't transferred outside it", 'data-processing agreement',
                   'no\nbackups beyond that disk', 'Version 0.4',
                   'Delete data already sent', 'key ids', 'prefers-color-scheme', 'name="viewport"'):
        assert phrase in text, phrase
    assert 'TODO(public release)' in text and 'TELEMETRY.md' in text  # placeholder, no live link to a private repo
