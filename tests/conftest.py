import os
import sys
from pathlib import Path

# Tests never start the background refresh worker or the daily scheduler.
os.environ.setdefault("REFRESH_WORKER", "off")

# Tests never use the real database from .env:
#   default                    -> a temporary SQLite file per test
#   TEST_DATABASE_URL=postgres -> that (throwaway!) Postgres; its public schema is wiped before every test
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", "")
os.environ.setdefault("DATABASE_SSL", "false")
os.environ["APIFY_TOKEN"] = ""  # tests mock Apify; never spend real credit
os.environ["YOUTUBE_API_KEY"] = ""  # tests mock the YouTube Data API
# Importing the app runs init_db() once, before any fixture: point it at a throwaway file, never data/.
import tempfile  # noqa: E402

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="spacematch-tests-"), "import.sqlite")

sys.path.insert(0, str(Path(__file__).resolve().parent))
