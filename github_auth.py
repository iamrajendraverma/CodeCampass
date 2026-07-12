"""GitHub OAuth device-flow login.

Lets the client obtain a GitHub *user* access token interactively — the user
approves in their browser instead of hand-creating a Personal Access Token. The
resulting token carries the user's own permissions, so the MCP server can then
see private repositories that user has access to.

Requires a registered GitHub OAuth App (or GitHub App) with **device flow
enabled**, whose client_id is provided via the GITHUB_CLIENT_ID env var (or by
editing DEFAULT_CLIENT_ID below). Create one at:
    https://github.com/settings/developers  →  New OAuth App
    then check "Enable Device Flow" in the app's settings.

Docs: https://docs.github.com/apps/creating-github-apps/writing-code-for-a-github-app/building-a-cli-with-a-github-app
"""

from __future__ import annotations

import os
import time

import httpx

import banner

DEVICE_CODE_URL = "https://github.com/login/device/code"
ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"

# "repo" grants read/write to private repositories; "read:org" lets us see
# private org repos the user belongs to. Trim to "public_repo" if you only ever
# want public data.
SCOPE = "repo read:org"

# Your OAuth App's client_id. Prefer setting GITHUB_CLIENT_ID in the environment
# (or .env) over editing this placeholder.
DEFAULT_CLIENT_ID = "YOUR_OAUTH_APP_CLIENT_ID"

_PLACEHOLDER = "YOUR_OAUTH_APP_CLIENT_ID"


class DeviceFlowError(RuntimeError):
    """Raised when the device-flow login cannot complete."""


def _resolve_client_id(client_id: str | None) -> str:
    client_id = client_id or os.environ.get("GITHUB_CLIENT_ID") or DEFAULT_CLIENT_ID
    if not client_id or client_id == _PLACEHOLDER:
        raise DeviceFlowError(
            "No GitHub OAuth client_id configured. Register an OAuth App with "
            "device flow enabled and set GITHUB_CLIENT_ID (see github_auth.py)."
        )
    return client_id


def login(client_id: str | None = None, scope: str = SCOPE) -> str:
    """Run the GitHub device flow and return a user access token.

    Blocks while polling GitHub for the user to approve in their browser. Raises
    DeviceFlowError on misconfiguration, denial, or timeout.
    """
    client_id = _resolve_client_id(client_id)
    headers = {"Accept": "application/json", "User-Agent": "codecompass/1.0"}

    with httpx.Client(timeout=30.0, headers=headers) as http:
        # 1) Ask GitHub for a device code + a short user code.
        resp = http.post(DEVICE_CODE_URL, data={"client_id": client_id, "scope": scope})
        resp.raise_for_status()
        data = resp.json()
        if "device_code" not in data:
            raise DeviceFlowError(f"Device code request failed: {data}")

        device_code = data["device_code"]
        user_code = data["user_code"]
        verification_uri = data["verification_uri"]
        interval = int(data.get("interval", 5))
        expires_in = int(data.get("expires_in", 900))

        # 2) Tell the user where to go and which code to enter.
        print(
            f"\n  {banner.BOLD}GitHub login{banner.RESET}\n"
            f"  Open {banner.CYAN}{verification_uri}{banner.RESET} in your browser and enter this code:\n\n"
            f"      {banner.BOLD}{banner.YELLOW}{user_code}{banner.RESET}\n\n"
            f"  {banner.DIM}Waiting for approval… (Ctrl-C to skip and continue with public data only){banner.RESET}"
        )

        # 3) Poll for the token until the user approves or the code expires.
        deadline = time.monotonic() + expires_in
        while time.monotonic() < deadline:
            time.sleep(interval)
            token_resp = http.post(
                ACCESS_TOKEN_URL,
                data={
                    "client_id": client_id,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
            )
            token_resp.raise_for_status()
            payload = token_resp.json()

            if "access_token" in payload:
                print(f"  {banner.GREEN}✓ Logged in.{banner.RESET}\n")
                return payload["access_token"]

            error = payload.get("error")
            if error == "authorization_pending":
                continue  # user hasn't finished yet
            if error == "slow_down":
                interval = int(payload.get("interval", interval + 5))
                continue
            if error == "expired_token":
                raise DeviceFlowError("The login code expired. Run the client again to retry.")
            if error == "access_denied":
                raise DeviceFlowError("Login was denied in the browser.")
            raise DeviceFlowError(f"Login failed: {payload}")

    raise DeviceFlowError("Timed out waiting for GitHub authorization.")
