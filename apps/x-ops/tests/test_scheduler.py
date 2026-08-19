from datetime import UTC, datetime

from x_ops.scheduler import TaskScheduler, _cron_matches


class ScheduledBackend:
    def __init__(self):
        self.calls = []
        self.updated = []

    async def list_tasks(self, _filters):
        return [
            {
                "id": "task-1",
                "enabled": True,
                "schedule": {"type": "cron", "cron": "30 9 * * *", "enabled": True},
            }
        ]

    async def trigger_task(self, task_id, trigger, *, fire_key=None):
        self.calls.append((task_id, trigger, fire_key))
        return {
            "runs": [{"id": "ordinary-task-run", "status": "queued"}],
            "duplicate": False,
        }

    async def update_task(self, task_id, payload):
        self.updated.append((task_id, payload))
        return {"id": task_id, **payload}


async def test_scheduler_only_uses_normal_task_trigger_and_deduplicates_minute():
    backend = ScheduledBackend()
    scheduler = TaskScheduler(backend)
    now = datetime(2026, 8, 20, 9, 30, tzinfo=UTC)
    assert await scheduler.poll_once(now) == ["task-1"]
    assert await scheduler.poll_once(now) == []
    assert backend.calls == [("task-1", "schedule", "cron:2026-08-20T09:30")]
    assert backend.updated[0][1]["schedule"]["last_fire_key"].startswith("cron:")


def test_small_cron_matcher():
    now = datetime(2026, 8, 20, 9, 30, tzinfo=UTC)
    assert _cron_matches("30 9 * * *", now)
    assert _cron_matches("0-30 9 20 8 4", now)
    assert not _cron_matches("31 9 * * *", now)
    assert not _cron_matches("5-3,30 9 * * *", now)
    assert not _cron_matches("invalid", now)


async def test_scheduler_does_not_advance_json_fire_key_when_trigger_fails():
    class FlakyBackend(ScheduledBackend):
        def __init__(self):
            super().__init__()
            self.fail = True

        async def trigger_task(self, task_id, trigger, *, fire_key=None):
            if self.fail:
                self.fail = False
                raise RuntimeError("database unavailable")
            return await super().trigger_task(task_id, trigger, fire_key=fire_key)

    backend = FlakyBackend()
    scheduler = TaskScheduler(backend)
    now = datetime(2026, 8, 20, 9, 30, tzinfo=UTC)

    assert await scheduler.poll_once(now) == []
    assert backend.updated == []
    assert await scheduler.poll_once(now) == ["task-1"]
    assert backend.updated[0][1]["schedule"]["last_fire_key"] == "cron:2026-08-20T09:30"
