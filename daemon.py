"""
24/7 Autonomous Background Scheduler Daemon for GenX71 AutoVJ.
Reads schedule configuration from SQLite, injects randomized Gaussian time jitter
to prevent bot-detection patterns, and executes the full generation & posting pipeline.
"""

import time
import random
import signal
import logging
from datetime import datetime, timedelta
from typing import Optional
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger

from core.db import init_db, get_setting
from core.charts import seed_charts_if_empty
from core.pipeline import generate_post_video

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("genx71.daemon")


class AutoVJDaemon:
    def __init__(self, db_path: str = None):
        self.db_path = db_path
        self.scheduler = BackgroundScheduler()
        self.running = False
        self.next_run_info = None

    def _compute_next_jittered_run(self) -> Optional[datetime]:
        enabled = get_setting("schedule_enabled", "true", db_path=self.db_path) == "true"
        if not enabled:
            return None

        raw_times = get_setting("daily_post_times", "12:30,19:15", db_path=self.db_path) or "12:30,19:15"
        jitter_mins = int(get_setting("jitter_minutes", "30", db_path=self.db_path) or "30")

        now = datetime.now()
        candidates = []

        for time_str in raw_times.split(","):
            time_str = time_str.strip()
            if ":" not in time_str:
                continue
            try:
                hh, mm = map(int, time_str.split(":"))
            except ValueError:
                continue

            for day_offset in (0, 1):
                base_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0) + timedelta(days=day_offset)
                # Gaussian jitter clipped to [-jitter_mins, +jitter_mins]
                if jitter_mins > 0:
                    offset = random.gauss(0, jitter_mins / 2.2)
                    offset = max(-jitter_mins, min(jitter_mins, offset))
                    jittered_dt = base_dt + timedelta(minutes=offset, seconds=random.randint(0, 59))
                else:
                    jittered_dt = base_dt

                if jittered_dt > now + timedelta(seconds=30):
                    candidates.append(jittered_dt)

        return min(candidates) if candidates else None

    def _run_scheduled_job(self):
        logger.info("Triggered scheduled GenX71 AutoVJ generation & post job.")
        try:
            post = generate_post_video(publish=True, db_path=self.db_path)
            logger.info(f"Scheduled job finished: {post['artist']} - {post['title']} (status={post['status']})")
        except Exception as e:
            logger.error(f"Scheduled job failed: {e}")
        finally:
            self.reschedule_next()

    def reschedule_next(self):
        # Remove existing scheduled job if any
        if self.scheduler.get_job("next_autovj_post"):
            self.scheduler.remove_job("next_autovj_post")

        next_dt = self._compute_next_jittered_run()
        if next_dt:
            self.next_run_info = next_dt.isoformat()
            logger.info(f"Next jittered post scheduled for: {self.next_run_info}")
            self.scheduler.add_job(
                self._run_scheduled_job,
                trigger=DateTrigger(run_date=next_dt),
                id="next_autovj_post",
                replace_existing=True
            )
        else:
            self.next_run_info = None
            logger.info("Automated scheduling is disabled.")

    def start(self):
        init_db(self.db_path)
        seed_charts_if_empty(self.db_path)
        self.scheduler.start()
        self.running = True
        self.reschedule_next()
        logger.info("GenX71 AutoVJ Daemon started.")

    def stop(self):
        self.running = False
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        logger.info("GenX71 AutoVJ Daemon stopped.")


if __name__ == "__main__":
    daemon = AutoVJDaemon()
    daemon.start()

    def _sig_handler(sig, frame):
        daemon.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    while True:
        time.sleep(10)
