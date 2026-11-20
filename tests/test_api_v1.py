"""Hub API v1: API keys, properties (address + links), links, refreshes, analytics, history and logs."""
from datetime import datetime, timezone

import pytest

from app.api import apikeys
from app.sources import playwright_scraper as ps
from app.services import refresh as rf
from app.db import storage
from test_app import client, csrf, db_path, new_client, property_id, signup  # noqa: F401

IG = 'https://www.instagram.com/reel/HubReel01/'
IG2 = 'https://www.instagram.com/reel/HubReel02/'
FB = 'https://www.facebook.com/reel/123456789012'
TT = 'https://www.tiktok.com/@spacematch/video/7122941464534912302'
PROPERTY_KEYS = {'id', 'address', 'active', 'created_at', 'updated_at', 'archived_at'}


class FakePW:
    def __enter__(self):
        return object()

    def __exit__(self, *a):
        return False


@pytest.fixture()
def scraper(monkeypatch):
    """Fake browser. Set scraper['fail'] = {'facebook'} to make a platform fail."""
    state = {'fail': set(), 'views': 100}

    def fake(url, job=None, pw=None):
        platform = 'facebook' if 'facebook' in url else 'tiktok' if 'tiktok' in url else 'instagram'
        if platform in state['fail']:
            raise ps.ScrapeError('Login wall - blocked for now')
        state['views'] += 1
        return {'views': state['views'], 'likes': 7, 'comments': 2, 'shares': None, 'detail': 'fake read'}
    monkeypatch.setattr(ps, 'scrape', fake)
    monkeypatch.setattr('playwright.sync_api.sync_playwright', lambda: FakePW())
    monkeypatch.setattr(rf, 'READ_DELAY_S', 0)
    apikeys.rate_window.windows.clear()
    return state


def make_key(c, scopes=None, name='Hub production'):
    body = {'name': name} | ({'scopes': scopes} if scopes else {})
    r = c.post('/api/api-keys', headers=csrf(c), json=body)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture()
def hub(client, scraper):
    """(dashboard client, Bearer headers) for an agent with a full-scope key."""
    key = make_key(client)
    return client, {'Authorization': f"Bearer {key['secret']}"}


def create(c, h, address='1620 N Hoyne Ave, Chicago, IL 60647', links=(IG,)):
    return c.post('/v1/properties', headers=h, json={'address': address, 'links': list(links)})


def new_property(c, h, **kw):
    r = create(c, h, **kw)
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------------------------ keys ----

def test_api_key_lifecycle(client):
    created = make_key(client)
    assert created['secret'].startswith('sk_live_') and created['status'] == 'active'
    listed = client.get('/api/api-keys').json()
    assert len(listed) == 1 and 'secret' not in listed[0] and listed[0]['prefix'].startswith('sk_live_')
    h = {'Authorization': f"Bearer {created['secret']}"}
    assert client.get('/v1/properties', headers=h).status_code == 200
    rotated = client.post(f"/api/api-keys/{created['id']}/rotate", headers=csrf(client), json={'grace_hours': 0}).json()
    assert client.get('/v1/properties', headers=h).status_code == 401  # old secret revoked at once
    h2 = {'Authorization': f"Bearer {rotated['secret']}"}
    assert client.get('/v1/properties', headers=h2).status_code == 200
    assert client.delete(f"/api/api-keys/{rotated['id']}", headers=csrf(client)).status_code == 200
    assert client.get('/v1/properties', headers=h2).status_code == 401


def test_rotate_with_grace_keeps_old_key_working(client):
    old = make_key(client)
    client.post(f"/api/api-keys/{old['id']}/rotate", headers=csrf(client), json={'grace_hours': 24})
    assert client.get('/v1/properties', headers={'Authorization': f"Bearer {old['secret']}"}).status_code == 200


def test_api_key_acts_as_the_agent_who_created_it(client, scraper):
    """The key decides whose property it is - not who is logged in elsewhere."""
    key_a = make_key(client)
    with new_client() as b:
        signup(b, email='agent-b@example.com')
        key_b = make_key(b)
        pa = new_property(client, {'Authorization': f"Bearer {key_a['secret']}"}, address='1 Agent A St')
        pb = new_property(b, {'Authorization': f"Bearer {key_b['secret']}"}, address='2 Agent B St')
        assert [p['id'] for p in client.get('/api/properties').json()] == [pa['id']]
        assert [p['id'] for p in b.get('/api/properties').json()] == [pb['id']]


def test_auth_errors_use_the_envelope(hub):
    c, h = hub
    r = c.get('/v1/properties')
    assert r.status_code == 401 and r.json()['error']['code'] == 'unauthorized'
    assert c.get('/v1/properties', headers={'Authorization': 'Bearer sk_live_nope'}).status_code == 401
    assert r.headers['X-Request-Id']
    ok = c.get('/v1/properties', headers={**h, 'X-Request-Id': 'hub-123'})
    assert ok.headers['X-Request-Id'] == 'hub-123' and 'X-RateLimit-Remaining' in ok.headers
    assert c.get('/v1/nope', headers=h).json()['error']['code'] == 'not_found'
    bad = create(c, h, address='x')
    assert bad.status_code == 422 and bad.json()['error']['field'] == 'address'


def test_scopes_enforced(client, scraper):
    key = make_key(client, scopes=['properties:read'])
    h = {'Authorization': f"Bearer {key['secret']}"}
    assert client.get('/v1/properties', headers=h).status_code == 200
    r = create(client, h)
    assert r.status_code == 403 and r.json()['error']['code'] == 'forbidden'


def test_dashboard_key_management_is_agent_only(client):
    assert new_client().get('/api/api-keys').status_code == 401
    assert client.post('/api/api-keys', json={'name': 'No csrf'}).status_code == 403


def test_rate_limit(hub, monkeypatch):
    c, h = hub
    monkeypatch.setattr(apikeys, 'API_RATE_LIMIT_PER_MIN', 2)
    c.get('/v1/properties', headers=h)
    c.get('/v1/properties', headers=h)
    r = c.get('/v1/properties', headers=h)
    assert r.status_code == 429 and r.headers['Retry-After'] and r.json()['error']['code'] == 'rate_limited'


# ------------------------------------------------------------ properties ----

def test_create_property_with_links(hub):
    c, h = hub
    p = new_property(c, h, links=[IG, IG2, FB])
    assert set(p) == PROPERTY_KEYS | {'links', 'refresh'}  # nothing beyond address + links
    assert p['id'].startswith('prop_') and p['active'] is True and p['created_at'].endswith('Z')
    assert [link['platform'] for link in p['links']] == ['instagram', 'instagram', 'facebook']
    assert all(link['status'] == 'pending' for link in p['links'])
    assert p['refresh']['trigger'] == 'link_change' and p['refresh']['status'] == 'queued'
    assert len(p['refresh']['links']) == 3


def test_create_property_validation(hub):
    c, h = hub
    no_links = c.post('/v1/properties', headers=h, json={'address': '1 A St'})
    assert no_links.status_code == 422 and no_links.json()['error']['field'] == 'links'
    assert create(c, h, links=[]).status_code == 422
    blank = create(c, h, links=['  '])
    assert blank.status_code == 422 and blank.json()['error']['code'] == 'validation_failed'
    bad = create(c, h, links=[IG, 'https://evil.com/reel/1'])
    assert bad.status_code == 422 and bad.json()['error'] == {
        'code': 'invalid_link', 'message': bad.json()['error']['message'], 'field': 'links[1]'}
    assert c.get('/v1/properties', headers=h).json()['items'] == []  # nothing saved
    extra = c.post('/v1/properties', headers=h, json={'address': '1 A St', 'links': [IG], 'external_id': 'x', 'price': '$1'})
    assert extra.status_code == 201 and 'external_id' not in extra.json() and 'price' not in extra.json()


def test_property_crud(hub):
    c, h = hub
    p = new_property(c, h)
    got = c.get(f"/v1/properties/{p['id']}", headers=h).json()
    assert got['address'] == p['address'] and len(got['links']) == 1
    assert c.patch(f"/v1/properties/{p['id']}", headers=h, json={'address': '9 New Rd'}).json()['address'] == '9 New Rd'
    archived = c.post(f"/v1/properties/{p['id']}/archive", headers=h).json()
    assert archived['active'] is False and archived['archived_at']
    assert c.get('/v1/properties?active=false', headers=h).json()['items'][0]['id'] == p['id']
    assert c.post(f"/v1/properties/{p['id']}/unarchive", headers=h).json()['active'] is True
    assert c.delete(f"/v1/properties/{p['id']}", headers=h).status_code == 204
    assert c.get(f"/v1/properties/{p['id']}", headers=h).status_code == 404


def test_idempotency_key_replays_create(hub):
    c, h = hub
    hh = {**h, 'Idempotency-Key': 'create-1'}
    a = create(c, hh, address='9 Same St')
    b = create(c, hh, address='9 Same St')
    assert a.status_code == b.status_code == 201 and a.json()['id'] == b.json()['id']
    assert b.headers.get('Idempotent-Replayed') == 'true'
    assert len(c.get('/v1/properties', headers=h).json()['items']) == 1


def test_paging(hub):
    c, h = hub
    for i in range(5):
        new_property(c, h, address=f'{i} Paging Rd')
    first = c.get('/v1/properties?limit=2', headers=h).json()
    assert len(first['items']) == 2 and first['next_cursor']
    seen = [x['address'] for x in first['items']]
    cursor = first['next_cursor']
    while cursor:
        nxt = c.get(f'/v1/properties?limit=2&cursor={cursor}', headers=h).json()
        seen += [x['address'] for x in nxt['items']]
        cursor = nxt['next_cursor']
    assert seen == [f'{i} Paging Rd' for i in range(5)]


def test_agents_are_isolated(hub):
    c, h = hub
    pid = new_property(c, h)['id']
    with new_client() as other:
        signup(other, email='rival@example.com')
        oh = {'Authorization': f"Bearer {make_key(other)['secret']}"}
        assert other.get(f'/v1/properties/{pid}', headers=oh).status_code == 404
        assert other.get(f'/v1/properties/{pid}/analytics', headers=oh).status_code == 404
        assert other.post(f'/v1/properties/{pid}/links', headers=oh, json={'url': IG2}).status_code == 404
        assert other.get('/v1/properties', headers=oh).json()['items'] == []


# ----------------------------------------------------------------- links ----

def test_links_add_replace_remove(hub):
    c, h = hub
    pid = new_property(c, h)['id']
    rf.run_queue()
    added = c.post(f'/v1/properties/{pid}/links', headers=h, json={'url': IG2})
    assert added.status_code == 201 and added.json()['added'] is True
    assert added.json()['refresh']['links'][0]['link_id'] == added.json()['link']['id']
    assert c.post(f'/v1/properties/{pid}/links', headers=h, json={'url': IG2}).json()['added'] is False
    links = c.get(f'/v1/properties/{pid}/links', headers=h).json()['links']
    assert [x['platform'] for x in links] == ['instagram', 'instagram']

    bad = c.put(f'/v1/properties/{pid}/links', headers=h, json={'links': [TT, 'https://evil.com/x/1']})
    assert bad.status_code == 422 and bad.json()['error']['field'] == 'links[1]'
    assert len(c.get(f'/v1/properties/{pid}/links', headers=h).json()['links']) == 2  # nothing changed
    assert c.put(f'/v1/properties/{pid}/links', headers=h, json={'links': []}).status_code == 422

    replaced = c.put(f'/v1/properties/{pid}/links', headers=h, json={'links': [IG2, TT]}).json()
    assert [x['platform'] for x in replaced['links']] == ['instagram', 'tiktok']
    assert len(replaced['added']) == 1 and len(replaced['removed']) == 1

    tt_id = next(x['id'] for x in replaced['links'] if x['platform'] == 'tiktok')
    assert c.delete(f'/v1/properties/{pid}/links/{tt_id}', headers=h).status_code == 204
    assert c.delete(f'/v1/properties/{pid}/links/{tt_id}', headers=h).status_code == 404
    last = c.get(f'/v1/properties/{pid}/links', headers=h).json()['links'][0]['id']
    blocked = c.delete(f'/v1/properties/{pid}/links/{last}', headers=h)
    assert blocked.status_code == 409 and 'at least one link' in blocked.json()['error']['message']

    history = c.get(f'/v1/properties/{pid}/links/history', headers=h).json()['items']
    assert sum(1 for x in history if x['current']) == 1 and len(history) == 3


def test_validate_link(hub):
    c, h = hub
    v = c.post('/v1/links/validate', headers=h, json={'url': 'https://vt.tiktok.com/ZSpaGf8Yp/'}).json()
    assert v['valid'] and v['platform'] == 'tiktok'
    assert c.post('/v1/links/validate', headers=h, json={'url': 'https://evil.com/x/12345'}).status_code == 422


def test_dashboard_keeps_several_links_per_platform(client):
    pid = property_id(client, links=[IG])
    client.post(f'/api/properties/{pid}/reels', headers=csrf(client), json={'url': IG2})
    reels = client.get(f'/api/properties/{pid}/reels').json()['reels']
    assert sorted(r['permalink'] for r in reels) == sorted([rf.parse_link(IG)['url'], rf.parse_link(IG2)['url']])


# ------------------------------------------------- refreshes + analytics ----

def test_refresh_flow_analytics_history_and_logs(hub, scraper):
    c, h = hub
    pid = new_property(c, h, links=[IG, IG2, FB])['id']
    assert rf.run_queue() == 1  # the link_change refresh

    a = c.get(f'/v1/properties/{pid}/analytics', headers=h).json()
    assert a['last_refresh']['trigger'] == 'link_change' and a['last_refresh']['status'] == 'done'
    assert a['next_daily_refresh_at'].endswith('Z') and len(a['reels']) == 3
    ig = next(r for r in a['reels'] if r['platform'] == 'instagram')
    assert ig['views'] > 100 and ig['shares'] is None and ig['ok_at'] == ig['checked_at'] and ig['error'] is None
    assert ig['source'] == 'reels_tab' and ig['link_id']

    # Facebook now fails: last good numbers stay, error is reported, ok_at unchanged.
    scraper['fail'] = {'facebook'}
    fb_before = next(r for r in a['reels'] if r['platform'] == 'facebook')
    r = c.post(f'/v1/properties/{pid}/refresh', headers=h, json={'requested_by': 'agent@example.com'})
    assert r.status_code == 202
    refresh = r.json()['refresh']
    assert refresh['status'] == 'queued' and refresh['requested_by'] == 'agent@example.com'
    assert c.post(f'/v1/properties/{pid}/refresh', headers=h).json()['refresh']['id'] == refresh['id']  # same one
    rf.run_queue()

    done = c.get(f"/v1/refreshes/{refresh['id']}", headers=h).json()
    assert done['status'] == 'partial' and done['finished_at'] and len(done['links']) == 3
    fb_state = next(x for x in done['links'] if x['platform'] == 'facebook')
    assert fb_state['error']['code'] == 'upstream_blocked'
    assert all(x['status'] == 'ok' for x in done['links'] if x['platform'] == 'instagram')

    fb = c.get(f'/v1/properties/{pid}/analytics?platform=facebook', headers=h).json()['reels'][0]
    assert fb['views'] == fb_before['views'] and fb['ok_at'] == fb_before['ok_at']
    assert fb['error']['code'] == 'upstream_blocked' and fb['checked_at'] >= fb['ok_at']
    statuses = {x['platform']: x['status'] for x in c.get(f'/v1/properties/{pid}/links', headers=h).json()['links']}
    assert statuses == {'instagram': 'ok', 'facebook': 'blocked'}

    hist = c.get(f'/v1/properties/{pid}/history', headers=h).json()['items']
    assert len(hist) == 5 and {x['trigger'] for x in hist} == {'link_change', 'manual'}
    logs = c.get(f'/v1/properties/{pid}/logs?status=failed', headers=h).json()['items']
    assert len(logs) == 1 and logs[0]['platform'] == 'facebook' and logs[0]['duration_ms'] is not None
    assert len(c.get(f'/v1/properties/{pid}/logs', headers=h).json()['items']) == 6

    again = c.post(f'/v1/properties/{pid}/refresh', headers=h)  # cooldown on manual refresh
    assert again.status_code == 429 and int(again.headers['Retry-After']) > 0
    listed = c.get(f'/v1/refreshes?property_id={pid}', headers=h).json()['items']
    assert [x['trigger'] for x in listed] == ['manual', 'link_change']

    bulk = c.get('/v1/analytics?updated_since=2000-01-01T00:00:00Z', headers=h).json()['items']
    assert [x['property_id'] for x in bulk] == [pid]
    assert c.get('/v1/analytics?updated_since=2999-01-01T00:00:00Z', headers=h).json()['items'] == []


def test_refresh_by_platform(hub):
    c, h = hub
    pid = new_property(c, h, links=[IG, FB])['id']
    rf.run_queue()
    with storage.db() as db:  # skip the cooldown from the link_change refresh
        db.execute("UPDATE refreshes SET created_at='2000-01-01T00:00:00+00:00'")
    r = c.post(f'/v1/properties/{pid}/refresh', headers=h, json={'platforms': ['facebook']}).json()['refresh']
    assert [x['platform'] for x in r['links']] == ['facebook']
    assert c.post(f'/v1/properties/{pid}/refresh', headers=h, json={'platforms': ['tiktok']}).status_code in (202, 422)


def test_refresh_rules(hub):
    c, h = hub
    pid = new_property(c, h)['id']
    rf.run_queue()
    c.post(f'/v1/properties/{pid}/archive', headers=h)
    assert c.post(f'/v1/properties/{pid}/refresh', headers=h).status_code == 409


def test_refresh_idempotency_and_cancel(hub):
    c, h = hub
    pid = new_property(c, h)['id']
    rf.run_queue()
    with storage.db() as db:
        db.execute("UPDATE refreshes SET created_at='2000-01-01T00:00:00+00:00'")
    hh = {**h, 'Idempotency-Key': 'refresh-abc'}
    first = c.post(f'/v1/properties/{pid}/refresh', headers=hh).json()['refresh']
    assert c.post(f'/v1/properties/{pid}/refresh', headers=hh).json()['refresh']['id'] == first['id']
    cancelled = c.post(f"/v1/refreshes/{first['id']}/cancel", headers=h).json()
    assert cancelled['status'] == 'cancelled'
    assert c.post(f"/v1/refreshes/{first['id']}/cancel", headers=h).status_code == 409


def test_bulk_refresh(hub):
    c, h = hub
    a = new_property(c, h, address='1 Bulk St')['id']
    b = new_property(c, h, address='2 Bulk St')['id']
    rf.run_queue()
    c.post(f'/v1/properties/{b}/archive', headers=h)
    r = c.post('/v1/refreshes', headers=h, json={'all_active': True}).json()
    assert [x['property_id'] for x in r['refreshes']] == [a] and r['refreshes'][0]['trigger'] == 'bulk'


def test_daily_enqueue_once_per_day(hub):
    c, h = hub
    active = new_property(c, h, address='1 Daily St')['id']
    archived = new_property(c, h, address='2 Daily St')['id']
    rf.run_queue()
    c.post(f'/v1/properties/{archived}/archive', headers=h)
    assert rf.enqueue_daily('2026-11-18') == 1
    assert rf.enqueue_daily('2026-11-18') == 0  # already ran today
    assert rf.run_queue() == 1
    assert c.get(f'/v1/properties/{active}/analytics', headers=h).json()['last_refresh']['trigger'] == 'daily'


def test_daily_schedule_follows_daylight_saving():
    winter = rf.next_daily_run(datetime(2026, 1, 10, 6, 0, tzinfo=timezone.utc))
    summer = rf.next_daily_run(datetime(2026, 7, 10, 6, 0, tzinfo=timezone.utc))
    assert (winter.hour, summer.hour) == (13, 12)  # 07:00 America/Chicago
    late = rf.next_daily_run(datetime(2026, 1, 10, 14, 0, tzinfo=timezone.utc))
    assert late.day == 11
    assert rf.schedule_info()['timezone'] == 'America/Chicago'


def test_health_needs_no_key(db_path):
    with new_client() as c:
        r = c.get('/v1/health').json()
        assert r['ok'] and r['time'].endswith('Z')


# ------------------------------------------------------------- migration ----

def test_old_listing_columns_are_dropped_and_links_kept(db_path):
    """A v0.8 database (external_id, price, ... and one-link-per-platform) upgrades without losing links."""
    with storage.db() as c:
        for col in storage.UNUSED_PROPERTY_COLUMNS:
            c.execute(f"ALTER TABLE properties ADD COLUMN {col} TEXT")
        c.execute("CREATE UNIQUE INDEX idx_properties_external ON properties(owner_id, external_id) "
                  "WHERE external_id IS NOT NULL")
        c.execute("INSERT INTO properties(id,owner_id,address,created_at,external_id,price) "
                  "VALUES('p1','u1','1 Old St',?,'hub-1','$1')", (storage.now(),))
    prop = {'id': 'p1', 'owner_id': 'u1'}
    rf.add_link(prop, IG)
    rf.add_link(prop, IG2)  # two instagram links
    storage.init_db()
    with storage.db() as c:
        cols = storage._columns(c, 'properties')
    assert not set(storage.UNUSED_PROPERTY_COLUMNS) & cols
    assert len(rf.current_links('p1')) == 2
    storage.init_db()  # running again changes nothing
    assert len(rf.current_links('p1')) == 2


def test_server_side_failure_is_error_not_blocked(hub, monkeypatch):
    """A missing browser is our problem: status 'error', short message, last good numbers kept."""
    c, h = hub
    pid = new_property(c, h)['id']
    rf.run_queue()  # good read
    def no_browser(url, job=None, pw=None):
        raise RuntimeError("BrowserType.launch: Executable doesn't exist at /opt/x/chrome\n╔══ banner ══╗\n║ run install ║")
    monkeypatch.setattr(ps, 'scrape', no_browser)
    with storage.db() as db:
        db.execute("UPDATE refreshes SET created_at='2000-01-01T00:00:00+00:00'")
    c.post(f'/v1/properties/{pid}/refresh', headers=h)
    rf.run_queue()
    reel = c.get(f'/v1/properties/{pid}/analytics', headers=h).json()['reels'][0]
    assert reel['views'] is not None and reel['ok_at'] <= reel['checked_at']  # last good numbers kept
    assert reel['error'] == {'code': 'internal', 'message': "BrowserType.launch: Executable doesn't exist at /opt/x/chrome"}
    assert c.get(f'/v1/properties/{pid}/links', headers=h).json()['links'][0]['status'] == 'error'


def test_unset_postman_variable_gets_a_clear_message(hub):
    c, _ = hub
    r = c.get('/v1/properties', headers={'Authorization': 'Bearer {{apiKey}}'})
    assert r.status_code == 401 and 'No API key was sent' in r.json()['error']['message']


def test_partial_read_keeps_last_known_views(hub, scraper, monkeypatch):
    """Render case: likes/comments read but views missing -> show the last known views, keep raw history."""
    c, h = hub
    pid = new_property(c, h)['id']
    rf.run_queue()  # full read
    full = c.get(f'/v1/properties/{pid}/analytics', headers=h).json()['reels'][0]
    assert full['views'] is not None
    monkeypatch.setattr(ps, 'scrape', lambda url, job=None, pw=None:
                        {'views': None, 'likes': 999, 'comments': 3, 'shares': None, 'detail': 'reels tab empty'})
    with storage.db() as db:
        db.execute("UPDATE refreshes SET created_at='2000-01-01T00:00:00+00:00'")
    c.post(f'/v1/properties/{pid}/refresh', headers=h)
    rf.run_queue()
    now_ = c.get(f'/v1/properties/{pid}/analytics', headers=h).json()['reels'][0]
    assert now_['views'] == full['views'] and now_['likes'] == 999 and now_['comments'] == 3
    hist = c.get(f'/v1/properties/{pid}/history', headers=h).json()['items']
    assert hist[0]['views'] is None and hist[1]['views'] == full['views']  # history stays as read
    dash = c.get(f'/api/properties/{pid}/reels').json()['reels'][0]['metrics']
    assert dash['views'] == full['views'] and dash['likes'] == 999
    card = c.get('/api/properties').json()[0]
    assert card['total_views'] == full['views']
