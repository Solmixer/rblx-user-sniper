from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import json
import os
import threading
import time

import requests

HOST = "127.0.0.1"
PORT = 8000

ROBLOX_URL = "https://auth.roblox.com/v2/usernames/validate"
BIRTHDAY = "04/15/02"
CONTEXT = "Signup"

# One request every 0.10 seconds globally.
REQUEST_INTERVAL = 0.10

# Global cooldown after HTTP 429.
RATE_LIMIT_COOLDOWN = 15.0

rate_lock = threading.Lock()
next_request_time = 0.0
rate_pause_until = 0.0

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/153.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}


def wait_for_slot():
    global next_request_time

    while True:
        with rate_lock:
            now = time.monotonic()
            target = max(next_request_time, rate_pause_until)

            if now >= target:
                next_request_time = now + REQUEST_INTERVAL
                return

            delay = target - now

        time.sleep(min(delay, 0.05))


def activate_cooldown():
    global rate_pause_until

    with rate_lock:
        rate_pause_until = max(
            rate_pause_until,
            time.monotonic() + RATE_LIMIT_COOLDOWN,
        )


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)

    # Match the original Python program's network behavior.
    session.trust_env = False
    session.proxies.clear()

    return session


SESSION = make_session()


class Handler(SimpleHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(fmt % args)

    def send_json(self, status_code, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        try:
            self.send_response(status_code)
            self.send_header(
                "Content-Type",
                "application/json; charset=utf-8",
            )
            # Allow the HTML UI to call this local server even if the
            # HTML was opened from another local/preview origin.
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header(
                "Content-Length",
                str(len(raw)),
            )
            self.send_header(
                "Cache-Control",
                "no-store",
            )
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        parsed = urlparse(self.path)

        # ---------------------------------------------------------
        # Local server ping (does not contact Roblox)
        # ---------------------------------------------------------
        if parsed.path == "/api/ping":
            self.send_json(
                200,
                {
                    "ok": True,
                    "server": "roblox-username-checker",
                },
            )
            return

        # ---------------------------------------------------------
        # Local connection test
        # ---------------------------------------------------------
        if parsed.path == "/api/test":
            try:
                response = SESSION.get(
                    "https://auth.roblox.com/v2/metadata",
                    timeout=(10, 15),
                )

                self.send_json(
                    200,
                    {
                        "ok": True,
                        "status": response.status_code,
                        "contentType": response.headers.get(
                            "content-type",
                            "",
                        ),
                    },
                )

            except requests.RequestException as exc:
                self.send_json(
                    502,
                    {
                        "ok": False,
                        "error": str(exc),
                    },
                )

            return

        # ---------------------------------------------------------
        # Username validation
        # ---------------------------------------------------------
        if parsed.path == "/api/check":
            query = parse_qs(parsed.query)
            username = query.get("username", [""])[0]

            if not username:
                self.send_json(
                    400,
                    {
                        "ok": False,
                        "error": "Missing username",
                    },
                )
                return

            if len(username) > 20:
                self.send_json(
                    400,
                    {
                        "ok": False,
                        "error": "Username is too long",
                    },
                )
                return

            wait_for_slot()

            params = {
                "request.username": username,
                "request.birthday": BIRTHDAY,
                "request.context": CONTEXT,
            }

            try:
                response = SESSION.get(
                    ROBLOX_URL,
                    params=params,
                    timeout=(10, 20),
                )

                # IMPORTANT:
                # Always convert Roblox's response into JSON for the browser.
                # This prevents the frontend from getting an HTML error page
                # and reporting the vague "Invalid response" message.
                if response.status_code == 429:
                    activate_cooldown()

                    self.send_json(
                        429,
                        {
                            "ok": False,
                            "rateLimited": True,
                            "error": "Roblox HTTP 429 rate limit",
                        },
                    )
                    return

                if response.status_code >= 500:
                    self.send_json(
                        502,
                        {
                            "ok": False,
                            "error": (
                                f"Roblox HTTP {response.status_code}"
                            ),
                        },
                    )
                    return

                # Try JSON first.
                try:
                    data = response.json()
                except ValueError:
                    # Return enough diagnostic information to identify
                    # an HTML/Cloudflare/etc. response instead of hiding it.
                    content_type = response.headers.get(
                        "content-type",
                        "",
                    )
                    preview = response.text[:300].replace(
                        "\n",
                        " ",
                    )

                    self.send_json(
                        502,
                        {
                            "ok": False,
                            "error": (
                                "Roblox returned a non-JSON response "
                                f"(HTTP {response.status_code}, "
                                f"{content_type})."
                            ),
                            "preview": preview,
                        },
                    )
                    return

                message = str(
                    data.get("message", "")
                ).strip()

                if message == "Username is valid":
                    result = {
                        "ok": True,
                        "status": "AVAILABLE",
                        "message": message,
                    }

                elif message == "Username is already in use":
                    result = {
                        "ok": True,
                        "status": "TAKEN",
                        "message": message,
                    }

                else:
                    result = {
                        "ok": True,
                        "status": "UNAVAILABLE",
                        "message": (
                            message
                            or "No validation message returned"
                        ),
                    }

                self.send_json(200, result)

            except requests.exceptions.ConnectTimeout:
                self.send_json(
                    502,
                    {
                        "ok": False,
                        "error": (
                            "Could not connect to Roblox "
                            "(connection timeout)."
                        ),
                    },
                )

            except requests.exceptions.ReadTimeout:
                self.send_json(
                    502,
                    {
                        "ok": False,
                        "error": (
                            "Roblox connection timed out "
                            "while reading the response."
                        ),
                    },
                )

            except requests.exceptions.ConnectionError as exc:
                self.send_json(
                    502,
                    {
                        "ok": False,
                        "error": (
                            "Could not connect to Roblox: "
                            + str(exc),
                        ),
                    },
                )

            except requests.RequestException as exc:
                self.send_json(
                    502,
                    {
                        "ok": False,
                        "error": "Roblox request failed: " + str(exc),
                    },
                )

            except Exception as exc:
                self.send_json(
                    500,
                    {
                        "ok": False,
                        "error": "Server error: " + repr(exc),
                    },
                )

            return

        # Normal static-file handling for index.html.
        super().do_GET()


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    print()
    print("==========================================")
    print(" Roblox Username Checker Local Server")
    print("==========================================")
    print()
    print("Open EXACTLY:")
    print("  http://127.0.0.1:8000/index.html")
    print()
    print("Server ping:")
    print("  http://127.0.0.1:8000/api/ping")
    print()
    print("Do NOT double-click index.html.")
    print("Keep this window open while scanning.")
    print()

    server = ThreadingHTTPServer(
        (HOST, PORT),
        Handler,
    )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        server.server_close()
