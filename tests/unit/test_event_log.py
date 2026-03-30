"""Unit tests for the structured event logger.

The conftest autouse fixture suppresses ``_write`` globally; this file restores
the real implementation via a local autouse fixture so actual file I/O can be
verified.  A fresh temp directory is used for every test.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import daoc_bot.event_log as ev

# Capture the real _write before conftest patches it (module-level → runs first).
_REAL_WRITE = ev._write


def _read_lines(tmp_path: Path) -> list[dict[str, Any]]:
    """Parse all JSONL lines from the log file written during a test."""
    logs = list((tmp_path / "logs").glob("*.jsonl"))
    if not logs:
        return []
    return [json.loads(line) for line in logs[0].read_text().splitlines() if line]


@pytest.fixture(autouse=True)
def _setup_real_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Restore the real _write and redirect I/O to a temp directory."""
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(ev, "_LOG_DIR", log_dir)
    monkeypatch.setattr(ev, "_log_file", None)
    monkeypatch.setattr(ev, "_match_proposal_times", {})
    monkeypatch.setattr(ev, "_match_start_times", {})
    # Undo the global suppression from conftest so real I/O happens here.
    monkeypatch.setattr("daoc_bot.event_log._write", _REAL_WRITE)


class TestWriteHelper:
    def test_creates_log_directory(self, tmp_path: Path) -> None:
        ev._write("test_event", key="value")
        assert (tmp_path / "logs").is_dir()

    def test_entry_has_event_and_ts(self, tmp_path: Path) -> None:
        ev._write("test_event")
        lines = _read_lines(tmp_path)
        assert len(lines) == 1
        assert lines[0]["event"] == "test_event"
        assert "ts" in lines[0]

    def test_extra_kwargs_included(self, tmp_path: Path) -> None:
        ev._write("test_event", alpha=1, bravo="two")
        lines = _read_lines(tmp_path)
        assert lines[0]["alpha"] == 1
        assert lines[0]["bravo"] == "two"

    def test_multiple_events_appended(self, tmp_path: Path) -> None:
        ev._write("event_a")
        ev._write("event_b")
        lines = _read_lines(tmp_path)
        assert len(lines) == 2
        assert lines[0]["event"] == "event_a"
        assert lines[1]["event"] == "event_b"


class TestPublicAPI:
    def test_team_registered(self, tmp_path: Path) -> None:
        ev.team_registered("Gandalf", 99)
        lines = _read_lines(tmp_path)
        assert lines[0]["event"] == "team_registered"
        assert lines[0]["leader_id"] == 99

    def test_queue_entered(self, tmp_path: Path) -> None:
        ev.queue_entered("Alpha")
        lines = _read_lines(tmp_path)
        assert lines[0]["event"] == "queue_entered"
        assert lines[0]["team"] == "Alpha"

    def test_queue_left(self, tmp_path: Path) -> None:
        ev.queue_left("Alpha", reason="unready")
        lines = _read_lines(tmp_path)
        assert lines[0]["reason"] == "unready"

    def test_match_proposed_records_timestamp(self) -> None:
        ev.match_proposed("M001", "Alpha", "Bravo")
        assert "M001" in ev._match_proposal_times  # type: ignore[attr-defined]

    def test_match_started_records_elapsed(self, tmp_path: Path) -> None:
        ev.match_proposed("M002", "Alpha", "Bravo")
        ev.match_started("M002", "Alpha", "Bravo")
        lines = _read_lines(tmp_path)
        started = next(ln for ln in lines if ln["event"] == "match_started")
        assert "elapsed_since_proposal_s" in started
        assert started["elapsed_since_proposal_s"] is not None

    def test_mmr_updated_includes_deltas(self, tmp_path: Path) -> None:
        ev.mmr_updated("Alpha", 1000, 1016, "Bravo", 1000, 984)
        lines = _read_lines(tmp_path)
        assert lines[0]["winner_delta"] == 16
        assert lines[0]["loser_delta"] == -16

    def test_match_cancelled_admin_clears_timestamps(self) -> None:
        ev.match_proposed("M003", "Alpha", "Bravo")
        ev.match_started("M003", "Alpha", "Bravo")
        ev.match_cancelled_admin("M003", "Alpha", "Bravo", reason="admin")
        # Both timestamp dicts should no longer have M003
        assert "M003" not in ev._match_proposal_times  # type: ignore[attr-defined]
        assert "M003" not in ev._match_start_times     # type: ignore[attr-defined]
