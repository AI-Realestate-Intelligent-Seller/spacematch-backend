"""One-shot daily refresh for cron / Task Scheduler (the server also runs this on its own schedule).

Run: python scripts/sync_all.py
Queues a `daily` refresh for every active property with links, then reads them all now.
Every attempt is written to the read logs; every good read is saved as a snapshot.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # run from anywhere: python scripts/<name>.py

from app.services import refresh as rf
def run_daily():
    queued = rf.enqueue_daily(force=True)
    done = rf.run_queue()
    print(f"Daily refresh: {queued} properties queued, {done} refreshes processed.")


if __name__ == '__main__':
    run_daily()
