"""
24/7 Autonomous Background Scheduler Daemon for GenX71 AutoVJ.
Reads schedule configuration from SQLite, injects randomized Gaussian time jitter
to prevent bot-detection patterns, and executes the full generation & posting pipeline.
"""

import os
import time
import random
import signal
import logging
import threading
from datetime import datetime, timedelta
from typing import Optional
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger

from core.db import (
    init_db, get_setting, set_setting, pop_next_queued_post,
    update_post_status, claim_post_for_publish,
)
from core.charts import seed_charts_if_empty
from core.pipeline import generate_post_video
from core.tiktok_poster import check_cookie_health, upload_to_tiktok, build_caption

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
        self.next_analytics_run_info = None
        self._job_lock = threading.Lock()
        self._current_slot_base: Optional[datetime] = None

    def _compute_next_jittered_run(self, now: Optional[datetime] = None) -> Optional[datetime]:
        enabled = get_setting("schedule_enabled", "true", db_path=self.db_path) == "true"
        if not enabled:
            self._current_slot_base = None
            return None

        raw_times = get_setting("daily_post_times", "12:30,19:15", db_path=self.db_path) or "12:30,19:15"
        jitter_mins = int(get_setting("jitter_minutes", "30", db_path=self.db_path) or "30")

        now = now or datetime.now()
        last_slot_dt: Optional[datetime] = None
        last_slot_raw = (get_setting("last_scheduled_slot", "", db_path=self.db_path) or "").strip()
        if last_slot_raw:
            try:
                last_slot_dt = datetime.fromisoformat(last_slot_raw)
            except ValueError:
                last_slot_dt = None

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
                # Never re-fire a time slot that was already executed (e.g. after a negative-jitter run)
                if last_slot_dt is not None and base_dt <= last_slot_dt:
                    continue

                # Gaussian jitter clipped to [-jitter_mins, +jitter_mins]
                if jitter_mins > 0:
                    offset = random.gauss(0, jitter_mins / 2.2)
                    offset = max(-jitter_mins, min(jitter_mins, offset))
                    jittered_dt = base_dt + timedelta(minutes=offset, seconds=random.randint(0, 59))
                else:
                    jittered_dt = base_dt

                if jittered_dt > now + timedelta(seconds=30):
                    candidates.append((jittered_dt, base_dt))

        if not candidates:
            self._current_slot_base = None
            return None

        chosen_jittered, chosen_base = min(candidates, key=lambda pair: pair[0])
        self._current_slot_base = chosen_base
        return chosen_jittered

    def _compute_next_analytics_run(self) -> Optional[datetime]:
        enabled = get_setting("analytics_report_enabled", "true", db_path=self.db_path) == "true"
        if not enabled:
            return None

        raw_time = (get_setting("analytics_report_time", "02:00", db_path=self.db_path) or "02:00").strip()
        try:
            hh, mm = map(int, raw_time.split(":"))
            if not (0 <= hh <= 23 and 0 <= mm <= 59):
                raise ValueError("Out of range")
        except Exception:
            hh, mm = 2, 0

        now = datetime.now()
        candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= now + timedelta(seconds=30):
            candidate = candidate + timedelta(days=1)
        return candidate

    def _run_analytics_job(self):
        logger.info("Triggered scheduled GenX71 post analytics LLM report.")
        try:
            from core.analytics import run_analytics_report
            res = run_analytics_report(period="daily", db_path=self.db_path)
            rep = res.get("report") or {}
            logger.info(
                "Daily analytics report #%s finished (model=%s, posts=%s, status=%s)",
                rep.get("id"), rep.get("model_used"), rep.get("posts_analyzed"), rep.get("status"),
            )

            now = datetime.now()
            if get_setting("analytics_weekly_enabled", "true", db_path=self.db_path) == "true" and now.weekday() == 6:
                logger.info("Running scheduled weekly (Sunday) analytics report & AI self-evaluation...")
                w_res = run_analytics_report(period="weekly", sync_first=False, db_path=self.db_path)
                w_rep = w_res.get("report") or {}
                logger.info(
                    "Weekly analytics report #%s finished (model=%s, posts=%s)",
                    w_rep.get("id"), w_rep.get("model_used"), w_rep.get("posts_analyzed"),
                )

            if get_setting("analytics_monthly_enabled", "true", db_path=self.db_path) == "true" and now.day == 1:
                logger.info("Running scheduled monthly (1st of month) analytics report & AI self-evaluation...")
                m_res = run_analytics_report(period="monthly", sync_first=False, db_path=self.db_path)
                m_rep = m_res.get("report") or {}
                logger.info(
                    "Monthly analytics report #%s finished (model=%s, posts=%s)",
                    m_rep.get("id"), m_rep.get("model_used"), m_rep.get("posts_analyzed"),
                )
        except Exception as exc:
            logger.error("Scheduled analytics report failed: %s", exc)
        finally:
            self.reschedule_analytics()

    def reschedule_analytics(self):
        if self.scheduler.get_job("daily_analytics_report"):
            self.scheduler.remove_job("daily_analytics_report")

        next_dt = self._compute_next_analytics_run()
        if next_dt:
            self.next_analytics_run_info = next_dt.isoformat()
            logger.info("Next daily AI analytics report scheduled for: %s", self.next_analytics_run_info)
            self.scheduler.add_job(
                self._run_analytics_job,
                trigger=DateTrigger(run_date=next_dt),
                id="daily_analytics_report",
                replace_existing=True,
            )
        else:
            self.next_analytics_run_info = None
            logger.info("Daily AI analytics reporting is disabled.")

    def _run_scheduled_job(self):
        if not self._job_lock.acquire(blocking=False):
            logger.warning("Scheduled post job already in progress; skipping overlapping trigger.")
            return

        logger.info("Triggered scheduled GenX71 AutoVJ generation & post job.")
        slot_base = self._current_slot_base or datetime.now()
        try:
            set_setting("last_scheduled_slot", slot_base.isoformat(), db_path=self.db_path)
        except Exception as exc:
            logger.warning("Could not persist last_scheduled_slot: %s", exc)

        try:
            queued = pop_next_queued_post(db_path=self.db_path)
            while queued and (not queued.get("video_path") or not os.path.exists(queued["video_path"])):
                logger.warning("Queued post #%s video missing on disk; marking failed and checking next.", queued["id"])
                update_post_status(queued["id"], "failed", error_message="Video file missing on disk.", db_path=self.db_path)
                queued = pop_next_queued_post(db_path=self.db_path)

            if queued:
                logger.info("Publishing queued post #%s: %s - %s", queued["id"], queued["artist"], queued["title"])
                health = check_cookie_health(db_path=self.db_path)
                if not health["healthy"]:
                    logger.warning("Skipped queued post #%s: %s", queued["id"], health["reason"])
                else:
                    claimed = claim_post_for_publish(queued["id"], allow_republish=False, db_path=self.db_path)
                    if not claimed:
                        logger.warning(
                            "Queued post #%s is already publishing or published; skipping duplicate upload.",
                            queued["id"],
                        )
                    else:
                        auto_pub = get_setting("auto_publish", "false", db_path=self.db_path) == "true"
                        caption = build_caption(
                            queued["artist"], queued["title"], queued["year"], queued.get("chart_info", "")
                        )
                        try:
                            res = upload_to_tiktok(
                                queued["video_path"], caption, auto_publish=auto_pub, db_path=self.db_path
                            )
                            update_post_status(queued["id"], res["status"], db_path=self.db_path)
                            logger.info(
                                "Scheduled queued post #%s finished: %s - %s (status=%s)",
                                queued["id"], queued["artist"], queued["title"], res["status"]
                            )
                        except Exception as exc:
                            logger.error("Scheduled queued post #%s upload failed: %s", queued["id"], exc)
                            update_post_status(queued["id"], "failed", error_message=str(exc), db_path=self.db_path)
            else:
                post = generate_post_video(publish=True, db_path=self.db_path)
                logger.info(f"Scheduled job finished: {post['artist']} - {post['title']} (status={post['status']})")
        except Exception as e:
            logger.error(f"Scheduled job failed: {e}")
        finally:
            self._job_lock.release()
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

        self.reschedule_analytics()

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
