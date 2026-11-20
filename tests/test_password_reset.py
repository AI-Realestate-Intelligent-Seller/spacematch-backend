"""Forgot / reset password, and one account per email."""
import re

import pytest

from app.api import auth
from test_app import csrf, db_path, new_client, signup  # noqa: F401

EMAIL, OLD, NEW = 'owner@example.com', 'correct-horse-1', 'brand-new-pass-2'


@pytest.fixture()
def mails(db_path, monkeypatch):
    sent = []
    monkeypatch.setattr(auth, 'send_email', lambda to, subject, text: sent.append((to, subject, text)) or True)
    auth.reset_limiter._hits.clear()  # limits are per process
    with new_client() as c:
        signup(c, email=EMAIL, password=OLD)
    return sent


def token_from(mail):
    return re.search(r'/reset-password/([\w-]+)', mail[2]).group(1)


def request_reset(c, email=EMAIL):
    return c.post('/api/auth/forgot-password', headers=csrf(c), json={'email': email})


def test_full_reset_flow(mails):
    with new_client() as c:
        r = request_reset(c, '  Owner@Example.com ')
        assert r.status_code == 200 and 'If an account exists' in r.json()['message']
        assert len(mails) == 1 and mails[0][0] == EMAIL and '1 hour' in mails[0][2]
        token = token_from(mails[0])
        assert c.get(f'/api/password-resets/{token}').json() == {'email': EMAIL}
        done = c.post(f'/api/password-resets/{token}', headers=csrf(c), json={'password': NEW})
        assert done.status_code == 200 and done.json()['user']['email'] == EMAIL
        assert c.get('/api/me').json()['user']['email'] == EMAIL  # logged in
    with new_client() as c:
        assert c.post('/api/auth/login', headers=csrf(c), json={'email': EMAIL, 'password': OLD}).status_code == 401
        assert c.post('/api/auth/login', headers=csrf(c), json={'email': EMAIL, 'password': NEW}).status_code == 200


def test_new_password_must_differ_from_old(mails):
    with new_client() as c:
        request_reset(c)
        token = token_from(mails[0])
        same = c.post(f'/api/password-resets/{token}', headers=csrf(c), json={'password': OLD})
        assert same.status_code == 422 and 'different from your current password' in same.json()['detail']
        assert c.get(f'/api/password-resets/{token}').status_code == 200  # link still usable
        assert c.post(f'/api/password-resets/{token}', headers=csrf(c), json={'password': NEW}).status_code == 200


def test_link_is_single_use_and_newest_only(mails):
    with new_client() as c:
        request_reset(c)
        request_reset(c)
        first, second = token_from(mails[0]), token_from(mails[1])
        assert c.get(f'/api/password-resets/{first}').status_code == 404  # replaced by the newer link
        assert c.post(f'/api/password-resets/{second}', headers=csrf(c), json={'password': NEW}).status_code == 200
        assert c.post(f'/api/password-resets/{second}', headers=csrf(c), json={'password': 'third-pass-3'}).status_code == 404


def test_expired_link(mails):
    with new_client() as c:
        request_reset(c)
        with auth.db() as db:
            db.execute('UPDATE password_resets SET expires_at=1')
        assert c.get(f'/api/password-resets/{token_from(mails[0])}').status_code == 404


def test_reset_logs_out_other_sessions(mails):
    with new_client() as other:
        assert other.post('/api/auth/login', headers=csrf(other), json={'email': EMAIL, 'password': OLD}).status_code == 200
        assert other.get('/api/me').json()['user']
        with new_client() as c:
            request_reset(c)
            c.post(f'/api/password-resets/{token_from(mails[0])}', headers=csrf(c), json={'password': NEW})
        assert other.get('/api/me').json()['user'] is None
        assert other.get('/api/properties').status_code == 401


def test_unknown_email_gets_same_answer_and_no_mail(mails):
    with new_client() as c:
        r = request_reset(c, 'nobody@example.com')
        assert r.status_code == 200 and r.json() == auth.RESET_SENT and mails == []


def test_reset_requests_are_rate_limited(mails):
    with new_client() as c:
        codes = [request_reset(c).status_code for _ in range(6)]
    assert codes[:5] == [200] * 5 and codes[5] == 429


def test_reset_needs_csrf_and_a_valid_password(mails):
    with new_client() as c:
        assert c.post('/api/auth/forgot-password', json={'email': EMAIL}).status_code == 403
        request_reset(c)
        assert c.post(f'/api/password-resets/{token_from(mails[0])}', headers=csrf(c),
                      json={'password': 'short'}).status_code == 422


def test_one_account_per_email(db_path):
    with new_client() as c:
        signup(c, email=EMAIL)
    with new_client() as c:
        for email in (EMAIL, 'OWNER@example.com', '  owner@EXAMPLE.com '):
            r = c.post('/api/auth/signup', headers=csrf(c), json={'name': 'Again', 'email': email, 'password': 'other-pass-9'})
            assert r.status_code == 409 and 'already exists' in r.json()['detail']
