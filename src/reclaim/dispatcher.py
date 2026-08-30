"""Webhook -> pipeline handoff.

Kept deliberately thin: a fresh case is dispatched exactly once. When Celery
runs in eager mode (the build default) the dispatch executes the pipeline
synchronously in-process; otherwise it enqueues the Celery task. The pipeline
module is imported lazily so the webhook boundary loads independent of it.
"""

from __future__ import annotations

import logging

from .config import Settings, get_settings

logger = logging.getLogger("reclaim.dispatcher")


def submit_case(case_id: str, event_id: str, settings: Settings | None = None) -> None:
    """Dispatch a freshly ingested case through the recovery pipeline.

    Called exactly once per new case (never for duplicates).
    """
    s = settings or get_settings()
    if s.reclaim_celery_eager:
        from .pipeline import run_case  # lazy: pipeline wires in a later step

        logger.info("DISPATCH eager case=%s event=%s", case_id, event_id)
        run_case(case_id)
    else:
        from .celery_app import app

        app.send_task(
            "reclaim.pipeline.run_case_task",
            args=[case_id],
            task_id=f"reclaim-{case_id}-ingest",
        )
        logger.info("DISPATCH celery case=%s event=%s", case_id, event_id)