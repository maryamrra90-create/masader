"""Celery application: queues, time limits, reliability settings and the beat schedule (architecture
B.3.4, H.2, IF-14). Both worker types use the prefork pool, where soft and hard time limits work."""
from __future__ import annotations

from celery import Celery
from celery.signals import worker_process_init

from ..settings import settings

app = Celery("masader", broker=settings.redis_url, include=["masader.pipeline.tasks"])
app.conf.update(
    task_acks_late=True,                 # ack after the stage finishes, so a crash redelivers it
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=100,      # routine recycling; a child with a stuck call thread exits at once
    broker_transport_options={"visibility_timeout": settings.visibility_timeout_s},
    broker_connection_retry_on_startup=True,
    task_default_queue="cpu",
    task_routes={
        "masader.stage.extract": {"queue": "llm"}, "masader.stage.verify": {"queue": "llm"},
        "masader.stage.write": {"queue": "llm"}, "masader.stage.validate": {"queue": "llm"},
        "masader.stage.*": {"queue": "cpu"}, "masader.ops.*": {"queue": "cpu"}, "masader.ingest.*": {"queue": "ingest"},
    },
    task_soft_time_limit=settings.stage_soft_limit_s,
    task_time_limit=settings.stage_hard_limit_s,
    task_ignore_result=True,
    worker_send_task_events=False,
    timezone="Asia/Dubai",
    beat_schedule={"reaper": {"task": "masader.ops.reaper", "schedule": settings.reaper_s}},
)
if settings.live_sources:                 # live connectors only when outbound access to the sources exists
    app.conf.beat_schedule.update({
        "ingest-rss": {"task": "masader.ingest.rss", "schedule": 1800.0},
        "ingest-gdelt": {"task": "masader.ingest.gdelt", "schedule": 900.0},
        "ingest-worldbank": {"task": "masader.ingest.worldbank", "schedule": 86400.0},
    })


@worker_process_init.connect
def _fresh_pool(**_):
    """A forked child must not reuse the parent's database connections."""
    from ..store.db import reset_pool

    reset_pool()
