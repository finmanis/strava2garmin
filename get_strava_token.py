"""One-time script to obtain a Strava refresh token via OAuth2."""
import http.server
import os
import threading
import urllib.parse
import webbrowser

import requests
from dotenv import load_dotenv

load_dotenv()

PORT = 8000
REDIRECT_URI = f"http://localhost:{PORT}"
AUTH_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"

client_id = os.environ["STRAVA_CLIENT_ID"]
client_secret = os.environ["STRAVA_CLIENT_SECRET"]

auth_code: list[str] = []  # mutable container so the handler can write to it


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        code = params.get("code", [None])[0]
        error = params.get("error", [None])[0]

        if error:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>Authorization denied. Close this tab.</h2>")
        elif code:
            auth_code.append(code)
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>Authorization successful! You can close this tab.</h2>")
        else:
            self.send_response(400)
            self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass  # silence server request logs


def main() -> None:
    url = (
        f"{AUTH_URL}"
        f"?client_id={client_id}"
        f"&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
        f"&response_type=code"
        f"&approval_prompt=auto"
        f"&scope=activity:read_all"
    )

    server = http.server.HTTPServer(("localhost", PORT), CallbackHandler)
    thread = threading.Thread(target=server.handle_request)
    thread.start()

    print(f"Opening browser for Strava authorization...")
    webbrowser.open(url)
    print(f"Waiting for callback on {REDIRECT_URI} ...")

    thread.join()
    server.server_close()

    if not auth_code:
        print("No authorization code received. Aborting.")
        return

    response = requests.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": auth_code[0],
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()

    print("\n--- Strava tokens ---")
    print(f"Access token:  {data['access_token']}")
    print(f"Refresh token: {data['refresh_token']}")
    print(f"Expires at:    {data['expires_at']}")
    print("\nAdd STRAVA_REFRESH_TOKEN to your .env and GitHub secrets:")
    print(f"  STRAVA_REFRESH_TOKEN={data['refresh_token']}")


if __name__ == "__main__":
    main()
