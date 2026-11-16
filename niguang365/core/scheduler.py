"""周收束与 123 上传的定时补跑。"""

from __future__ import annotations

import threading
import time
from datetime import datetime, time as dtime, timedelta
from typing import Any

from .domain import WEEK_STATUS_CLEANUP_PENDING, WEEK_STATUS_CLOSED, WEEK_STATUS_COLLECTING, WEEK_STATUS_UPLOAD_BLOCKED, WEEK_STATUS_UPLOAD_FAILED, WEEK_STATUS_UPLOADED
from .repo import WeekRecord
from .service import Service


class UploadScheduler:
    def __init__(self, service: Service, *, cooldown_seconds: int = 600):
        self.service = service
        self.cooldown_seconds = cooldown_seconds
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def scheduled_at(self, week: WeekRecord, now: datetime) -> datetime:
        target_weekday = int(self.service.setting("upload_weekday", 0))
        hour = int(self.service.setting("upload_hour", 20))
        minute = int(self.service.setting("upload_minute", 0))
        day = week.end_date + timedelta(days=1)
        for _ in range(7):
            if day.weekday() == target_weekday:
                break
            day += timedelta(days=1)
        return datetime.combine(day, dtime(hour, minute), tzinfo=now.tzinfo)

    def _recent_failed(self, week_id: str, now: datetime) -> bool:
        latest = self.service.repo.latest_upload_task(week_id)
        if not latest or latest["status"] not in {"failed", "uploaded_with_cleanup_pending"}:
            return False
        try:
            created = datetime.fromisoformat(latest["created_at"])
        except ValueError:
            return False
        if created.tzinfo is None:
            created = created.replace(tzinfo=now.tzinfo)
        return (now - created).total_seconds() < self.cooldown_seconds

    def tick(self, now: datetime | None = None, *, background: bool = True) -> list[dict[str, Any]]:
        now = now or self.service.now()
        current_start = now.date() - timedelta(days=now.date().weekday())
        actions: list[dict[str, Any]] = []
        for week in self.service.repo.due_weeks(current_start):
            if now < self.scheduled_at(week, now):
                continue
            if week.status in {WEEK_STATUS_COLLECTING, WEEK_STATUS_UPLOAD_BLOCKED}:
                result = self.service.close_week(week.id)
                actions.append({"week": week.id, "action": "close", "result": result.get("ok")})
                if not result.get("ok"):
                    continue
                week = self.service.repo.get_week(week.id)
                if week is None:
                    continue
            if week.status not in {WEEK_STATUS_CLOSED, WEEK_STATUS_UPLOAD_FAILED, WEEK_STATUS_CLEANUP_PENDING, WEEK_STATUS_UPLOADED}:
                continue
            latest = self.service.repo.latest_upload_task(week.id)
            if latest and latest["status"] in {"queued", "running"}:
                continue
            if week.status == WEEK_STATUS_UPLOADED and not week.needs_sync:
                continue
            if self._recent_failed(week.id, now):
                continue
            if week.selected_unpublished:
                continue
            try:
                task = self.service.queue_upload(week.id, allow_current=True, background=background)
                actions.append({"week": week.id, "action": "upload", "task": task["id"], "status": task["status"]})
            except Exception as exc:  # noqa: BLE001
                actions.append({"week": week.id, "action": "upload_error", "error": str(exc)})
        return actions

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception:
                    pass
                self._stop.wait(30)

        self._thread = threading.Thread(target=loop, name="niguang365-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
