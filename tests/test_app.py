import pytest
from fastapi.testclient import TestClient

from app import main
from app.db import storage
from app.services.urls import InvalidReelURL, parse_reel_url


def new_client():
    return TestClient(main.app)


def csrf(c):
    return {'X-CSRF-Token': c.get('/api/me').json()['csrf']}


def signup(c, email='owner@example.com', name='Olivia Owner', password='correct-horse-1'):
    r = c.post('/api/auth/signup', headers=csrf(c), json={'name': name, 'email': email, 'password': password})
    assert r.status_code == 200, r.text
    return r.json()['user']


DEFAULT_LINK = 'https://www.youtube.com/shorts/DefaultLnk1'


def property_id(c, address='123 Test Street, Chicago', links=(DEFAULT_LINK,)):
    r = c.post('/api/properties', headers=csrf(c), json={'address': address, 'links': list(links)})
    assert r.status_code == 200, r.text
    return r.json()['id']


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    """Fresh, empty database for each test (SQLite file, or a wiped TEST_DATABASE_URL Postgres schema)."""
    if storage.pg_enabled():
        with storage.db() as c:
            c.executescript('DROP SCHEMA public CASCADE; CREATE SCHEMA public;')
    else:
        monkeypatch.setattr(storage, 'DB_PATH', tmp_path / 'test.sqlite')
    storage.init_db()
    main.auth.login_limiter._hits.clear()


@pytest.fixture()
def client(db_path):
    """A logged-in agent."""
    with new_client() as c:
        signup(c)
        yield c


def test_url_allowlist_and_embedding():
    ig = parse_reel_url('https://www.instagram.com/reel/Cx_abc12/?utm_source=copy')
    assert ig['url'] == 'https://www.instagram.com/reel/Cx_abc12/'
    assert ig['embed_url'] == 'https://www.instagram.com/reel/Cx_abc12/embed/'
    fb = parse_reel_url('https://www.facebook.com/reel/123456789/')
    assert fb['platform'] == 'facebook'
    assert 'plugins/video.php' in fb['embed_url']
    watch = parse_reel_url('https://www.facebook.com/watch/?v=123456')
    assert watch['platform'] == 'facebook'
    share = parse_reel_url('https://www.facebook.com/share/r/abcDEF123/')
    assert share['key'] == 'share:abcDEF123'
    for bad in ['http://instagram.com/reel/C1234/', 'https://instagram.com.evil.com/reel/C1234/',
                'https://www.instagram.com/p/C1234/', 'https://127.0.0.1/admin',
                'https://evil.com/reel/1234', 'https://user:pass@www.instagram.com/reel/C1234/',
                'https://www.instagram.com:8443/reel/C1234/']:
        with pytest.raises((InvalidReelURL, ValueError)):
            parse_reel_url(bad)


def test_csrf_required(client):
    c = client
    body = {'address': 'Demo location 123 Test Street', 'links': [DEFAULT_LINK]}
    assert c.post('/api/properties', json=body).status_code == 403
    assert c.post('/api/properties', headers={'X-CSRF-Token': 'incorrect'}, json=body).status_code == 403
    assert c.post('/api/properties', headers=csrf(c), json=body).status_code == 200


def test_old_oauth_and_manual_routes_are_gone(client):
    assert client.post('/api/reels/x/sync').status_code == 404
    assert client.get('/oauth/instagram/start').status_code == 404
    assert client.get('/api/accounts').status_code == 404


def test_no_properties_seeded_for_new_owner(client):
    assert client.get('/api/properties').json() == []


def test_paste_url_link(client):
    c = client
    pid = property_id(c)
    url = 'https://www.instagram.com/reel/TestReel7/'
    r = c.post(f'/api/properties/{pid}/reels', json={'url': url}, headers=csrf(c))
    assert r.status_code == 200
    item = r.json()['reel']
    linked = c.get(f'/api/properties/{pid}/reels').json()['reels']
    assert len(linked) == 2 and all(x['metrics'] is None for x in linked)  # default link + this one
    # Same URL is idempotent
    assert c.post(f'/api/properties/{pid}/reels', json={'url': url}, headers=csrf(c)).json()['added'] is False
    assert len(c.get(f'/api/properties/{pid}/reels').json()['reels']) == 2
    assert c.delete(f'/api/properties/{pid}/reels/{item["id"]}', headers=csrf(c)).status_code == 200
    assert len(c.get(f'/api/properties/{pid}/reels').json()['reels']) == 1
    assert c.post(f'/api/properties/{pid}/reels', json={'url': 'https://evil.com/reel/1'},
                  headers=csrf(c)).status_code == 422


def test_manual_counts_endpoint_removed(client):
    c = client
    pid = property_id(c)
    reel = c.post(f'/api/properties/{pid}/reels', json={'url': 'https://www.instagram.com/reel/PubReel2026/'},
                  headers=csrf(c)).json()['reel']
    r = c.post(f'/api/reels/{reel["id"]}/public-counters', headers=csrf(c),
               json={'views': 190, 'source_note': 'Reel public page'})
    assert r.status_code in (404, 405)


def test_property_csv_export_restricted_and_provenance(client):
    c = client
    pid = property_id(c)
    add = c.post(f'/api/properties/{pid}/reels',
                 json={'url': 'https://www.facebook.com/reel/123456987'}, headers=csrf(c))
    assert add.status_code == 200
    reel_id = add.json()['reel']['id']
    storage.save_snapshot(reel_id, {'views': 211, 'comments': 4}, {}, source='playwright_public',
                          source_detail='Observed counter manually')
    exported = c.get(f'/api/properties/{pid}/export.csv')
    assert exported.status_code == 200
    assert 'text/csv' in exported.headers['content-type']
    assert '211' in exported.text and 'Observed counter manually' in exported.text
    assert 'playwright_public' in exported.text
    assert c.get('/api/properties/another-users-id/export.csv').status_code == 404


def test_edit_and_delete_property(client):
    c = client
    pid = property_id(c)
    assert c.patch(f'/api/properties/{pid}', headers=csrf(c), json={'address': '9 New Address Rd'}).status_code == 200
    assert c.get(f'/api/properties/{pid}').json()['address'] == '9 New Address Rd'
    c.post(f'/api/properties/{pid}/reels', json={'url': 'https://www.instagram.com/reel/GoneSoon1/'}, headers=csrf(c))
    assert c.delete(f'/api/properties/{pid}', headers=csrf(c)).status_code == 200
    assert c.get(f'/api/properties/{pid}').status_code == 404
    assert storage.get_one('SELECT count(*) n FROM reels')['n'] == 0


def test_spa_routes_serve_the_app(db_path):
    with new_client() as c:
        for path in ('/', '/login', '/signup', '/invite/abc123', '/properties', '/properties/xyz', '/brokers'):
            r = c.get(path)
            assert r.status_code == 200 and '<div id="view">' in r.text, path
        assert c.get('/api/nope').status_code == 404
        # Versioned asset URLs + revalidation: a stale cached app.js must never meet a new index.html
        html = c.get('/login').text
        assert '/static/app.js?v=' in html and '/static/style.css?v=' in html
        assert c.get('/static/app.js').headers['cache-control'] == 'no-cache'


def test_add_property_needs_address_and_at_least_one_link(client):
    c = client
    h = csrf(c)
    assert c.post('/api/properties', headers=h, json={'address': '1 No Links St'}).status_code == 422
    assert c.post('/api/properties', headers=h, json={'address': '1 No Links St', 'links': []}).status_code == 422
    blank = c.post('/api/properties', headers=h, json={'address': '1 No Links St', 'links': ['  ']})
    assert blank.status_code == 422 and 'at least one link' in blank.json()['detail']
    bad = c.post('/api/properties', headers=h, json={'address': '2 Bad Link St',
                                                     'links': [DEFAULT_LINK, 'https://evil.com/reel/1']})
    assert bad.status_code == 422 and bad.json()['detail'].startswith('Link 2:')
    assert c.get('/api/properties').json() == []  # nothing saved


def test_several_links_for_the_same_platform(client):
    c = client
    links = ['https://www.instagram.com/reel/FirstOne1/', 'https://www.instagram.com/reel/SecondOne2/',
             'https://www.instagram.com/reel/FirstOne1/', 'https://vt.tiktok.com/ZSpaGf8Yp/']  # duplicate dropped
    pid = property_id(c, links=links)
    reels = c.get(f'/api/properties/{pid}/reels').json()['reels']
    assert sorted(r['platform'] for r in reels) == ['instagram', 'instagram', 'tiktok']
    third = c.post(f'/api/properties/{pid}/reels', headers=csrf(c),
                   json={'url': 'https://www.instagram.com/reel/ThirdOne3/'}).json()
    assert third['added'] is True
    assert len(c.get(f'/api/properties/{pid}/reels').json()['reels']) == 4
    card = c.get('/api/properties').json()[0]
    assert set(card) >= {'id', 'address', 'reel_count'} and card['reel_count'] == 4
    assert not {'external_id', 'price', 'mls_number', 'status'} & set(card)


def test_last_link_cannot_be_removed(client):
    c = client
    pid = property_id(c)
    only = c.get(f'/api/properties/{pid}/reels').json()['reels'][0]
    r = c.delete(f'/api/properties/{pid}/reels/{only["id"]}', headers=csrf(c))
    assert r.status_code == 409 and 'at least one link' in r.json()['detail']


def test_archive_and_unarchive_from_dashboard(client):
    c = client
    pid = property_id(c)
    assert c.post(f'/api/properties/{pid}/archive').status_code == 403  # CSRF required
    assert c.post(f'/api/properties/{pid}/archive', headers=csrf(c)).status_code == 200
    prop = c.get(f'/api/properties/{pid}').json()
    assert prop['active'] == 0 and prop['archived_at']
    assert c.get('/api/properties').json()[0]['active'] == 0
    blocked = c.post(f'/api/properties/{pid}/fetch-public', headers=csrf(c), json={})
    assert blocked.status_code == 409 and 'archived' in blocked.json()['detail']
    assert c.post(f'/api/properties/{pid}/unarchive', headers=csrf(c)).status_code == 200
    prop = c.get(f'/api/properties/{pid}').json()
    assert prop['active'] == 1 and prop['archived_at'] is None
    with new_client() as other:  # someone else's property: not found
        signup(other, email='other-agent@example.com')
        assert other.post(f'/api/properties/{pid}/archive', headers=csrf(other)).status_code == 404
