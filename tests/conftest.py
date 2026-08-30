"""Shared test fixtures.

Hermetic by construction: settings never read the real ``.env``, the database
lives in a per-test tmp file, and the LLM + Act layers are in safe modes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reclaim.config import Settings, clear_settings_cache
from reclaim.db import Database, init_schema, reset_db_for_tests

TEST_WEBHOOK_SECRET = "test-secret-for-hermetic-tests-0123456789abcdef"


@pytest.fixture(autouse=True)
def _no_dotenv() -> None:
    """Ensure no test reads the real .env or reuses the cached singleton."""
    clear_settings_cache()


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # ignore .env: tests are hermetic
        razorpay_webhook_secret=TEST_WEBHOOK_SECRET,
        llm_mode="offline",
        act_mode="stub",
        reclaim_celery_eager=True,
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
    )


@pytest.fixture()
def db(settings: Settings) -> Database:
    database = Database(settings)
    init_schema(database.engine)
    reset_db_for_tests(database)
    yield database
    reset_db_for_tests()
    database.close()