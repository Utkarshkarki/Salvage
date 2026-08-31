"""Celery application + broker config.

Broker: Upstash Redis over TLS. The URL scheme MUST be ``rediss://`` (Upstash
rejects plain ``redis://``). The default for this build is eager mode
(``RECLAIM_CELERY_EAGER=1``) so tests and the batch run never need a live
broker: scheduled retries execute synchronously with their eta applied.
"""

from __future__ import annotations

from typing import Any

from celery import Celery
from celery.app.base import Celery as CeleryApp
from celery.schedules import crontab

from .config import Settings, get_settings


def build_app(settings: Settings | None = None) -> CeleryApp:
    s = settings or get_settings()

    broker = s.redis_url
    backend = s.redis_url

    celery = Celery("reclaim", broker=broker, backend=backend)

    celery.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        broker_connection_retry_on_startup=True,
        task_always_eager=s.reclaim_celery_eager,
        task_eager_propagates=True,
        task_acks_late=True,
        worker_prefetch_multiplier=1,
        # Periodic reconciliation: the stale-lock sweep runs every 5 minutes so
        # a case stuck in ACTING is recovered to human review even if the worker
        # that owned it died mid-pipeline. (The task itself is idempotent.)
        beat_schedule={
            "reclaim-sweep-stale-acting": {
                "task": "reclaim.tasks.sweep_stale_acting_task",
                "schedule": crontab(minute="*/5"),
            },
        },
        # Upstash + Celery TLS: explicit ssl dicts (CERT_NONE via None) cover
        # the rediss:// scheme. Confirm against current Upstash/Celery docs
        # when you run a real worker.
        broker_use_ssl=_tls_opts(s) if s.redis_tls else None,
        redis_backend_use_ssl=_tls_opts(s) if s.redis_tls else None,
    )
    return celery


def _tls_opts(settings: Settings) -> dict[str, Any]:
    return {"ssl_cert_reqs": None, "ssl_keyfile": None, "ssl_certfile": None, "ssl_ca_certs": None}


# Module-level app: imported by tasks and the FastAPI lifespan.
app: CeleryApp = build_app()


def task_id(case_id: str, attempt_number: int) -> str:
    """Deterministic task id so scheduled retries are themselves idempotent."""
    return f"reclaim-{case_id}-{attempt_number}"