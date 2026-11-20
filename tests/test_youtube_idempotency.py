"""YouTube Data API (HTTP mocked) and Idempotency-Key body conflicts."""
import httpx
import pytest

from app.sources import playwright_scraper as ps
from app.services import refresh as rf
from app.sources import youtube_api
from test_api_v1 import hub, make_key, scraper  # noqa: F401
from test_app import client, csrf, db_path, property_id  # noqa: F401

SHORT = 'https://www.youtube.com/shorts/JX1mw5HWeTM'


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code, self._payload = status, payload

    def json(self):
        return self._payload


class FakePW:
    def __enter__(self):
        return object()

    def __exit__(self, *a):
        return False


@pytest.fixture()
def yt(monkeypatch):
    state = {'calls': [], 'response': FakeResponse(200, {'items': [{'id': 'JX1mw5HWeTM', 'statistics': {
        'viewCount': '16', 'likeCount': '0', 'commentCount': '0'}}]})}

    def fake_get(url, params=None, headers=None, timeout=None):
        state['calls'].append((url, params, headers))
        if isinstance(state['response'], Exception):
            raise state['response']
        return state['response']
    monkeypatch.setattr(youtube_api, 'YOUTUBE_API_KEY', 'test-key')
    monkeypatch.setattr(youtube_api.httpx, 'get', fake_get)
    monkeypatch.setattr('playwright.sync_api.sync_playwright', lambda: FakePW())
    monkeypatch.setattr(rf, 'READ_DELAY_S', 0)
    return state


def test_youtube_via_data_api(yt):
    assert ps.scrape(SHORT) == {'views': 16, 'likes': 0, 'comments': 0, 'shares': None,
                                'detail': 'YouTube Data API v3 (video JX1mw5HWeTM)'}
    url, params, headers = yt['calls'][0]
    assert params == {'part': 'statistics', 'id': 'JX1mw5HWeTM'}
    assert headers == {'X-goog-api-key': 'test-key'} and 'key' not in params  # key never in the URL
    assert ps.scrape('https://youtu.be/JX1mw5HWeTM')['views'] == 16  # same video id from any link form


def test_hidden_likes_and_disabled_comments_are_null(yt):
    yt['response'] = FakeResponse(200, {'items': [{'statistics': {'viewCount': '560144'}}]})
    r = ps.scrape(SHORT)
    assert (r['views'], r['likes'], r['comments']) == (560144, None, None)


def test_missing_video_is_a_scrape_error(yt):
    yt['response'] = FakeResponse(200, {'items': []})
    with pytest.raises(ps.ScrapeError, match='private or removed'):
        ps.scrape(SHORT)


@pytest.mark.parametrize('failure', [FakeResponse(403, {'error': {'errors': [{'reason': 'quotaExceeded'}]}}),
                                     httpx.ConnectTimeout('slow')])
def test_api_failure_falls_back_to_browser(yt, monkeypatch, failure):
    yt['response'] = failure
    monkeypatch.setattr(ps, 'scrape_youtube', lambda s, vid: {'views': 1, 'likes': 1, 'comments': None,
                                                               'shares': None, 'detail': 'browser'})

    class FakeSession:
        def __init__(self, *a):
            pass

        def close(self):
            pass
    monkeypatch.setattr(ps, 'Session', FakeSession)
    job = ps.new_job([], False)
    assert ps.scrape(SHORT, job, pw=object())['detail'] == 'browser'
    assert any('falling back to the browser' in line['msg'] for line in job.log)


def test_refresh_saves_youtube_api_numbers(client, yt):
    pid = property_id(client, links=[SHORT])
    prop = rf.get_one('SELECT * FROM properties WHERE id=?', (pid,))
    rf.create_refresh(prop, 'manual')
    rf.run_queue()
    reel = rf.property_analytics(prop)['reels'][0]
    assert (reel['views'], reel['likes'], reel['comments'], reel['source']) == (16, 0, 0, 'youtube_api')


def test_same_idempotency_key_with_a_different_body_is_409(hub):
    c, h = hub
    hh = {**h, 'Idempotency-Key': 'same-key'}
    first = c.post('/v1/properties', headers=hh, json={'address': '1 First St', 'links': [SHORT]})
    assert first.status_code == 201
    replay = c.post('/v1/properties', headers=hh, json={'links': [SHORT], 'address': '1 First St'})  # same body, other order
    assert replay.status_code == 201 and replay.json()['id'] == first.json()['id']
    clash = c.post('/v1/properties', headers=hh, json={'address': '2 Other St', 'links': [SHORT]})
    assert clash.status_code == 409 and clash.json()['error']['code'] == 'conflict'
    assert len(c.get('/v1/properties', headers=h).json()['items']) == 1


def test_refresh_idempotency_body_conflict(hub):
    c, h = hub
    pid = c.post('/v1/properties', headers=h, json={'address': '3 Refresh St', 'links': [SHORT]}).json()['id']
    rf.run_queue()
    with rf.db() as db:
        db.execute("UPDATE refreshes SET created_at='2000-01-01T00:00:00+00:00'")
    hh = {**h, 'Idempotency-Key': 'refresh-key'}
    assert c.post(f'/v1/properties/{pid}/refresh', headers=hh, json={'requested_by': 'a@x.co'}).status_code == 202
    assert c.post(f'/v1/properties/{pid}/refresh', headers=hh, json={'requested_by': 'a@x.co'}).status_code == 202
    assert c.post(f'/v1/properties/{pid}/refresh', headers=hh, json={'requested_by': 'b@x.co'}).status_code == 409
