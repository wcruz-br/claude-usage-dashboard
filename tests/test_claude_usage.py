"""Tests for claude_usage.py — pure-logic functions only.

Excluded from testing: fetch_usage (requires live API), run (infinite loop),
render_dashboard / clear_screen (terminal I/O).
"""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

import claude_usage as cu

# ---------------------------------------------------------------------------
# color_for_utilization
# ---------------------------------------------------------------------------


class TestColorForUtilization:
    def test_below_60_is_green(self) -> None:
        assert cu.color_for_utilization(0.0) == cu.GREEN
        assert cu.color_for_utilization(0.59) == cu.GREEN

    def test_at_60_is_yellow(self) -> None:
        assert cu.color_for_utilization(0.60) == cu.YELLOW

    def test_between_60_and_85_is_yellow(self) -> None:
        assert cu.color_for_utilization(0.84) == cu.YELLOW

    def test_at_85_is_red(self) -> None:
        assert cu.color_for_utilization(0.85) == cu.RED

    def test_above_85_is_red(self) -> None:
        assert cu.color_for_utilization(1.0) == cu.RED


# ---------------------------------------------------------------------------
# render_bar
# ---------------------------------------------------------------------------


class TestRenderBar:
    def test_total_block_count_matches_width(self) -> None:
        bar = cu.render_bar(0.5, width=10)
        filled = bar.count("█")
        empty = bar.count("░")
        assert filled + empty == 10

    def test_zero_utilization_all_empty(self) -> None:
        bar = cu.render_bar(0.0, width=10)
        assert bar.count("█") == 0
        assert bar.count("░") == 10

    def test_full_utilization_all_filled(self) -> None:
        bar = cu.render_bar(1.0, width=10)
        assert bar.count("█") == 10
        assert bar.count("░") == 0

    def test_color_reflects_utilization(self) -> None:
        assert cu.GREEN in cu.render_bar(0.3, width=10)
        assert cu.YELLOW in cu.render_bar(0.7, width=10)
        assert cu.RED in cu.render_bar(0.9, width=10)


# ---------------------------------------------------------------------------
# format_resets_at
# ---------------------------------------------------------------------------


def _future_iso(seconds: int) -> str:
    """Return an ISO 8601 string `seconds` from now (UTC)."""
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


def _past_iso(seconds: int) -> str:
    """Return an ISO 8601 string `seconds` in the past (UTC)."""
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()


class TestFormatResetsAt:
    def test_none_returns_dash(self) -> None:
        assert cu.format_resets_at(None) == "—"

    def test_empty_string_returns_dash(self) -> None:
        assert cu.format_resets_at("") == "—"

    def test_expired_returns_expired(self) -> None:
        assert cu.format_resets_at(_past_iso(60)) == "expired"

    def test_under_24h_shows_hours_and_minutes(self) -> None:
        result = cu.format_resets_at(_future_iso(3 * 3600 + 15 * 60))
        # Allow ±1 min tolerance: a few ms elapse between _future_iso() and now()
        assert result.startswith("in 3h1")
        assert "m (" in result

    def test_over_24h_shows_days(self) -> None:
        result = cu.format_resets_at(_future_iso(49 * 3600))
        assert result.startswith("in 2d")

    def test_exact_days_no_hours_suffix(self) -> None:
        result = cu.format_resets_at(_future_iso(48 * 3600 + 30))
        assert "d0h" not in result

    def test_invalid_string_returned_as_is(self) -> None:
        assert cu.format_resets_at("not-a-date") == "not-a-date"


# ---------------------------------------------------------------------------
# _load_from_credentials_file
# ---------------------------------------------------------------------------


class TestLoadFromCredentialsFile:
    def test_returns_none_when_file_missing(self, tmp_path) -> None:
        with patch.object(cu, "CREDENTIALS_PATH", tmp_path / "missing.json"):
            assert cu._load_from_credentials_file() is None

    def test_returns_none_on_invalid_json(self, tmp_path) -> None:
        f = tmp_path / ".credentials.json"
        f.write_text("not json", encoding="utf-8")
        with patch.object(cu, "CREDENTIALS_PATH", f):
            assert cu._load_from_credentials_file() is None

    def test_returns_parsed_dict_on_valid_file(self, tmp_path) -> None:
        payload = {
            "claudeAiOauth": {
                "accessToken": "tok",
                "refreshToken": "ref",
                "expiresAt": 9999999999000,
            }
        }
        f = tmp_path / ".credentials.json"
        f.write_text(json.dumps(payload), encoding="utf-8")
        with patch.object(cu, "CREDENTIALS_PATH", f):
            result = cu._load_from_credentials_file()
        assert result == payload


# ---------------------------------------------------------------------------
# load_credentials
# ---------------------------------------------------------------------------


class TestLoadCredentials:
    def _write_creds(self, tmp_path, payload: dict):
        f = tmp_path / ".credentials.json"
        f.write_text(json.dumps(payload), encoding="utf-8")
        return f

    def test_raises_when_no_credentials_found(self, tmp_path) -> None:
        with patch.object(cu, "CREDENTIALS_PATH", tmp_path / "missing.json"):
            with patch("platform.system", return_value="Linux"):
                with pytest.raises(FileNotFoundError):
                    cu.load_credentials()

    def test_raises_when_oauth_key_missing(self, tmp_path) -> None:
        f = self._write_creds(tmp_path, {"otherKey": {}})
        with patch.object(cu, "CREDENTIALS_PATH", f):
            with patch("platform.system", return_value="Linux"):
                with pytest.raises(KeyError):
                    cu.load_credentials()

    def test_returns_oauth_credentials(self, tmp_path) -> None:
        oauth = {
            "accessToken": "tok",
            "refreshToken": "ref",
            "expiresAt": 9999999999000,
        }
        f = self._write_creds(tmp_path, {"claudeAiOauth": oauth})
        with patch.object(cu, "CREDENTIALS_PATH", f):
            with patch("platform.system", return_value="Linux"):
                result = cu.load_credentials()
        assert result == oauth

    def test_macos_falls_back_to_file_when_keychain_returns_none(
        self, tmp_path
    ) -> None:
        oauth = {
            "accessToken": "tok",
            "refreshToken": "ref",
            "expiresAt": 9999999999000,
        }
        f = self._write_creds(tmp_path, {"claudeAiOauth": oauth})
        with patch.object(cu, "CREDENTIALS_PATH", f):
            with patch("platform.system", return_value="Darwin"):
                with patch.object(cu, "_load_from_macos_keychain", return_value=None):
                    result = cu.load_credentials()
        assert result == oauth
