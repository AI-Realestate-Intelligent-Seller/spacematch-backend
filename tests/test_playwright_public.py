"""v0.4 Playwright public counters - parsing with synthetic data and the job API (browser mocked)."""
import json
import re
import time

from app.sources import playwright_scraper as ps
from test_app import client, csrf, db_path, new_client, property_id, signup  # noqa: F401
from app.services.urls import parse_reel_url

IG = 'https://www.instagram.com/spacematchchicago/reel/DTOz5t5jLQH/'
FB = 'https://www.facebook.com/share/r/1cHAH3Wqdg/'


def test_instagram_url_with_username_is_accepted():
    assert parse_reel_url(IG)['url'] == 'https://www.instagram.com/reel/DTOz5t5jLQH/'


def test_ig_index_matches_shortcode_only():
    grid = json.dumps({'data': {'edges': [
        {'node': {'media': {'code': 'OTHER1', 'play_count': 99, 'like_count': 1}}},
        {'node': {'media': {'code': 'DTOz5t5jLQH', 'play_count': 30630, 'like_count': 408,
                            'comment_count': 9, 'user': {'username': 'spacematchchicago'}}}},
    ]}})
    idx = ps.ig_media_index(['for (;;);' + grid])
    assert idx['DTOz5t5jLQH']['play_count'] == 30630
    assert idx['DTOz5t5jLQH']['username'] == 'spacematchchicago'
    assert idx['OTHER1']['play_count'] == 99


def test_compact_tile_counts():
    assert ps.parse_compact('30.6K') == 30600
    assert ps.parse_compact('1.7M') == 1_700_000
    assert ps.parse_compact('1,234') == 1234


def test_fb_views_tied_to_video_id():
    blob = ('"associated_video":{"is_live_streaming":false,"id":"111"},"video_view_count":5,"play_count":7,'
            '"id":"ZmVlZGJhY2s6b3RoZXJvdGhlcm90aGVy"}'
            '"associated_video":{"is_live_streaming":false,"id":"1413509380910133"},"video_view_count":768802,'
            '"play_count":1799009,"id":"ZmVlZGJhY2s6cGZiaWQwMmt0VHFzeUFj"}')
    views, fid = ps.fb_views_from_watch(blob, '1413509380910133')
    assert views == {'play_count': 1799009, 'video_view_count': 768802}
    assert fid == 'ZmVlZGJhY2s6cGZiaWQwMmt0VHFzeUFj'
    assert ps.fb_views_from_watch(blob, '999') == ({}, None)


def wait_job(c, job_id):
    for _ in range(400):  # up to 20 s - background jobs can be slow on a busy machine
        job = c.get(f'/api/scrape-jobs/{job_id}').json()
        if job['status'] != 'running':
            return job
        time.sleep(0.05)
    raise AssertionError('job did not finish')


def fake_scrape(url, job=None, pw=None):
    job.say('fake browser step')
    if 'facebook' in url:
        return {'views': 1799009, 'likes': 36453, 'comments': 1361, 'shares': 637, 'detail': 'fb test'}
    return {'views': 30630, 'likes': 408, 'comments': 9, 'shares': None, 'detail': 'ig test'}


class FakePW:
    def __enter__(self):
        return object()

    def __exit__(self, *a):
        return False


def test_fetch_public_job_saves_snapshot(client, monkeypatch):
    monkeypatch.setattr(ps, 'scrape', fake_scrape)
    monkeypatch.setattr('playwright.sync_api.sync_playwright', lambda: FakePW())
    c = client
    pid = property_id(c)
    reel = c.post(f'/api/properties/{pid}/reels', json={'url': IG}, headers=csrf(c)).json()['reel']
    r = c.post(f'/api/properties/{pid}/fetch-public', json={'show_window': False, 'reel_ids': [reel['id']]}, headers=csrf(c))
    assert r.status_code == 200
    job = wait_job(c, r.json()['job_id'])
    assert job['status'] == 'done'
    assert any('fake browser step' in line['msg'] for line in job['log'])
    linked = {x['id']: x for x in c.get(f'/api/properties/{pid}/reels').json()['reels']}[reel['id']]
    assert linked['metrics']['views'] == 30630
    assert linked['metrics']['reach'] is None
    assert linked['metric_source'] == 'playwright_public'


def test_fetch_all_public_for_property(client, monkeypatch):
    monkeypatch.setattr(ps, 'scrape', fake_scrape)
    monkeypatch.setattr('playwright.sync_api.sync_playwright', lambda: FakePW())
    c = client
    pid = property_id(c)
    for url in (IG, FB):
        c.post(f'/api/properties/{pid}/reels', json={'url': url}, headers=csrf(c))
    r = c.post(f'/api/properties/{pid}/fetch-public', json={}, headers=csrf(c))
    job = wait_job(c, r.json()['job_id'])
    assert len([x for x in job['results'] if x['ok']]) == 3  # default link + IG + FB
    fb = [x for x in c.get(f'/api/properties/{pid}/reels').json()['reels'] if x['platform'] == 'facebook'
          and x['metric_source'] == 'playwright_public'][0]
    assert fb['metrics']['views'] == 1799009 and fb['metrics']['shares'] == 637


def test_failed_scrape_does_not_invent_counts(client, monkeypatch):
    def boom(url, job=None, pw=None):
        raise ps.ScrapeError('blocked')
    monkeypatch.setattr(ps, 'scrape', boom)
    monkeypatch.setattr('playwright.sync_api.sync_playwright', lambda: FakePW())
    c = client
    pid = property_id(c)
    reel = c.post(f'/api/properties/{pid}/reels', json={'url': IG}, headers=csrf(c)).json()['reel']
    job = wait_job(c, c.post(f'/api/properties/{pid}/fetch-public', headers=csrf(c)).json()['job_id'])
    assert job['status'] == 'error'
    assert c.get(f'/api/reels/{reel["id"]}/history').json() == []


def test_job_endpoints_require_login_and_csrf(client):
    c = client
    pid = property_id(c)
    assert c.post(f'/api/properties/{pid}/fetch-public', json={}).status_code == 403
    assert c.get('/api/scrape-jobs/nope').status_code == 404


def test_jobs_only_visible_to_their_starter(client, monkeypatch):
    monkeypatch.setattr(ps, 'scrape', fake_scrape)
    monkeypatch.setattr('playwright.sync_api.sync_playwright', lambda: FakePW())
    pid = property_id(client)
    reel = client.post(f'/api/properties/{pid}/reels', json={'url': IG}, headers=csrf(client)).json()['reel']
    job_id = client.post(f'/api/properties/{pid}/fetch-public', headers=csrf(client), json={}).json()['job_id']
    wait_job(client, job_id)
    with new_client() as other:
        signup(other, email='other@example.com')
        assert other.get(f'/api/scrape-jobs/{job_id}').status_code == 404


# ---- YouTube + TikTok ----

def test_youtube_and_tiktok_urls():
    for u in ('https://www.youtube.com/shorts/JX1mw5HWeTM', 'https://youtu.be/JX1mw5HWeTM',
              'https://www.youtube.com/watch?v=JX1mw5HWeTM&t=3'):
        assert parse_reel_url(u)['key'] == 'JX1mw5HWeTM'
        assert parse_reel_url(u)['platform'] == 'youtube'
    share = parse_reel_url('https://vt.tiktok.com/ZSpaGf8Yp/')
    assert share['platform'] == 'tiktok' and share['key'] == 'share:ZSpaGf8Yp'
    full = parse_reel_url('https://www.tiktok.com/@thespacematchteam/video/7122941464534912302?_r=1')
    assert full['key'] == '7122941464534912302'
    import pytest
    from app.services.urls import InvalidReelURL
    for bad in ('https://www.youtube.com/@channel', 'https://www.tiktok.com/@user', 'https://youtube.com.evil.com/shorts/JX1mw5HWeTM'):
        with pytest.raises(InvalidReelURL):
            parse_reel_url(bad)


def test_yt_counts_tied_to_video():
    blob = ('"externalVideoId":"OTHERVIDEO1","viewCount":"999","likeCount":"5"'
            + 'x' * 5000 +
            '"isUnlisted":false,"viewCount":"6","category":"People","externalVideoId":"JX1mw5HWeTM","likeCount":"0"'
            '"commentsHeaderRenderer":{"countText":{"runs":[{"text":"2,461,983"},{"text":" Comments"}')
    assert ps.yt_counts(blob, 'JX1mw5HWeTM') == {'views': 6, 'likes': 0, 'comments': 2461983}
    assert ps.yt_counts('"externalVideoId":"JX1mw5HWeTM","viewCount":"6"', 'JX1mw5HWeTM')['comments'] is None


def test_tt_stats_from_page_data():
    data = {'__DEFAULT_SCOPE__': {'webapp.video-detail': {'itemInfo': {'itemStruct': {
        'id': '7122941464534912302', 'author': {'uniqueId': 'thespacematchteam'},
        'stats': {'playCount': 118, 'diggCount': 4, 'commentCount': 0, 'shareCount': 1},
        'statsV2': {'playCount': '118', 'diggCount': '4', 'commentCount': '0', 'shareCount': '1', 'collectCount': '0'}}}}}}
    html = '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">' + json.dumps(data) + '</script>'
    stats, vid, author = ps.tt_stats(html)
    assert (stats['playCount'], stats['diggCount'], stats['commentCount'], stats['shareCount']) == (118, 4, 0, 1)
    assert vid == '7122941464534912302' and author == 'thespacematchteam'
    assert ps.tt_stats('<html>login wall</html>') == ({}, None, None)


import pytest  # noqa: E402


@pytest.mark.skipif(__import__('app.db.storage', fromlist=['storage']).pg_enabled(), reason='SQLite-only upgrade path')
def test_old_database_upgrades_without_losing_data(tmp_path, monkeypatch):
    import sqlite3
    from app.db import storage
    path = tmp_path / 'old.sqlite'
    monkeypatch.setattr(storage, 'DB_PATH', path)
    storage.init_db()
    with storage.db() as c:  # make it look like a v0.3 database
        sql = c.execute("SELECT sql FROM sqlite_master WHERE name='reels'").fetchone()[0]
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=OFF")
    old_sql = sql.replace("'instagram','facebook','youtube','tiktok'", "'instagram','facebook'")
    conn.executescript(f"DROP TABLE reels; {old_sql};")
    conn.commit()
    conn.close()
    pid = 'legacy-property'
    with storage.db() as c:
        c.execute('INSERT INTO properties(id,owner_id,address,created_at) VALUES(?,?,?,?)', (pid, storage.DEMO_USER, '1 Old St', storage.now()))
    reel = storage.save_reel(storage.DEMO_USER, 'instagram', 'KeepMe1', permalink='https://www.instagram.com/reel/KeepMe1/')
    with storage.db() as c:
        c.execute('INSERT INTO property_reels(property_id,reel_id,linked_at) VALUES(?,?,?)', (pid, reel['id'], storage.now()))
    storage.save_snapshot(reel['id'], {'views': 5}, {}, source='playwright_public')
    storage.init_db()  # runs the upgrade
    assert storage.get_one('SELECT count(*) n FROM metric_snapshots')['n'] == 1
    assert storage.get_one('SELECT count(*) n FROM property_reels')['n'] == 1
    storage.save_reel(storage.DEMO_USER, 'tiktok', '7122941464534912302', permalink='https://www.tiktok.com/@a/video/7122941464534912302')


def test_terminal_log_has_one_line_per_reel_seen():
    """Every reel in the captured Instagram data is logged once, in the agreed format."""
    import logging
    lines = []

    class Grab(logging.Handler):
        def emit(self, record):
            lines.append(record.levelname + ' ' + record.getMessage())
    grab = Grab()
    ps.reel_log.addHandler(grab)
    try:
        s = type('S', (), {'logged': set()})()
        grid = json.dumps({'data': {'edges': [
            {'node': {'media': {'code': 'AAA111', 'play_count': 77, 'like_count': 1, 'comment_count': 0}}},
            {'node': {'media': {'code': 'BBB222', 'like_count': 5}}},  # no play count yet
        ]}})
        ps.log_seen_reels(s, [grid])
        ps.log_seen_reels(s, [grid])  # nothing logged twice
        ps.log_seen_reels(s, [grid], final=True)  # leftovers with whatever is known
    finally:
        ps.reel_log.removeHandler(grab)
    assert len(lines) == 2
    assert re.fullmatch(r'INFO reel = AAA111, date \+ timestamp: \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ, '
                        r'playcount = 77, like_count = 1, comments_count = 0', lines[0])
    assert lines[1].startswith('INFO reel = BBB222, ')
    assert lines[1].endswith('playcount = N/A, like_count = 5, comments_count = N/A')
