"""
One-time setup wizard for strava2garmin.

Automates:
  - Strava OAuth flow  → refresh token
  - Garmin Connect login → cached token file
  - GitHub Gist creation with all three state files
  - GitHub Actions secrets (all 7) set via gh CLI

Prerequisites:
  - gh CLI installed and authenticated (run: gh auth login)
  - pip install -r requirements.txt already done
"""
import getpass
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import webbrowser

import requests
from garminconnect import Garmin

STRAVA_AUTH_URL = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
OAUTH_PORT = 8000
GARMIN_TOKEN_FILE = "./garmin_tokens.json"


# ── UI helpers ────────────────────────────────────────────────────────────────

def section(title: str) -> None:
    print(f"\n{'─' * 52}")
    print(f"  {title}")
    print(f"{'─' * 52}")

def ok(msg: str) -> None:
    print(f"  ✓ {msg}")

def fail(msg: str) -> None:
    print(f"  ✗ {msg}")
    sys.exit(1)


# ── Prerequisites ─────────────────────────────────────────────────────────────

def check_prerequisites() -> str:
    """Verify gh CLI is installed and authenticated. Returns the repo name."""
    section("Checking prerequisites")

    result = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    if result.returncode != 0:
        fail("gh CLI is not authenticated. Run: gh auth login --scopes gist")
    ok("gh CLI authenticated")

    result = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        fail("Not inside a GitHub repository, or no remote configured.")
    repo = json.loads(result.stdout).get("nameWithOwner", "unknown")
    ok(f"GitHub repo: {repo}")
    return repo


# ── Strava OAuth ──────────────────────────────────────────────────────────────

def get_strava_refresh_token(client_id: str, client_secret: str) -> str:
    """Run the Strava OAuth2 flow and return a refresh token."""
    redirect_uri = f"http://localhost:{OAUTH_PORT}"
    auth_code: list[str] = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            code = params.get("code", [None])[0]
            error = params.get("error", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            if code:
                auth_code.append(code)
                self.wfile.write(b"<h2>Authorization successful. You can close this tab.</h2>")
            else:
                self.wfile.write(f"<h2>Authorization failed: {error}. Close this tab.</h2>".encode())

        def log_message(self, *args: object) -> None:
            pass

    url = (
        f"{STRAVA_AUTH_URL}?client_id={client_id}"
        f"&redirect_uri={urllib.parse.quote(redirect_uri)}"
        f"&response_type=code&approval_prompt=auto&scope=activity:read_all"
    )

    server = http.server.HTTPServer(("localhost", OAUTH_PORT), _Handler)
    thread = threading.Thread(target=server.handle_request)
    thread.start()

    print(f"\n  Opening browser for Strava authorization...")
    webbrowser.open(url)
    thread.join()
    server.server_close()

    if not auth_code:
        fail("No authorization code received from Strava.")

    resp = requests.post(
        STRAVA_TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": auth_code[0],
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return str(resp.json()["refresh_token"])


# ── Garmin token caching ───────────────────────────────────────────────────────

def get_garmin_tokens(email: str, password: str) -> None:
    """Log in to Garmin Connect and cache OAuth tokens to GARMIN_TOKEN_FILE."""
    def _prompt_mfa() -> str:
        return input("  Garmin MFA/2FA code: ").strip()

    print(f"\n  Logging in to Garmin Connect as {email} ...")
    client = Garmin(email, password, prompt_mfa=_prompt_mfa)
    client.login(tokenstore=GARMIN_TOKEN_FILE)


# ── GitHub Gist ───────────────────────────────────────────────────────────────

def create_gist(refresh_token: str) -> str:
    """Create a secret Gist with the three state files. Returns the Gist ID."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "last_synced_id.txt"), "w") as f:
            f.write("0")
        with open(os.path.join(tmpdir, "strava_refresh_token.txt"), "w") as f:
            f.write(refresh_token)
        shutil.copy(GARMIN_TOKEN_FILE, os.path.join(tmpdir, "garmin_tokens.json"))

        result = subprocess.run(
            [
                "gh", "gist", "create",
                "last_synced_id.txt",
                "strava_refresh_token.txt",
                "garmin_tokens.json",
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
        )

    if result.returncode != 0:
        fail(f"Failed to create Gist: {result.stderr.strip()}")

    gist_url = result.stdout.strip()
    gist_id = gist_url.rstrip("/").split("/")[-1]
    ok(f"Gist created: {gist_url}")
    return gist_id


# ── GitHub Actions secrets ────────────────────────────────────────────────────

def set_secrets(secrets: dict[str, str]) -> None:
    """Set each key/value pair as a GitHub Actions repository secret."""
    for name, value in secrets.items():
        result = subprocess.run(
            ["gh", "secret", "set", name, "--body", value],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            ok(name)
        else:
            fail(f"{name}: {result.stderr.strip()}")


# ── .env writer ───────────────────────────────────────────────────────────────

def write_env(values: dict[str, str], path: str = ".env") -> None:
    if os.path.exists(path):
        answer = input(f"\n  {path} already exists. Overwrite? [y/N] ").strip().lower()
        if answer != "y":
            print("  Skipped writing .env")
            return
    with open(path, "w") as f:
        for key, value in values.items():
            f.write(f"{key}={value}\n")
    ok(f".env written")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("\n┌─────────────────────────────────────────┐")
    print("│       strava2garmin  setup wizard       │")
    print("└─────────────────────────────────────────┘")

    check_prerequisites()

    # ── Strava ────────────────────────────────────────────────────────────────
    section("Step 1 / 4  —  Strava")
    print("\n  Create your Strava API app at https://www.strava.com/settings/api")
    print("  Set Website = http://localhost  and  Callback Domain = localhost\n")
    strava_client_id = input("  Strava Client ID: ").strip()
    strava_client_secret = getpass.getpass("  Strava Client Secret: ")
    strava_refresh_token = get_strava_refresh_token(strava_client_id, strava_client_secret)
    ok("Strava refresh token obtained")

    # ── Garmin ────────────────────────────────────────────────────────────────
    section("Step 2 / 4  —  Garmin Connect")
    reuse_garmin = False
    if os.path.exists(GARMIN_TOKEN_FILE):
        answer = input(f"\n  {GARMIN_TOKEN_FILE} already exists. Reuse it? [Y/n] ").strip().lower()
        reuse_garmin = answer != "n"

    if reuse_garmin:
        ok("Reusing existing Garmin token file")
    else:
        garmin_email = input("\n  Garmin Connect email: ").strip()
        garmin_password = getpass.getpass("  Garmin Connect password: ")
        get_garmin_tokens(garmin_email, garmin_password)
        ok("Garmin tokens cached")
    garmin_email = input("  Garmin Connect email (for secrets): ").strip() if reuse_garmin else garmin_email
    garmin_password = getpass.getpass("  Garmin Connect password (for secrets): ") if reuse_garmin else garmin_password

    # ── Gist ──────────────────────────────────────────────────────────────────
    section("Step 3 / 4  —  GitHub Gist")
    gist_id = create_gist(strava_refresh_token)

    # ── Secrets ───────────────────────────────────────────────────────────────
    section("Step 4 / 4  —  GitHub Actions secrets")
    gh_pat = subprocess.run(
        ["gh", "auth", "token"], capture_output=True, text=True
    ).stdout.strip()

    secrets = {
        "STRAVA_CLIENT_ID": strava_client_id,
        "STRAVA_CLIENT_SECRET": strava_client_secret,
        "STRAVA_REFRESH_TOKEN": strava_refresh_token,
        "GARMIN_EMAIL": garmin_email,
        "GARMIN_PASSWORD": garmin_password,
        "GIST_ID": gist_id,
        "GH_PAT": gh_pat,
    }
    set_secrets(secrets)

    # ── .env ──────────────────────────────────────────────────────────────────
    write_env(secrets)

    print("\n┌─────────────────────────────────────────┐")
    print("│              Setup complete!            │")
    print("└─────────────────────────────────────────┘")
    print("\n  Go to: Actions → Sync swims to Garmin → Run workflow")
    print("  The first run will upload all your historical swims.\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nSetup cancelled.")
        sys.exit(1)
    except requests.exceptions.RequestException as e:
        print(f"\n  Network error: {e}")
        sys.exit(1)
