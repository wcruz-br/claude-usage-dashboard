#!/usr/bin/env python3
"""
Real-time usage dashboard for Claude Code (Pro/Max plans).
Reads OAuth credentials from ~/.claude/.credentials.json and polls
the undocumented endpoint https://api.anthropic.com/api/oauth/usage
every 5 minutes.

Dependencies: stdlib only (Python 3.11+)
Requires 3.11 for: datetime.UTC and datetime.fromisoformat() with "Z" suffix

Note: undocumented endpoint, subject to change without notice! If it stops working,
check whether the credentials file format changed or the endpoint was updated.
As a last resort, consider migrating to `ccusage`.
"""

import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import TypedDict

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class OAuthCredentials(TypedDict):
    accessToken: str
    refreshToken: str
    expiresAt: int  # timestamp in milliseconds


class UsageWindow(TypedDict):
    utilization: float  # raw percentage returned by the API (e.g. 4.0 = 4%)
    resets_at: str | None  # ISO 8601 or null


class UsageLimits(TypedDict):
    five_hour: UsageWindow | None
    seven_day: UsageWindow | None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
USAGE_ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
ANTHROPIC_BETA_HEADER = "oauth-2025-04-20"
USER_AGENT = "claude-code/2.1.58"
REFRESH_INTERVAL_SECONDS = 300  # 5 minutes
BAR_WIDTH = 30


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def load_credentials() -> OAuthCredentials:
    """Read the OAuth token from the Claude Code credentials file."""
    if not CREDENTIALS_PATH.exists():
        raise FileNotFoundError(
            f"Credentials file not found: {CREDENTIALS_PATH}\n"
            "Run 'claude' and log in before using this script."
        )

    raw = CREDENTIALS_PATH.read_text(encoding="utf-8")
    data = json.loads(raw)

    if "claudeAiOauth" not in data:
        raise KeyError(
            "'claudeAiOauth' key missing from .credentials.json. "
            "The file format may have changed."
        )

    return data["claudeAiOauth"]


def is_token_expired(credentials: OAuthCredentials) -> bool:
    """Return True if the access token is expired."""
    expires_at_ms = credentials.get("expiresAt", 0)
    now_ms = int(time.time() * 1000)
    return now_ms >= expires_at_ms


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def fetch_usage(access_token: str) -> UsageLimits:
    """Query the usage endpoint and return the limits."""
    req = urllib.request.Request(
        USAGE_ENDPOINT,
        method="GET",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": ANTHROPIC_BETA_HEADER,
        },
    )

    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[91m"
YELLOW = "\033[93m"
GREEN = "\033[92m"
CYAN = "\033[96m"
WHITE = "\033[97m"


def color_for_utilization(utilization: float) -> str:
    if utilization >= 0.85:
        return RED
    if utilization >= 0.60:
        return YELLOW
    return GREEN


def render_bar(utilization: float, width: int = BAR_WIDTH) -> str:
    filled = round(utilization * width)
    empty = width - filled
    color = color_for_utilization(utilization)
    return f"{color}{'█' * filled}{DIM}{'░' * empty}{RESET}"


def format_resets_at(resets_at: str | None) -> str:
    if not resets_at:
        return "—"
    try:
        dt = datetime.fromisoformat(resets_at)
        local_dt = dt.astimezone()
        remaining = dt - datetime.now().astimezone()
        total_seconds = int(remaining.total_seconds())
        if total_seconds < 0:
            return "expired"
        hours, remainder = divmod(total_seconds, 3600)
        minutes = remainder // 60
        if hours >= 24:
            days, remaining_hours = divmod(hours, 24)
            time_str = f"{days}d{remaining_hours}h" if remaining_hours else f"{days}d"
            return f"in {time_str}"
        time_str = f"{hours}h{minutes:02d}m"
        return f"in {time_str} ({local_dt.strftime('%H:%M')})"
    except (ValueError, TypeError):
        return resets_at


def render_window(label: str, window: UsageWindow | None) -> str:
    if window is None:
        return f"  {BOLD}{label}{RESET}: {DIM}not available{RESET}\n"

    utilization = window.get("utilization", 0.0) / 100
    pct = utilization * 100
    resets_at = window.get("resets_at")

    bar = render_bar(utilization)
    color = color_for_utilization(utilization)
    pct_str = f"{color}{BOLD}{pct:5.1f}%{RESET}"
    resets_str = format_resets_at(resets_at)

    lines = [
        f"  {BOLD}{label}{RESET}",
        f"  {bar} {pct_str}  {DIM}reset: {resets_str}{RESET}",
        "",
    ]
    return "\n".join(lines)


def clear_screen() -> None:
    # Use an ANSI escape sequence instead of shelling out to clear/cls.
    # \033[2J clears the entire screen, \033[H moves the cursor to the
    # top-left. This avoids spawning a subprocess every refresh (300s)
    # and works consistently across macOS, Linux, and modern Windows
    # terminals (Windows Terminal, VS Code, PowerShell 7+).
    print("\033[2J\033[H", end="", flush=True)


def render_dashboard(usage: UsageLimits, fetched_at: datetime) -> None:
    clear_screen()
    timestamp = fetched_at.strftime("%Y-%m-%d %H:%M:%S")

    print(f"\n{BOLD}{CYAN}  Claude Code — Usage Monitor{RESET}")
    print(
        f"  {DIM}Updated at {timestamp} · "
        f"next refresh in {REFRESH_INTERVAL_SECONDS // 60} min{RESET}"
    )
    print(f"  {DIM}{'─' * 50}{RESET}\n")

    print(render_window("5-hour window", usage.get("five_hour")), end="")
    print(render_window("7-day window ", usage.get("seven_day")), end="")

    print(f"  {DIM}Press Ctrl+C to exit.{RESET}\n")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def run() -> None:
    print(f"{CYAN}Starting Claude Code usage monitor...{RESET}")

    while True:
        try:
            credentials = load_credentials()

            if is_token_expired(credentials):
                print(
                    f"\n{YELLOW}Token expired.{RESET} Run 'claude' once to renew "
                    "credentials automatically, then restart this script.\n"
                )
                sys.exit(1)

            usage = fetch_usage(credentials["accessToken"])
            render_dashboard(usage, datetime.now().astimezone())

        except FileNotFoundError as exc:
            print(f"\n{RED}Error:{RESET} {exc}\n")
            sys.exit(1)

        except urllib.error.HTTPError as exc:
            print(f"\n{RED}HTTP error {exc.code}:{RESET} {exc.reason}")
            if exc.code == 401:
                print(
                    "Invalid or expired token. Run 'claude' to renew "
                    "and restart this script."
                )
                sys.exit(1)
            print(f"Retrying in {REFRESH_INTERVAL_SECONDS}s...\n")

        except urllib.error.URLError as exc:
            print(
                f"\n{YELLOW}Network error:{RESET} {exc.reason}. "
                f"Retrying in {REFRESH_INTERVAL_SECONDS}s...\n"
            )

        except KeyboardInterrupt:
            print(f"\n{DIM}Monitor stopped.{RESET}\n")
            sys.exit(0)

        except Exception as exc:  # pylint: disable=broad-except
            print(f"\n{RED}Unexpected error:{RESET} {exc}\n")

        try:
            time.sleep(REFRESH_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            print(f"\n{DIM}Monitor stopped.{RESET}\n")
            sys.exit(0)


if __name__ == "__main__":
    run()
