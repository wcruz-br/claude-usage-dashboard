#!/usr/bin/env python3
"""
Real-time usage dashboard for Claude Code (Pro/Max plans).

Reads OAuth credentials from the platform-appropriate secure store used by
Claude Code (macOS Keychain, Windows Credential Manager, or the JSON file at
~/.claude/.credentials.json on Linux/WSL) and polls the undocumented endpoint
https://api.anthropic.com/api/oauth/usage every 5 minutes.

Dependencies: stdlib only (Python 3.11+)
Requires 3.11 for: datetime.UTC and datetime.fromisoformat() with "Z" suffix

Note: undocumented endpoint, subject to change without notice! If it stops working,
check whether the credentials file format changed or the endpoint was updated.
As a last resort, consider migrating to `ccusage`.
"""

import json
import platform
import select
import subprocess
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
# Identify this script honestly instead of impersonating the official
# Claude Code CLI. Spoofing a specific CLI version is brittle — if
# Anthropic ever filters by User-Agent, a stale hardcoded version will
# break silently. A clear UA also makes it obvious this is an
# independent tool, not official Anthropic software.
USER_AGENT = "claude-usage-dashboard/1.0"
REFRESH_INTERVAL_SECONDS = 300  # 5 minutes
BAR_WIDTH = 30

# Service/target name used by Claude Code when storing credentials in the
# OS-native secret store. On macOS this is the Keychain service name; on
# Windows it's the Credential Manager target name. Identified empirically
# via `security dump-keychain | grep -i claude` on macOS.
KEYCHAIN_SERVICE_NAME = "Claude Code-credentials"


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def _load_from_macos_keychain() -> dict | None:
    """Read credentials from macOS Keychain via the built-in `security` CLI."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE_NAME, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout.strip())
    except (json.JSONDecodeError, subprocess.SubprocessError, FileNotFoundError):
        pass
    return None


def _load_from_windows_credential_manager() -> dict | None:
    """Read credentials from Windows Credential Manager via PowerShell.

    Note: untested on a real Windows machine — known limitation with SecureString
    handling. Falls back to the credentials file gracefully.
    See: https://github.com/wcruz-br/claude-usage-dashboard/issues/5
    """
    ps_script = (
        f"Get-StoredCredential -Target '{KEYCHAIN_SERVICE_NAME}' "
        "| Select-Object -ExpandProperty Password "
        "| ForEach-Object { "
        "[System.Text.Encoding]::UTF8.GetString($_) }"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout.strip())
    except (json.JSONDecodeError, subprocess.SubprocessError, FileNotFoundError):
        pass
    return None


def _load_from_credentials_file() -> dict | None:
    """Read credentials from ~/.claude/.credentials.json."""
    if CREDENTIALS_PATH.exists():
        try:
            return json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return None


def load_credentials() -> OAuthCredentials:
    """Read the OAuth token from the platform-appropriate secure store.

    Resolution order:
        1. macOS Keychain (on Darwin)
        2. Windows Credential Manager (on Windows)
        3. ~/.claude/.credentials.json (universal fallback, used on Linux/WSL)

    Raises FileNotFoundError if no credentials are found in any location.
    """
    data: dict | None = None
    system = platform.system()

    if system == "Darwin":
        data = _load_from_macos_keychain()
    elif system == "Windows":
        data = _load_from_windows_credential_manager()

    if data is None:
        data = _load_from_credentials_file()

    if data is None:
        raise FileNotFoundError(
            "Claude Code credentials not found. Checked:\n"
            f"  macOS:   Keychain (service '{KEYCHAIN_SERVICE_NAME}')\n"
            f"  Linux:   {CREDENTIALS_PATH}\n"
            f"  Windows: Credential Manager (target '{KEYCHAIN_SERVICE_NAME}')\n"
            "Run 'claude' in your terminal and log in before using this script."
        )

    if "claudeAiOauth" not in data:
        raise KeyError(
            "'claudeAiOauth' key missing from stored credentials. "
            "The credential format may have changed."
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

    print(f"\n  {DIM}Press Ctrl+C to exit · SPACE to refresh now{RESET}\n")


# ---------------------------------------------------------------------------
# Interruptible sleep
# ---------------------------------------------------------------------------


def _interruptible_sleep_unix(seconds: int) -> None:
    """Wait up to `seconds`, returning early if SPACE is pressed (Unix/macOS)."""
    import termios
    import tty

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        end_time = time.monotonic() + seconds
        while True:
            remaining = end_time - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([sys.stdin], [], [], min(remaining, 1.0))
            if ready:
                if sys.stdin.read(1) == " ":
                    break
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def _interruptible_sleep_windows(seconds: int) -> None:
    """Wait up to `seconds`, returning early if SPACE is pressed (Windows)."""
    import msvcrt

    end_time = time.monotonic() + seconds
    while time.monotonic() < end_time:
        if msvcrt.kbhit() and msvcrt.getwch() == " ":  # type: ignore[attr-defined]
            break
        time.sleep(0.1)


def _interruptible_sleep(seconds: int) -> None:
    """Sleep for up to `seconds`, interrupted immediately by a SPACE keypress."""
    system = platform.system()
    if system == "Windows":
        _interruptible_sleep_windows(seconds)
    elif sys.stdin.isatty():
        _interruptible_sleep_unix(seconds)
    else:
        time.sleep(seconds)


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
            # The usage endpoint is undocumented and can change without notice.
            # Logging the response body on errors turns "HTTP 403: Forbidden"
            # (opaque) into something that usually tells us what actually
            # happened: auth scheme changed, endpoint moved, deprecation
            # notice, etc. Truncated to 200 chars to avoid flooding the
            # terminal if the server returns a full HTML error page.
            try:
                body = exc.read().decode("utf-8", "replace")[:200]
            except Exception:  # pylint: disable=broad-except
                body = "(could not read response body)"
            print(f"\n{RED}HTTP error {exc.code}:{RESET} {exc.reason}")
            if body:
                print(f"  {DIM}{body}{RESET}")
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
            _interruptible_sleep(REFRESH_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            print(f"\n{DIM}Monitor stopped.{RESET}\n")
            sys.exit(0)


if __name__ == "__main__":
    run()
