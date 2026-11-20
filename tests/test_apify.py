"""Instagram through Apify (HTTP mocked - no real credit is spent)."""
import time

import pytest

from app.sources import apify_source
from app.sources import playwright_scraper as ps
from app.services import refresh as rf
from test_app import client, csrf, db_path, property_id  # noqa: F401

REEL = 'https://www.instagram.com/reel/DTOz5t5jLQH/'


def item(code, plays=100, likes=5, comments=1, owner='spacematchchicago', ts='2026-01-08T01:35:36.000Z'):
    return {'shortCode': code, 'url': f'https://www.instagram.com/p/{code}/', 'ownerUsername': owner,
            'videoPlayCount': plays, 'videoViewCount': None, 'likesCount': likes, 'commentsCount': comments,
            'timestamp': ts, 'type': 'Video'}


class FakePW:
    def __enter__(self):
        return object()

    def __exit__(self, *a):
        return False


@pytest.fixture()
def apify(monkeypatch):
    """Apify on, with a scripted run_reel_scraper. Set state['fail'] to an exception to raise it."""
    state = {'calls': [], 'fail': None}

    def fake_run(targets, limit):
        state['calls'].append((targets, limit))
        if state['fail']:
            raise state['fail']
        t = targets[0]
        if '/reel/' in t:
            code = t.rstrip('/').split('/')[-1]
            return [] if code == 'Gone12345' else [item(code, plays=30667, likes=407, comments=27)]
        return [item('Old1', 10, ts='2026-01-01T00:00:00Z'), item('New2', 20, ts='2026-11-10T00:00:00Z'),
                item('DTOz5t5jLQH', 30667, 407, 27), item('Collab3', 729, owner='someone_else', ts='2026-11-14T00:00:00Z'),
                {'error': 'not_found', 'errorDescription': 'skip me'}]
    monkeypatch.setattr(apify_source, 'APIFY_TOKEN', 'test-token')
    monkeypatch.setattr(apify_source, 'run_reel_scraper', fake_run)
    monkeypatch.setattr('playwright.sync_api.sync_playwright', lambda: FakePW())
    monkeypatch.setattr(rf, 'READ_DELAY_S', 0)
    return state


def test_reel_read_via_apify(apify):
    result = ps.scrape(REEL)
    assert result == {'views': 30667, 'likes': 407, 'comments': 27, 'shares': None,
                      'detail': 'Instagram via Apify Reel Scraper (@spacematchchicago)'}
    assert apify['calls'] == [(['https://www.instagram.com/reel/DTOz5t5jLQH/'], 1)]


def test_hidden_counts_become_null(apify, monkeypatch):
    monkeypatch.setattr(apify_source, 'run_reel_scraper', lambda t, n: [item('DTOz5t5jLQH', plays=None, likes=-1)])
    r = ps.scrape(REEL)
    assert r['views'] is None and r['likes'] is None and r['comments'] == 1


def test_missing_reel_is_a_scrape_error_not_a_fallback(apify, monkeypatch):
    browser = []
    monkeypatch.setattr(ps, 'Session', lambda *a, **k: browser.append(1))
    with pytest.raises(ps.ScrapeError, match='private or removed'):
        ps.scrape('https://www.instagram.com/reel/Gone12345/')
    assert browser == []  # no browser run


def test_apify_failure_falls_back_to_browser(apify, monkeypatch):
    apify['fail'] = apify_source.ApifyError('Apify account is out of credit')
    monkeypatch.setattr(ps, 'scrape_instagram', lambda s, url, code: {'views': 1, 'likes': 2, 'comments': 3,
                                                                       'shares': None, 'detail': 'browser'})

    class FakeSession:
        def __init__(self, *a):
            pass

        def close(self):
            pass
    monkeypatch.setattr(ps, 'Session', FakeSession)
    job = ps.new_job([], False)
    assert ps.scrape(REEL, job, pw=object())['detail'] == 'browser'
    assert any('falling back to the browser' in line['msg'] for line in job.log)


def test_refresh_saves_apify_numbers_with_source(client, apify):
    pid = property_id(client, links=[REEL])
    rf.create_refresh(rf.get_one('SELECT * FROM properties WHERE id=?', (pid,)), 'manual')
    rf.run_queue()
    reel = rf.property_analytics(rf.get_one('SELECT * FROM properties WHERE id=?', (pid,)))['reels'][0]
    assert (reel['views'], reel['likes'], reel['comments'], reel['source']) == (30667, 407, 27, 'apify')


def test_profile_scan_via_apify(apify):
    data = apify_source.instagram_profile(REEL, max_reels=30)
    assert data['provider'] == 'apify' and data['username'] == 'spacematchchicago' and data['scrolls'] is None
    assert [r['shortcode'] for r in data['reels']] == ['Collab3', 'New2', 'DTOz5t5jLQH', 'Old1']  # newest first
    assert data['reels'][0]['owner_username'] == 'someone_else'  # collabs on the profile are kept
    assert data['target'] == {'shortcode': 'DTOz5t5jLQH', 'found': True, 'source': 'profile',
                              'reel': next(r for r in data['reels'] if r['shortcode'] == 'DTOz5t5jLQH')}
    assert apify['calls'] == [(['https://www.instagram.com/reel/DTOz5t5jLQH/'], 1), (['spacematchchicago'], 30)]


def test_profile_link_skips_reel_lookup(apify):
    data = apify_source.instagram_profile('https://www.instagram.com/spacematchchicago/', max_reels=5)
    assert data['target'] is None and apify['calls'] == [(['spacematchchicago'], 5)]


def test_dashboard_scan_uses_apify(client, apify):
    c = client
    r = c.post('/api/profile-scans', headers=csrf(c), json={'url': REEL, 'max_reels': 30}).json()
    for _ in range(200):
        scan = c.get(f"/api/profile-scans/{r['scan_id']}").json()
        if scan['status'] != 'running':
            break
        time.sleep(0.05)
    assert scan['status'] == 'done' and scan['max_reels'] == 30 and scan['data']['provider'] == 'apify'
    assert scan['data']['reel_count'] == 4
