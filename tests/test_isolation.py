"""The test run can never open the user's real database (see conftest.py)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from conftest import REAL_DATA_DIR, REFUSED, db_file

from jarvis import db
from jarvis.config import Config


def test_default_config_database_is_refused():
    real = Config().db_path
    assert REAL_DATA_DIR in Path(real).resolve().parents
    with pytest.raises(RuntimeError, match="real Jarvis database"):
        db.connect(real)


def test_uri_form_is_refused_too():
    import sqlite3

    uri = f"{(REAL_DATA_DIR / 'jarvis.db').as_uri()}?mode=ro"
    with pytest.raises(RuntimeError):
        sqlite3.connect(uri, uri=True)


def test_env_database_is_a_scratch_file():
    assert REAL_DATA_DIR not in Path(os.environ["JARVIS_DB"]).resolve().parents
    assert REAL_DATA_DIR not in Path(Config.from_env().db_path).resolve().parents


def test_db_file_parsing(tmp_path):
    assert db_file(":memory:") is None
    assert db_file(tmp_path / "a.db") == (tmp_path / "a.db").resolve()
    assert db_file(f"{(tmp_path / 'b c.db').as_uri()}?mode=ro", uri=True) == (
        tmp_path / "b c.db").resolve()


@pytest.fixture(autouse=True)
def _expected_refusals():
    """The refusals these tests provoke on purpose are not leaks."""
    before = len(REFUSED)
    yield
    del REFUSED[before:]
