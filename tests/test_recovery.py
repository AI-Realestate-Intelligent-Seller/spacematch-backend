"""Recovery email (Account page), changing the login email, and recovering an account with the recovery email."""
import re

import pytest

from app.api import auth, recovery
from test_app import csrf, db_path, new_client, signup  # noqa: F401

EMAIL, PASSWORD, NEW = 'owner@example.com', 'correct-horse-1', 'brand-new-pass-2'
BACKUP = 'olivia.personal@gmail.com'


@pytest.fixture()
def mails(db_path, monkeypatch):
    sent = []
    monkeypatch.setattr(recovery, 'send_email', lambda to, subject, text: sent.append((to, subject, text)) or True)
    for limiter in (recovery.recover_limiter, recovery.code_limiter, recovery.password_limiter, auth.login_limiter):
        limiter._hits.clear()
    return sent


def code_in(mail):
    return re.search(r'^\s+(\d{6})$', mail[2], re.M).group(1)


def allow_resend():
    with recovery.db() as c:
        c.execute('UPDATE email_codes SET sent_at=sent_at-120')


def add_backup(c, mails, email=BACKUP):
    r = c.post('/api/account/recovery-email', headers=csrf(c), json={'email': email, 'password': PASSWORD})
    assert r.status_code == 200, r.text
    assert r.json()['pending_recovery_email'] == email and r.json()['recovery_email'] is None
    r = c.post('/api/account/recovery-email/verify', headers=csrf(c), json={'code': code_in(mails[-1])})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture()
def owner(mails):
    """A logged-in agent with a verified recovery email; `mails` emptied afterwards."""
    with new_client() as c:
        signup(c, email=EMAIL, password=PASSWORD)
        add_backup(c, mails)
        mails.clear()
        yield c


# ------------------------------------------------------------ Account page ----

def test_add_and_verify_recovery_email(mails):
    with new_client() as c:
        signup(c, email=EMAIL, password=PASSWORD)
        assert c.get('/api/account').json()['recovery_email'] is None
        account = add_backup(c, mails)
        assert account['recovery_email'] == BACKUP and account['recovery_email_verified_at']
        assert account['pending_recovery_email'] is None
    to = [m[0] for m in mails]
    assert to == [BACKUP, EMAIL]  # the code, then an alert to the login email
    assert 'o•••@g•••.com' in mails[1][2] and BACKUP not in mails[1][2]


def test_recovery_email_rules(mails):
    with new_client() as c:
        signup(c, email=EMAIL, password=PASSWORD)
        post = lambda body: c.post('/api/account/recovery-email', headers=csrf(c), json=body)
        assert post({'email': BACKUP, 'password': 'wrong-password'}).status_code == 400
        assert post({'email': ' Owner@Example.com ', 'password': PASSWORD}).status_code == 422  # same as login
        assert post({'email': 'not-an-email', 'password': PASSWORD}).status_code == 422
        r = post({'email': 'olivia@example.com', 'password': PASSWORD})
        assert r.status_code == 200 and r.json()['same_domain'] is True
        assert post({'email': BACKUP, 'password': PASSWORD}).status_code == 429  # one code a minute
        assert c.post('/api/account/recovery-email').status_code == 403  # CSRF
    with new_client() as anonymous:
        assert anonymous.get('/api/account').status_code == 401


def test_wrong_codes_are_limited(mails):
    with new_client() as c:
        signup(c, email=EMAIL, password=PASSWORD)
        c.post('/api/account/recovery-email', headers=csrf(c), json={'email': BACKUP, 'password': PASSWORD})
        good = code_in(mails[-1])
        bad = '000000' if good != '000000' else '111111'
        verify = lambda code: c.post('/api/account/recovery-email/verify', headers=csrf(c), json={'code': code})
        details = [verify(bad).json()['detail'] for _ in range(5)]
        assert '4 tries left' in details[0] and '1 try left' in details[3] and 'Too many wrong codes' in details[4]
        assert verify(good).status_code == 400  # the code is dead after 5 wrong tries
        assert c.get('/api/account').json()['recovery_email'] is None


def test_expired_code_and_resend(mails):
    with new_client() as c:
        signup(c, email=EMAIL, password=PASSWORD)
        c.post('/api/account/recovery-email', headers=csrf(c), json={'email': BACKUP, 'password': PASSWORD})
        first = code_in(mails[-1])
        assert c.post('/api/account/recovery-email/resend', headers=csrf(c)).status_code == 429
        allow_resend()
        assert c.post('/api/account/recovery-email/resend', headers=csrf(c)).status_code == 200
        second = code_in(mails[-1])
        verify = lambda code: c.post('/api/account/recovery-email/verify', headers=csrf(c), json={'code': code})
        if first != second:
            assert verify(first).status_code == 400  # only the newest code works
        with recovery.db() as db:
            db.execute('UPDATE email_codes SET expires_at=1')
        r = verify(second)
        assert r.status_code == 400 and 'expired' in r.json()['detail']


def test_change_keeps_old_until_verified_then_alerts_both(owner, mails):
    c = owner
    other = 'olivia.new@outlook.com'
    allow_resend()
    c.post('/api/account/recovery-email', headers=csrf(c), json={'email': other, 'password': PASSWORD})
    account = c.get('/api/account').json()
    assert account['recovery_email'] == BACKUP and account['pending_recovery_email'] == other
    c.post('/api/account/recovery-email/verify', headers=csrf(c), json={'code': code_in(mails[-1])})
    assert c.get('/api/account').json()['recovery_email'] == other
    assert {m[0] for m in mails[1:]} == {EMAIL, BACKUP}


def test_cancel_and_remove(owner, mails):
    c = owner
    allow_resend()
    r = c.post('/api/account/recovery-email', headers=csrf(c), json={'email': 'x@outlook.com', 'password': PASSWORD})
    assert r.json()['pending_recovery_email'] == 'x@outlook.com'
    assert c.post('/api/account/recovery-email/cancel', headers=csrf(c)).json()['pending_recovery_email'] is None
    remove = lambda pw: c.post('/api/account/recovery-email/remove', headers=csrf(c), json={'password': pw})
    assert remove('wrong-password').status_code == 400
    assert remove(PASSWORD).json()['recovery_email'] is None
    assert {m[0] for m in mails[-2:]} == {EMAIL, BACKUP}
    assert remove(PASSWORD).status_code == 404


def test_change_login_email(owner, mails):
    c = owner
    new_login = 'olivia@newco.com'
    with new_client() as other:
        signup(other, email='taken@example.com')
    start = lambda email: c.post('/api/account/email', headers=csrf(c), json={'email': email, 'password': PASSWORD})
    assert start('taken@example.com').status_code == 409
    assert start(BACKUP).status_code == 422  # that's the recovery email
    assert start(new_login).json()['pending_login_email'] == new_login
    assert mails[-1][0] == new_login
    r = c.post('/api/account/email/verify', headers=csrf(c), json={'code': code_in(mails[-1])})
    assert r.status_code == 200 and r.json()['user']['email'] == new_login
    assert mails[-1][0] == EMAIL and 'changed' in mails[-1][1]  # alert to the old address
    with new_client() as fresh:
        login = lambda email: fresh.post('/api/auth/login', headers=csrf(fresh), json={'email': email, 'password': PASSWORD})
        assert login(EMAIL).status_code == 401
        assert login(new_login).status_code == 200


# --------------------------------------------------------- Recover account ----

def recover(c, email=EMAIL, backup=BACKUP):
    return c.post('/api/auth/recover', headers=csrf(c), json={'email': email, 'recovery_email': backup})


def test_full_recovery(owner, mails):
    with new_client() as c:
        r = recover(c, ' Owner@Example.com ', ' Olivia.Personal@Gmail.com ')
        assert r.json() == recovery.RECOVER_SENT
        assert [m[0] for m in mails] == [BACKUP]
        assert c.post('/api/auth/recover/verify', headers=csrf(c),
                      json={'email': EMAIL, 'code': code_in(mails[0])}).status_code == 200
        r = c.post('/api/auth/recover/password', headers=csrf(c), json={'password': NEW})
        assert r.status_code == 200 and r.json()['user']['email'] == EMAIL
        assert c.get('/api/me').json()['user']['email'] == EMAIL  # logged in here
    assert owner.get('/api/me').json()['user'] is None  # the old session was logged out
    assert {m[0] for m in mails[1:]} == {EMAIL, BACKUP} and all('recovered' in m[1] for m in mails[1:])
    with new_client() as c:
        login = lambda pw: c.post('/api/auth/login', headers=csrf(c), json={'email': EMAIL, 'password': pw})
        assert login(PASSWORD).status_code == 401
        assert login(NEW).status_code == 200


@pytest.mark.parametrize('email,backup', [
    (EMAIL, 'someone.else@gmail.com'),        # wrong recovery email
    ('nobody@example.com', BACKUP),           # unknown account
])
def test_recovery_answers_the_same_and_sends_nothing(owner, mails, email, backup):
    with new_client() as c:
        assert recover(c, email, backup).json() == recovery.RECOVER_SENT
    assert mails == []


def test_unverified_recovery_email_cannot_recover(mails):
    with new_client() as c:
        signup(c, email=EMAIL, password=PASSWORD)
        c.post('/api/account/recovery-email', headers=csrf(c), json={'email': BACKUP, 'password': PASSWORD})
    mails.clear()
    with new_client() as c:
        assert recover(c).json() == recovery.RECOVER_SENT
    assert mails == []


def test_recovery_code_errors_do_not_reveal_anything(owner, mails):
    with new_client() as c:
        recover(c)
        good = code_in(mails[0])
        bad = '000000' if good != '000000' else '111111'
        verify = lambda email, code: c.post('/api/auth/recover/verify', headers=csrf(c), json={'email': email, 'code': code})
        matched, unknown = verify(EMAIL, bad), verify('nobody@example.com', bad)
        assert matched.status_code == unknown.status_code == 400
        assert matched.json()['detail'] == unknown.json()['detail'] == recovery.BAD_CODE


def test_new_password_needs_a_verified_code_and_must_differ(owner, mails):
    with new_client() as c:
        set_pw = lambda pw: c.post('/api/auth/recover/password', headers=csrf(c), json={'password': pw})
        assert set_pw(NEW).status_code == 400  # no code entered yet
        recover(c)
        c.post('/api/auth/recover/verify', headers=csrf(c), json={'email': EMAIL, 'code': code_in(mails[0])})
        assert set_pw(PASSWORD).status_code == 422  # same as the current password
        assert set_pw(NEW).status_code == 200
        assert c.post('/api/auth/recover/verify', headers=csrf(c),
                      json={'email': EMAIL, 'code': code_in(mails[0])}).status_code == 400  # single use


def test_recovery_requests_are_rate_limited(owner, mails):
    with new_client() as c:
        codes = [recover(c, EMAIL, f'guess{i}@gmail.com').status_code for i in range(6)]
    assert codes[:5] == [200] * 5 and codes[5] == 429


def test_failed_logins_are_counted_for_the_recover_link(owner):
    with new_client() as c:
        tries = [c.post('/api/auth/login', headers=csrf(c), json={'email': EMAIL, 'password': 'nope-nope'})
                 for _ in range(5)]
    assert [t.headers['X-Failed-Attempts'] for t in tries] == ['1', '2', '3', '4', '5']


def test_mask_email():
    assert recovery.mask_email('olivia.personal@gmail.com') == 'o•••@g•••.com'
    assert recovery.mask_email('a@b.co.uk') == 'a•••@b•••.uk'
