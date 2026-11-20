"""Instagram profile scans: parser, dashboard /api/profile-scans and Hub /v2/profile-scans (browser faked)."""
import json
import time

import pytest

from app.api import apikeys
from app.sources import playwright_scraper as ps
from app.services import scans
from test_app import client, csrf, db_path, new_client, signup  # noqa: F401
from app.services.urls import InvalidReelURL

REEL = 'https://www.instagram.com/reel/DTOz5t5jLQH/'
FAKE_RESULT = {
    'platform': 'instagram', 'source_url': REEL, 'username': 'spacematchchicago',
    'profile_url': 'https://www.instagram.com/spacematchchicago/reels/',
    'target': {'shortcode': 'DTOz5t5jLQH', 'found': True,
               'reel': {'shortcode': 'DTOz5t5jLQH', 'url': REEL, 'play_count': 30631, 'like_count': 408,
                        'comment_count': 9, 'taken_at': '2026-01-10T15:00:00Z'}},
    'scrolls': 3, 'reel_count': 2, 'collected_at': '2026-11-18T15:00:00Z',
    'reels': [{'shortcode': 'Other1', 'url': 'https://www.instagram.com/reel/Other1/', 'play_count': 77,
               'like_count': 1, 'comment_count': 0, 'taken_at': None},
              {'shortcode': 'DTOz5t5jLQH', 'url': REEL, 'play_count': 30631, 'like_count': 408,
               'comment_count': 9, 'taken_at': '2026-01-10T15:00:00Z'}],
}


class FakePW:
    def __enter__(self):
        return object()

    def __exit__(self, *a):
        return False


@pytest.fixture()
def fake_browser(monkeypatch):
    calls = []

    def fake_scan(url, job=None, pw=None, max_scrolls=10):
        calls.append((url, max_scrolls))
        if job:
            job.say('Scroll 1/3: 2 reels collected')
        if 'private' in url:
            raise ps.ScrapeError("Could not find the reel owner's username (reel may be private or removed)\nmore")
        return dict(FAKE_RESULT, source_url=url)
    monkeypatch.setattr(ps, 'scan_instagram_profile', fake_scan)
    monkeypatch.setattr('playwright.sync_api.sync_playwright', lambda: FakePW())
    apikeys.rate_window.windows.clear()
    return calls


def wait_done(get):
    for _ in range(400):
        body = get()
        scan = body.get('scan', body)
        if scan['status'] != 'running':
            return body
        time.sleep(0.05)
    raise AssertionError('scan did not finish')


def test_parse_instagram_target():
    assert ps.parse_instagram_target(REEL) == (None, 'DTOz5t5jLQH')
    assert ps.parse_instagram_target('https://www.instagram.com/spacematchchicago/reel/DTOz5t5jLQH/') == \
        ('spacematchchicago', 'DTOz5t5jLQH')
    assert ps.parse_instagram_target('https://www.instagram.com/spacematchchicago/reels/') == ('spacematchchicago', None)
    for bad in ('https://www.tiktok.com/@a/video/1', 'https://www.instagram.com/p/ABC123/', 'http://instagram.com/x/'):
        with pytest.raises(InvalidReelURL):
            ps.parse_instagram_target(bad)


def test_profile_reels_parser_keeps_grid_order_and_post_time():
    grid = json.dumps({'data': {'edges': [
        {'node': {'media': {'code': 'A1', 'play_count': 77, 'like_count': 1, 'comment_count': 0, 'taken_at': 1767200000,
                            'user': {'username': 'acct'}}}},
        {'node': {'media': {'code': 'B2', 'play_count': 5, 'like_count': 2}}},
    ]}})
    idx = ps.ig_profile_reels(['for (;;);' + grid])
    assert list(idx) == ['A1', 'B2']
    assert idx['A1']['taken_at'] == 1767200000 and idx['A1']['username'] == 'acct'
    assert ps._utc_iso(1767200000).endswith('Z')


def test_dashboard_scan_flow(client, fake_browser):
    c = client
    r = c.post('/api/profile-scans', headers=csrf(c), json={'url': REEL, 'max_reels': 30})
    assert r.status_code == 200, r.text
    scan_id, job_id = r.json()['scan_id'], r.json()['job_id']
    scan = wait_done(lambda: c.get(f'/api/profile-scans/{scan_id}').json())
    assert scan['status'] == 'done' and scan['username'] == 'spacematchchicago' and scan['reel_count'] == 2
    assert scan['data']['scan_id'] == scan_id and scan['data']['target']['found'] is True
    assert fake_browser == [(REEL, 3)]  # 30 reels -> 3 scrolls in the browser fallback
    assert any('reels collected' in line['msg'] for line in c.get(f'/api/scrape-jobs/{job_id}').json()['log'])
    raw = c.get(f'/api/profile-scans/{scan_id}/json')
    assert raw.status_code == 200 and raw.headers['content-type'].startswith('application/json')
    assert 'inline; filename="instagram-spacematchchicago-' in raw.headers['content-disposition']
    assert json.loads(raw.text)['reels'][1]['play_count'] == 30631


def test_dashboard_scan_validation_and_access(client, fake_browser):
    c = client
    assert c.post('/api/profile-scans', json={'url': REEL}).status_code == 403  # CSRF
    bad = c.post('/api/profile-scans', headers=csrf(c), json={'url': 'https://www.tiktok.com/@a/video/123'})
    assert bad.status_code == 422 and 'Instagram' in bad.json()['detail']
    assert c.post('/api/profile-scans', headers=csrf(c), json={'url': REEL, 'max_reels': 500}).status_code == 422
    scan_id = c.post('/api/profile-scans', headers=csrf(c), json={'url': REEL}).json()['scan_id']
    wait_done(lambda: c.get(f'/api/profile-scans/{scan_id}').json())
    with new_client() as other:
        signup(other, email='rival@example.com')
        assert other.get(f'/api/profile-scans/{scan_id}').status_code == 404
        assert other.get(f'/api/profile-scans/{scan_id}/json').status_code == 404
    assert new_client().get(f'/api/profile-scans/{scan_id}').status_code == 401


def test_failed_scan_reports_first_line(client, fake_browser):
    c = client
    scan_id = c.post('/api/profile-scans', headers=csrf(c),
                     json={'url': 'https://www.instagram.com/reel/private123/'}).json()['scan_id']
    scan = wait_done(lambda: c.get(f'/api/profile-scans/{scan_id}').json())
    assert scan['status'] == 'failed' and scan['data'] is None
    assert scan['error'] == "Could not find the reel owner's username (reel may be private or removed)"
    assert c.get(f'/api/profile-scans/{scan_id}/json').status_code == 409


@pytest.mark.skip(reason="/v2 profile scan is disabled for now (router commented out in main.py)")
def test_v2_profile_scan(client, fake_browser):
    c = client
    key = c.post('/api/api-keys', headers=csrf(c), json={'name': 'Hub'}).json()
    h = {'Authorization': f"Bearer {key['secret']}"}
    r = c.post('/v2/profile-scans', headers=h, json={'url': REEL, 'max_reels': 5})
    assert r.status_code == 202
    body = r.json()
    assert body['scan']['status'] == 'running' and 'data' not in body['scan']
    sid = body['scan']['id']
    assert body['links'] == {'self': f'/v2/profile-scans/{sid}', 'json': f'/v2/profile-scans/{sid}/json'}
    done = wait_done(lambda: c.get(f'/v2/profile-scans/{sid}', headers=h).json())
    assert done['scan']['status'] == 'done' and done['scan']['data']['reels'][0]['shortcode'] == 'Other1'
    raw = c.get(f'/v2/profile-scans/{sid}/json', headers=h)
    assert raw.status_code == 200 and json.loads(raw.text)['username'] == 'spacematchchicago'
    # errors use the Hub envelope; keys are scoped to their agent
    bad = c.post('/v2/profile-scans', headers=h, json={'url': 'https://example.com/x/123'})
    assert bad.status_code == 422 and bad.json()['error'] == {
        'code': 'invalid_link', 'message': bad.json()['error']['message'], 'field': 'url'}
    assert c.get('/v2/profile-scans/scan_nope', headers=h).json()['error']['code'] == 'not_found'
    assert c.get(f'/v2/profile-scans/{sid}').json()['error']['code'] == 'unauthorized'
    with new_client() as other:
        signup(other, email='rival2@example.com')
        okey = other.post('/api/api-keys', headers=csrf(other), json={'name': 'Other hub'}).json()
        assert other.get(f'/v2/profile-scans/{sid}', headers={'Authorization': f"Bearer {okey['secret']}"}).status_code == 404


def test_stale_running_scan_is_reported_failed(db_path):
    row = scans.create_scan('agent-1', REEL, 5, 'x')
    with scans.db() as c:
        c.execute("UPDATE profile_scans SET created_at='2000-01-01T00:00:00+00:00' WHERE id=?", (row['id'],))
    obj = scans.scan_object(scans.get_scan('agent-1', row['id']))
    assert obj['status'] == 'failed' and 'interrupted' in obj['error']
