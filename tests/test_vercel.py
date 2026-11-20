"""Vercel: serverless + read-only files. Settings must not write local files there and must say what is missing."""
import os
import subprocess
import sys
from pathlib import Path

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]
CODE = "from app.core import settings as s; print(s.BASE_URL, s.REFRESH_WORKER)"


def settings_on_vercel(**env):
    base = {"PATH": os.environ["PATH"], "VERCEL": "1", "VERCEL_PROJECT_PRODUCTION_URL": "hub.example.app",
            **env}
    return subprocess.run([sys.executable, "-c", CODE], cwd=ROOT, env=base, capture_output=True, text=True)


def test_vercel_needs_database_url():
    r = settings_on_vercel(DATABASE_URL="", SESSION_SECRET="x", TOKEN_ENCRYPTION_KEY=Fernet.generate_key().decode())
    assert r.returncode != 0 and "Set DATABASE_URL" in r.stderr


def test_vercel_needs_secrets_and_never_writes_dev_keys():
    r = settings_on_vercel(DATABASE_URL="postgresql://u:p@db.example/x", BASE_URL="", SESSION_SECRET="",
                           TOKEN_ENCRYPTION_KEY="")
    assert r.returncode != 0 and "Set SESSION_SECRET" in r.stderr and "Vercel" in r.stderr


def test_vercel_defaults_base_url_and_turns_worker_off():
    r = settings_on_vercel(DATABASE_URL="postgresql://u:p@db.example/x", BASE_URL="", REFRESH_WORKER="",
                           SESSION_SECRET="x", TOKEN_ENCRYPTION_KEY=Fernet.generate_key().decode())
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["https://hub.example.app", "False"]


def test_pyproject_lists_the_same_dependencies_as_requirements():
    """Vercel installs from pyproject.toml, Docker/Render from requirements.txt: both must list the app's packages."""
    import tomllib
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    declared = set(project["dependencies"]) | set(project["optional-dependencies"]["dev"])
    required = {line.strip() for line in (ROOT / "requirements.txt").read_text().splitlines()
                if line.strip() and not line.startswith("#")}
    assert declared == required


def test_client_ip_uses_vercels_forwarded_address(monkeypatch):
    """Behind Vercel (and the frontend's /api rewrite) the socket address is Vercel's: rate limits must use the
    visitor's address from X-Forwarded-For, or every user would share one limit."""
    from types import SimpleNamespace

    from app.core import security

    request = SimpleNamespace(headers={"x-forwarded-for": "203.0.113.7, 10.0.0.1"}, client=SimpleNamespace(host="10.9.9.9"))
    monkeypatch.setattr(security, "ON_VERCEL", True)
    assert security.client_ip(request) == "203.0.113.7"
    monkeypatch.setattr(security, "ON_VERCEL", False)  # elsewhere uvicorn --proxy-headers already set request.client
    assert security.client_ip(request) == "10.9.9.9"
