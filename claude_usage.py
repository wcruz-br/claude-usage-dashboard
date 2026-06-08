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

import ctypes
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

# Windows Credential Manager constants
CRED_TYPE_GENERIC = 1
CRED_PERSIST_SESSION = 2


# ---------------------------------------------------------------------------
# Windows Credential Manager (ctypes)
# ---------------------------------------------------------------------------


def _load_from_windows_credential_manager() -> dict | None:
    """Read credentials from Windows Credential Manager via ctypes + CredReadW.

    Uses the Windows API directly (advapi32.dll) to avoid dependency on the
    CredentialManager PowerShell module and handle SecureString correctly.
    """
    if platform.system() != "Windows":
        return None

    try:
        # Load advapi32.dll
        advapi32 = ctypes.windll.advapi32

        # Define CREDENTIALW structure
        class CREDENTIALW(ctypes.Structure):
            _fields_ = [
                ("Flags", ctypes.c_ulong),
                ("Type", ctypes.c_ulong),
                ("TargetName", ctypes.c_wchar_p),
                ("Comment", ctypes.c_wchar_p),
                ("LastWritten", ctypes.c_ulonglong),
                ("CredentialBlobSize", ctypes.c_ulong),
                ("CredentialBlob", ctypes.c_void_p),
                ("Persist", ctypes.c_ulong),
                ("AttributeCount", ctypes.c_ulong),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", ctypes.c_wchar_p),
                ("UserName", ctypes.c_wchar_p),
            ]

        # Define pointer type for CREDENTIALW
        PCREDENTIALW = ctypes.POINTER(CREDENTIALW)

        # Call CredReadW
        cred_ptr = PCREDENTIALW()
        target_name = KEYCHAIN_SERVICE_NAME

        result = advapi32.CredReadW(
            target_name,
            CRED_TYPE_GENERIC,
            0,
            ctypes.byref(cred_ptr),
        )

        if result == 0:
            # Failed to read credential
            return None

        try:
            cred = cred_ptr.contents
            if cred.CredentialBlob and cred.CredentialBlobSize > 0:
                # Read the credential blob as bytes
                blob_bytes = ctypes.string_at(
                    cred.CredentialBlob, cred.CredentialBlobSize
                )
                # Decode as UTF-8 and parse as JSON
                blob_str = blob_bytes.decode("utf-8")
                return json.loads(blob_str)
        finally:
            # Free the credential memory
            advapi32.CredFree(cred_ptr)

    except (OSError, json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        pass

    return None


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

        days = total_seconds // 86400
        hours = (total_seconds % 86400) // 3600
        minutes = (total_seconds % 3600) // 60

        if days > 0:
            if hours == 0 and minutes == 0:
                return f"in {days}d"
            return f"in {days}d{hours}h"
        return f"in {hours}h{minutes}m ({local_dt.strftime('%H:%M')})"
    except (ValueError, TypeError, OverflowError):
        return resets_at


def render_dashboard(limits: UsageLimits) -> str:
    """Render the usage dashboard as a string."""
    lines: list[str] = []

    # Header
    lines.append(f"{BOLD}{WHITE}Claude Code Usage{RESET}")
    lines.append(f"{DIM}Refreshes every {REFRESH_INTERVAL_SECONDS // 60} minutes • Ctrl+C to exit{RESET}")
    lines.append("")

    # Five-hour window
    five_hour = limits.get("five_hour")
    if five_hour:
        util = five_hour.get("utilization", 0) / 100
        bar = render_bar(util)
        resets = format_resets_at(five_hour.get("resets_at"))
        lines.append(f"{CYAN}5-hour:{RESET} {bar} {util * 100:.1f}%")
        lines.append(f"{DIM}        Resets {resets}{RESET}")
    else:
        lines.append(f"{CYAN}5-hour:{RESET} {DIM}—{RESET}")

    lines.append("")

    # Seven-day window
    seven_day = limits.get("seven_day")
    if seven_day:
        util = seven_day.get("utilization", 0) / 100
        bar = render_bar(util)
        resets = format_resets_at(seven_day.get("resets_at"))
        lines.append(f"{CYAN}7-day:{RESET}  {bar} {util * 100:.1f}%")
        lines.append(f"{DIM}        Resets {resets}{RESET}")
    else:
        lines.append(f"{CYAN}7-day:{RESET}  {DIM}—{RESET}")

    return "\n".join(lines)


def clear_screen() -> None:
    """Clear the terminal screen and move cursor to top-left."""
    # ANSI escape sequence: clear screen + move cursor home
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run() -> None:
    """Main loop: fetch and display usage statistics."""
    print(f"{DIM}Loading credentials...{RESET}", flush=True)
    credentials = load_credentials()

    if is_token_expired(credentials):
        print(f"{RED}Error: Access token expired. Please re-authenticate.{RESET}")
        print(f"{DIM}Run 'claude' in your terminal to log in again.{RESET}")
        sys.exit(1)

    access_token = credentials["accessToken"]
    last_refresh = 0.0

    print(f"{DIM}Press Ctrl+C to exit{RESET}\n", flush=True)

    try:
        while True:
            now = time.time()

            # Refresh if needed
            if now - last_refresh >= REFRESH_INTERVAL_SECONDS:
                try:
                    limits = fetch_usage(access_token)
                    last_refresh = now
                except urllib.error.HTTPError as e:
                    error_body = ""
                    try:
                        error_body = e.read().decode("utf-8")
                    except Exception:
                        pass
                    print(f"{RED}API error {e.code}: {error_body}{RESET}", flush=True)
                    time.sleep(10)
                    continue
                except urllib.error.URLError as e:
                    print(f"{RED}Network error: {e.reason}{RESET}", flush=True)
                    time.sleep(10)
                    continue

                clear_screen()
                print(render_dashboard(limits))
                print()

            # Wait with interruptible sleep
            sleep_time = min(1, REFRESH_INTERVAL_SECONDS - (now - last_refresh))
            if sleep_time > 0:
                select.select([sys.stdin], [], [], sleep_time)

    except KeyboardInterrupt:
        print(f"\n{DIM}Goodbye!{RESET}")
        sys.exit(0)


if __name__ == "__main__":
    run()