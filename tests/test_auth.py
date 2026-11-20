"""Accounts, sessions, broker invites and per-user data isolation."""
import re

import pytest

from app.api import brokers as brokers_module
from app.db import storage
from app.core.security import hash_password, verify_password
from test_app import client, csrf, db_path, new_client, property_id, signup  # noqa: F401

IG = 'https://www.instagram.com/reel/SharedReel1/'


@pytest.fixture()
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(brokers_module, 'send_email', lambda to, subject, text: sent.append((to, subject, text)) or True)
    monkeypatch.setattr(brokers_module, 'email_configured', lambda: True)
    return sent


def invite(c, email='bob@broker.com', pid=None, access='view', name='Bob Broker'):
    r = c.post('/api/brokers/invite', headers=csrf(c),
               json={'email': email, 'name': name, 'brokerage': 'Acme Realty', 'property_id': pid, 'access': access})
    assert r.status_code == 200, r.text
    return r.json()


def token_from(outbox):
    return re.search(r'/invite/([\w-]+)', outbox[-1][2]).group(1)


def broker_client(token, password='broker-pass-1'):
    c = new_client()
    r = c.post(f'/api/invites/{token}/accept', headers=csrf(c), json={'password': password})
    assert r.status_code == 200, r.text
    assert r.json()['user']['role'] == 'broker'
    return c


def test_password_hashing():
    h = hash_password('s3cret-pass')
    assert h.startswith('scrypt$') and 's3cret' not in h
    assert verify_password('s3cret-pass', h) and not verify_password('wrong', h)
    assert not verify_password('x', None) and not verify_password('x', 'garbage')


def test_api_requires_login(db_path):
    with new_client() as c:
        assert c.get('/api/me').json()['user'] is None
        for path in ('/api/properties', '/api/brokers', '/api/scrape-jobs/x', '/api/properties/x/export.csv'):
            assert c.get(path).status_code == 401, path
        assert c.post('/api/properties', headers=csrf(c), json={'address': '1 Nowhere St'}).status_code == 401


def test_signup_login_logout(db_path):
    with new_client() as c:
        user = signup(c, email='Ann@Example.com')
        assert user['email'] == 'ann@example.com' and user['role'] == 'agent'
        assert c.get('/api/me').json()['user']['id'] == user['id']
        assert c.post('/api/auth/signup', headers=csrf(c),
                      json={'name': 'Again', 'email': 'ann@example.com', 'password': 'another-pass'}).status_code == 409
        assert c.post('/api/auth/logout', headers=csrf(c)).status_code == 200
        assert c.get('/api/properties').status_code == 401
        assert c.post('/api/auth/login', headers=csrf(c),
                      json={'email': 'ann@example.com', 'password': 'nope-nope'}).status_code == 401
        assert c.post('/api/auth/login', headers=csrf(c),
                      json={'email': 'ANN@example.com', 'password': 'correct-horse-1'}).status_code == 200
        assert c.get('/api/properties').status_code == 200


def test_signup_validation_and_login_csrf(db_path):
    with new_client() as c:
        h = csrf(c)
        assert c.post('/api/auth/signup', headers=h, json={'name': 'Al', 'email': 'bad', 'password': 'longenough'}).status_code == 422
        assert c.post('/api/auth/signup', headers=h, json={'name': 'Al', 'email': 'a@b.co', 'password': 'short'}).status_code == 422
        assert c.post('/api/auth/login', json={'email': 'a@b.co', 'password': 'x'}).status_code == 403


def test_login_rate_limited(db_path):
    with new_client() as c:
        signup(c)
        c.post('/api/auth/logout', headers=csrf(c))
        for _ in range(10):
            c.post('/api/auth/login', headers=csrf(c), json={'email': 'owner@example.com', 'password': 'wrong'})
        r = c.post('/api/auth/login', headers=csrf(c), json={'email': 'owner@example.com', 'password': 'correct-horse-1'})
        assert r.status_code == 429


def test_owners_cannot_see_each_others_data(client):
    pid = property_id(client)
    reel = client.post(f'/api/properties/{pid}/reels', json={'url': IG}, headers=csrf(client)).json()['reel']
    with new_client() as other:
        signup(other, email='mallory@example.com')
        assert other.get('/api/properties').json() == []
        for path in (f'/api/properties/{pid}', f'/api/properties/{pid}/reels', f'/api/properties/{pid}/export.csv',
                     f'/api/reels/{reel["id"]}/history'):
            assert other.get(path).status_code == 404, path
        h = csrf(other)
        assert other.post(f'/api/properties/{pid}/reels', headers=h, json={'url': IG}).status_code == 404
        assert other.patch(f'/api/properties/{pid}', headers=h, json={'address': 'Hijacked'}).status_code == 404
        assert other.delete(f'/api/properties/{pid}', headers=h).status_code == 404
        assert other.post(f'/api/properties/{pid}/fetch-public', headers=h, json={}).status_code == 404
        assert other.post('/api/brokers/invite', headers=h, json={'email': 'x@y.co', 'name': 'Xavier',
                                                                  'property_id': pid}).status_code == 404


def test_broker_invite_flow_and_view_access(client, outbox):
    pid = property_id(client)
    other_pid = property_id(client, '77 Private Lane')
    client.post(f'/api/properties/{pid}/reels', json={'url': IG}, headers=csrf(client))
    result = invite(client, pid=pid, access='view')
    assert result['pending'] and result['emailed'] and 'invite_link' not in result
    assert outbox[-1][0] == 'bob@broker.com'
    token = token_from(outbox)

    with new_client() as anon:
        assert anon.get(f'/api/invites/{token}').json()['email'] == 'bob@broker.com'
        assert anon.post('/api/auth/login', headers=csrf(anon),  # can't log in before setting a password
                         json={'email': 'bob@broker.com', 'password': 'anything'}).status_code == 401

    with broker_client(token) as b:
        props = b.get('/api/properties').json()
        assert [p['id'] for p in props] == [pid] and props[0]['access'] == 'view'
        assert len(b.get(f'/api/properties/{pid}/reels').json()['reels']) == 2  # default link + IG
        assert b.get(f'/api/properties/{pid}/export.csv').status_code == 200
        assert b.get(f'/api/properties/{other_pid}').status_code == 404
        h = csrf(b)
        assert b.post(f'/api/properties/{pid}/reels', headers=h,
                      json={'url': 'https://www.instagram.com/reel/NotAllowed1/'}).status_code == 403
        assert b.post(f'/api/properties/{pid}/fetch-public', headers=h, json={}).status_code == 403
        assert b.patch(f'/api/properties/{pid}', headers=h, json={'address': 'Changed'}).status_code == 403
        assert b.post('/api/properties', headers=h, json={'address': 'Broker property'}).status_code == 403
        assert b.get('/api/brokers').status_code == 403
        assert 'brokers' not in b.get(f'/api/properties/{pid}').json()

    with new_client() as anon:  # link is single-use
        assert anon.get(f'/api/invites/{token}').status_code == 404
        assert anon.post('/api/auth/login', headers=csrf(anon),
                         json={'email': 'bob@broker.com', 'password': 'broker-pass-1'}).status_code == 200


def test_full_access_broker_and_access_changes(client, outbox):
    pid = property_id(client)
    invite(client, pid=pid, access='full')
    owner_id = client.get('/api/me').json()['user']['id']
    with broker_client(token_from(outbox)) as b:
        r = b.post(f'/api/properties/{pid}/reels', headers=csrf(b), json={'url': IG})
        assert r.status_code == 200
        assert r.json()['reel']['owner_id'] == owner_id  # social links belong to the property's agent
        assert b.delete(f'/api/properties/{pid}', headers=csrf(b)).status_code == 403  # only owners delete
        broker_id = b.get('/api/me').json()['user']['id']

        # Owner sees the broker and the link, then downgrades and removes them.
        listed = client.get('/api/brokers').json()
        assert listed[0]['active'] and listed[0]['properties'][0]['access'] == 'full'
        assert len(client.get(f'/api/properties/{pid}/reels').json()['reels']) == 2  # default link + IG
        assert client.patch(f'/api/properties/{pid}/brokers/{broker_id}', headers=csrf(client),
                            json={'access': 'view'}).status_code == 200
        assert b.post(f'/api/properties/{pid}/reels', headers=csrf(b),
                      json={'url': 'https://www.instagram.com/reel/Second22/'}).status_code == 403
        assert client.delete(f'/api/properties/{pid}/brokers/{broker_id}', headers=csrf(client)).status_code == 200
        assert b.get('/api/properties').json() == []
        assert b.get(f'/api/properties/{pid}').status_code == 404


def test_existing_broker_gets_access_without_new_invite(client, outbox):
    pid1, pid2 = property_id(client), property_id(client, '2 Second Ave')
    invite(client, pid=pid1)
    with broker_client(token_from(outbox)) as b:
        result = invite(client, pid=pid2, access='full')
        assert not result['pending'] and 'Log in' in outbox[-1][2]
        assert {p['id']: p['access'] for p in b.get('/api/properties').json()} == {pid1: 'view', pid2: 'full'}


def test_invite_rules(client, outbox):
    pid = property_id(client)
    h = csrf(client)
    # can't invite an owner account (including yourself)
    r = client.post('/api/brokers/invite', headers=h, json={'email': 'owner@example.com', 'name': 'Me',
                                                            'property_id': pid})
    assert r.status_code == 409
    # new brokers need a name
    assert client.post('/api/brokers/invite', headers=h, json={'email': 'new@b.co', 'property_id': pid}).status_code == 422
    assert client.post('/api/brokers/invite', headers=h, json={'email': 'new@b.co', 'name': 'Ned',
                                                               'property_id': pid, 'access': 'admin'}).status_code == 422
    # brokers can't self sign up as owners with an invited email
    with new_client() as anon:
        invite(client, email='pending@b.co', pid=pid)
        assert anon.post('/api/auth/signup', headers=csrf(anon), json={'name': 'Pen', 'email': 'pending@b.co',
                                                                       'password': 'whatever-1'}).status_code == 409


def test_resend_and_expired_invite(client, outbox):
    pid = property_id(client)
    broker_id = invite(client, pid=pid)['broker_id']
    first = token_from(outbox)
    assert client.post(f'/api/brokers/{broker_id}/resend', headers=csrf(client)).status_code == 200
    second = token_from(outbox)
    assert first != second
    with storage.db() as c:
        c.execute('UPDATE invites SET expires_at=0')
    with new_client() as anon:
        assert anon.get(f'/api/invites/{second}').status_code == 404


def test_invite_link_returned_when_email_not_configured(client, monkeypatch):
    monkeypatch.setattr(brokers_module, 'send_email', lambda *a: False)
    monkeypatch.setattr(brokers_module, 'email_configured', lambda: False)
    result = invite(client, pid=property_id(client))
    assert '/invite/' in result['invite_link'] and not result['emailed']


def test_first_owner_adopts_legacy_data(db_path):
    with storage.db() as c:
        c.execute('INSERT INTO properties(id,owner_id,address,created_at) VALUES(?,?,?,?)', ('legacy-p', storage.DEMO_USER, '1 Legacy St', storage.now()))
    storage.save_reel(storage.DEMO_USER, 'instagram', 'Legacy1', permalink='https://www.instagram.com/reel/Legacy1/')
    with new_client() as c:
        signup(c)
        assert [p['id'] for p in c.get('/api/properties').json()] == ['legacy-p']
    with new_client() as c2:
        signup(c2, email='second@example.com')
        assert c2.get('/api/properties').json() == []


def test_existing_owner_users_become_agents(db_path):
    """A database from before v0.8 (role 'owner', old CHECK) is migrated on start, keeping every row."""
    if storage.pg_enabled():
        with storage.db() as c:
            c.execute('ALTER TABLE users DROP CONSTRAINT users_role_check')
            c.execute("ALTER TABLE users ADD CONSTRAINT users_role_check CHECK (role IN ('owner','broker'))")
    else:
        import sqlite3
        conn = sqlite3.connect(storage.DB_PATH)
        old = conn.execute("SELECT sql FROM sqlite_master WHERE name='users'").fetchone()[0]
        conn.execute('PRAGMA foreign_keys=OFF')
        conn.executescript(f"DROP TABLE users; {old.replace(chr(39) + 'agent' + chr(39), chr(39) + 'owner' + chr(39))};")
        conn.commit()
        conn.close()
    with storage.db() as c:
        c.execute("INSERT INTO users(id,email,name,role,password_hash,created_at) VALUES('u-old','old@example.com',"
                  "'Old Owner','owner',?,?)", (hash_password('old-pass-123'), storage.now()))
        c.execute("INSERT INTO users(id,email,name,role,created_at) VALUES('b-1','b@example.com','Broker B','broker',?)",
                  (storage.now(),))
        c.execute("INSERT INTO properties(id,owner_id,address,created_at) VALUES('p-old','u-old','1 Old St',?)",
                  (storage.now(),))
        c.execute("INSERT INTO property_brokers VALUES('p-old','b-1','view','u-old',?)", (storage.now(),))
    storage.init_db()  # what the app runs on start
    roles = {r['id']: r['role'] for r in storage.get_many('SELECT id, role FROM users')}
    assert roles == {'u-old': 'agent', 'b-1': 'broker'}
    assert storage.get_one("SELECT count(*) AS n FROM property_brokers")['n'] == 1  # nothing cascaded away
    with new_client() as c:  # the migrated agent can log in and use agent-only features
        r = c.post('/api/auth/login', headers=csrf(c), json={'email': 'old@example.com', 'password': 'old-pass-123'})
        assert r.status_code == 200 and r.json()['user']['role'] == 'agent'
        assert [p['id'] for p in c.get('/api/properties').json()] == ['p-old']
        assert c.get('/api/api-keys').status_code == 200 and c.get('/api/brokers').status_code == 200
    with pytest.raises(Exception):  # the new constraint rejects the old role
        with storage.db() as c:
            c.execute("INSERT INTO users(id,email,name,role,created_at) VALUES('x','x@x.co','X','owner',?)", (storage.now(),))
