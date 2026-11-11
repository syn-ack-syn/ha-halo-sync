"""AutoResume: when a stream that the TV ended is started again."""

from __future__ import annotations

from pathlib import Path

import pytest

from halo_sync import resume as resume_mod
from halo_sync.resume import MIN_INTERVAL_S, AutoResume


@pytest.mark.parametrize("reason", ["tv_standby", "tv_off"])
def test_tv_reasons_set_pending(reason):
    r = AutoResume()
    assert r.stream_ended(reason) is True
    assert r.pending


@pytest.mark.parametrize("reason", ["user", "error", None])
def test_other_reasons_do_not_set_pending(reason):
    r = AutoResume()
    assert r.stream_ended(reason) is False
    assert not r.pending


@pytest.mark.parametrize("reason", ["user", "error", None])
def test_other_reasons_clear_pending(reason):
    r = AutoResume(pending=True)
    assert r.stream_ended(reason) is True
    assert not r.pending


@pytest.mark.parametrize("reason", ["tv_standby", "tv_off", "user", "error", None])
def test_option_off_never_sets_pending_or_starts(reason):
    r = AutoResume(enabled=False)
    assert r.stream_ended(reason) is False
    assert not r.pending
    assert r.should_start("ok", False, 0.0) is False
    assert r.should_start("ok", False, 1000.0) is False


def test_should_start_only_when_ok_and_not_running():
    r = AutoResume(pending=True)
    assert r.should_start("standby", False, 0.0) is False
    assert r.should_start("unreachable", False, 0.0) is False
    assert r.should_start("ok", True, 0.0) is False
    assert r.should_start("ok", False, 0.0) is True


def test_should_start_needs_pending():
    r = AutoResume()
    assert r.should_start("ok", False, 0.0) is False


def test_second_start_within_the_interval_is_refused():
    r = AutoResume(pending=True)
    assert r.should_start("ok", False, 100.0) is True
    assert r.should_start("ok", False, 100.0 + MIN_INTERVAL_S - 0.1) is False
    assert r.should_start("ok", False, 100.0 + MIN_INTERVAL_S) is True


def test_refused_checks_do_not_count_as_attempts():
    r = AutoResume(pending=True)
    assert r.should_start("standby", False, 5.0) is False
    assert r.should_start("ok", False, 6.0) is True  # the standby check above did not start the interval


def test_min_interval_is_ten_seconds():
    assert MIN_INTERVAL_S == 10.0


def test_stream_started_clears_pending():
    r = AutoResume(pending=True)
    assert r.stream_started() is True
    assert not r.pending
    assert r.stream_started() is False


def test_user_stopped_clears_pending():
    r = AutoResume(pending=True)
    assert r.user_stopped() is True
    assert not r.pending
    assert r.user_stopped() is False


def test_set_enabled_reports_changes_and_off_clears_pending():
    r = AutoResume(pending=True)
    assert r.set_enabled(True) is False
    assert r.set_enabled(False) is True
    assert not r.enabled and not r.pending
    assert r.set_enabled(False) is False
    assert r.set_enabled(True) is True
    assert r.enabled and not r.pending


def test_repeated_end_reports_no_change():
    r = AutoResume()
    assert r.stream_ended("tv_standby") is True
    assert r.stream_ended("tv_off") is False


def test_defaults():
    r = AutoResume()
    assert r.enabled is True and r.pending is False


def test_resume_module_does_not_import_home_assistant():
    src = Path(resume_mod.__file__).read_text()
    assert "homeassistant" not in src
