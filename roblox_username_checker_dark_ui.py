import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import itertools
import string
import requests
import threading
import concurrent.futures
import queue
import random
from urllib.parse import urlencode

OUTPUT_FILE = "Usernames.txt"

# Roblox's current documentation lists username validation on auth.roblox.com.
VALIDATE_URL = "https://auth.roblox.com/v2/usernames/validate"
METADATA_URL = "https://auth.roblox.com/v2/metadata"

# A date used only as context for Roblox's public username-validation endpoint.
# It is not tied to the user's account.
VALIDATION_BIRTHDAY = "04/15/02"
VALIDATION_CONTEXT = "Signup"

# Keep concurrency modest. More threads do not fix network/DNS failures.
MAX_WORKERS = 5
MAX_RETRIES = 2

running = False
checked_count = 0
available_count = 0
error_count = 0

state_lock = threading.Lock()
stop_event = threading.Event()
ui_queue = queue.Queue()
thread_local = threading.local()
file_lock = threading.Lock()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/153.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}


def queue_log(message):
    ui_queue.put(("log", message))


def queue_checked(value):
    ui_queue.put(("checked", value))


def queue_available(value):
    ui_queue.put(("available", value))


def queue_errors(value):
    ui_queue.put(("errors", value))


def queue_status(value):
    ui_queue.put(("status", value))


def process_ui_queue():
    try:
        while True:
            kind, value = ui_queue.get_nowait()

            if kind == "log":
                log_box.insert(tk.END, value + "\n")
                log_box.see(tk.END)

            elif kind == "checked":
                checked_label.config(text=f"Checked: {value:,}")

            elif kind == "available":
                found_label.config(text=f"Available: {value:,}")

            elif kind == "errors":
                error_label.config(text=f"Errors: {value:,}")

            elif kind == "status":
                status_label.config(text=value)

    except queue.Empty:
        pass

    root.after(100, process_ui_queue)


def get_session():
    """One Session per worker thread; don't share a Session between workers."""
    if not hasattr(thread_local, "session"):
        session = requests.Session()
        session.headers.update(HEADERS)

        # Prevent an unexpected HTTP(S)_PROXY environment variable from causing
        # every request to time out through a dead proxy.
        session.trust_env = False
        session.proxies.clear()

        thread_local.session = session

    return thread_local.session


def generate_usernames(length, include_numbers):
    chars = string.ascii_lowercase
    if include_numbers:
        chars += string.digits

    for combo in itertools.product(chars, repeat=length):
        if stop_event.is_set():
            return
        yield "".join(combo)


def save_username(username):
    with file_lock:
        with open(OUTPUT_FILE, "a", encoding="utf-8") as f:
            f.write(username + "\n")


def classify_validation_message(data):
    """Return a human-readable status from Roblox's validation JSON."""
    message = str(data.get("message", "")).strip()

    if message == "Username is valid":
        return "AVAILABLE", message

    if message == "Username is already in use":
        return "TAKEN", message

    if message:
        return "UNAVAILABLE", message

    return "UNKNOWN", "No validation message returned"


def check_username(username):
    global checked_count, available_count, error_count

    if stop_event.is_set():
        return

    session = get_session()

    params = {
        "request.username": username,
        "request.birthday": VALIDATION_BIRTHDAY,
        "request.context": VALIDATION_CONTEXT,
    }

    for attempt in range(MAX_RETRIES + 1):
        if stop_event.is_set():
            return

        try:
            response = session.get(
                VALIDATE_URL,
                params=params,
                timeout=(10, 20),
            )

            # Retry temporary server/rate-limit responses.
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt < MAX_RETRIES:
                    delay = (2 ** attempt) + random.uniform(0.25, 0.75)
                    queue_log(
                        f"{username} - HTTP {response.status_code}, retrying in {delay:.1f}s..."
                    )
                    if stop_event.wait(delay):
                        return
                    continue

                raise requests.HTTPError(
                    f"HTTP {response.status_code}",
                    response=response,
                )

            response.raise_for_status()
            data = response.json()
            status, message = classify_validation_message(data)

            if status == "AVAILABLE":
                with state_lock:
                    available_count += 1
                    available = available_count

                queue_available(available)
                queue_log(f"{username} - AVAILABLE")
                save_username(username)

            elif status == "TAKEN":
                queue_log(f"{username} - TAKEN")

            elif status == "UNAVAILABLE":
                queue_log(f"{username} - UNAVAILABLE ({message})")

            else:
                with state_lock:
                    error_count += 1
                    errors = error_count
                queue_errors(errors)
                queue_log(f"{username} - UNKNOWN RESPONSE: {message}")

            with state_lock:
                checked_count += 1
                checked = checked_count
            queue_checked(checked)
            return

        except requests.exceptions.ConnectTimeout:
            if attempt < MAX_RETRIES:
                delay = (2 ** attempt) + random.uniform(0.25, 0.75)
                queue_log(
                    f"{username} - CONNECT TIMEOUT, retrying in {delay:.1f}s..."
                )
                if stop_event.wait(delay):
                    return
                continue

            with state_lock:
                error_count += 1
                checked_count += 1
                errors = error_count
                checked = checked_count

            queue_errors(errors)
            queue_checked(checked)
            queue_log(
                f"{username} - CONNECT TIMEOUT. Check your DNS/proxy/firewall/network."
            )
            return

        except requests.exceptions.ReadTimeout:
            if attempt < MAX_RETRIES:
                delay = (2 ** attempt) + random.uniform(0.25, 0.75)
                queue_log(
                    f"{username} - READ TIMEOUT, retrying in {delay:.1f}s..."
                )
                if stop_event.wait(delay):
                    return
                continue

            with state_lock:
                error_count += 1
                checked_count += 1
                errors = error_count
                checked = checked_count

            queue_errors(errors)
            queue_checked(checked)
            queue_log(f"{username} - READ TIMEOUT")
            return

        except requests.exceptions.HTTPError as exc:
            with state_lock:
                error_count += 1
                checked_count += 1
                errors = error_count
                checked = checked_count

            queue_errors(errors)
            queue_checked(checked)
            queue_log(f"{username} - HTTP ERROR: {exc}")
            return

        except requests.exceptions.RequestException as exc:
            with state_lock:
                error_count += 1
                checked_count += 1
                errors = error_count
                checked = checked_count

            queue_errors(errors)
            queue_checked(checked)
            queue_log(f"{username} - NETWORK ERROR: {exc}")
            return

        except (ValueError, TypeError) as exc:
            with state_lock:
                error_count += 1
                checked_count += 1
                errors = error_count
                checked = checked_count

            queue_errors(errors)
            queue_checked(checked)
            queue_log(f"{username} - BAD API RESPONSE: {exc}")
            return

        except Exception as exc:
            with state_lock:
                error_count += 1
                checked_count += 1
                errors = error_count
                checked = checked_count

            queue_errors(errors)
            queue_checked(checked)
            queue_log(f"{username} - ERROR: {exc}")
            return


def test_connection():
    """Run before scanning so a dead Roblox connection is obvious."""
    queue_status("Testing Roblox connection...")
    queue_log("Testing connection to Roblox...")

    try:
        session = requests.Session()
        session.headers.update(HEADERS)
        session.trust_env = False
        session.proxies.clear()

        # We only need an HTTP response here to prove DNS + TCP + TLS work.
        response = session.get(METADATA_URL, timeout=(10, 15))

        queue_log(f"Connection test: HTTP {response.status_code}")
        queue_status("Connected to Roblox")
        return True

    except requests.exceptions.ConnectTimeout:
        queue_log("Connection test FAILED: connect timeout.")
        queue_log("Python could not establish HTTPS connection to Roblox.")
        queue_status("Roblox connection failed")
        return False

    except requests.exceptions.RequestException as exc:
        queue_log(f"Connection test FAILED: {exc}")
        queue_status("Roblox connection failed")
        return False


def scan():
    global running

    length = int(length_var.get())
    include_numbers = numbers_var.get()
    requested_workers = int(workers_var.get())
    workers = min(requested_workers, MAX_WORKERS)

    # Number of possible combinations.
    alphabet_size = 36 if include_numbers else 26
    total = alphabet_size ** length

    queue_log(
        f"Starting {length}-character scan: {total:,} possible combinations."
    )
    queue_log(f"Using {workers} worker threads.")

    if not test_connection():
        running = False
        queue_log("Scan not started because Roblox could not be reached.")
        return

    usernames = generate_usernames(length, include_numbers)
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    futures = set()
    max_in_flight = workers

    try:
        # Keep only a small, fixed number of requests in flight.
        for _ in range(max_in_flight):
            if stop_event.is_set():
                break
            try:
                username = next(usernames)
            except StopIteration:
                break
            futures.add(executor.submit(check_username, username))

        while futures and not stop_event.is_set():
            done, pending = concurrent.futures.wait(
                futures,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            futures = pending

            for _ in done:
                if stop_event.is_set():
                    break
                try:
                    username = next(usernames)
                except StopIteration:
                    break
                futures.add(executor.submit(check_username, username))

    finally:
        executor.shutdown(wait=False, cancel_futures=True)
        running = False

        if stop_event.is_set():
            queue_log("Scan stopped.")
            queue_status("Stopped")
        else:
            queue_log("Scan finished.")
            queue_status("Finished")


def start_scan():
    global running, checked_count, available_count, error_count

    if running:
        return

    checked_count = 0
    available_count = 0
    error_count = 0

    checked_label.config(text="Checked: 0")
    found_label.config(text="Available: 0")
    error_label.config(text="Errors: 0")
    status_label.config(text="Starting...")
    log_box.delete("1.0", tk.END)

    stop_event.clear()
    running = True

    threading.Thread(target=scan, daemon=True).start()
    queue_log("Started scan...")


def stop_scan():
    global running

    if not running:
        return

    stop_event.set()
    running = False
    queue_status("Stopping...")
    queue_log("Stopping scan...")


def on_close():
    stop_event.set()
    root.destroy()


# -----------------------------
# Modern dark UI
# -----------------------------

import math

BG = "#0a0f16"
PANEL = "#111925"
CARD = "#141e2b"
TEXT = "#eef4ff"
MUTED = "#8d9bb0"
BORDER = "#223044"
INPUT_BG = "#0c131d"
LOG_BG = "#080d13"
GREEN = "#18d56b"
GREEN_HOVER = "#2af17f"
RED = "#ff4d67"
RED_HOVER = "#ff6b80"
BLUE = "#4da3ff"
YELLOW = "#f6c85f"


def rounded_points(x1, y1, x2, y2, r):
    r = min(r, (x2 - x1) / 2, (y2 - y1) / 2)
    points = []

    for cx, cy, start in [
        (x2 - r, y1 + r, -90),
        (x2 - r, y2 - r, 0),
        (x1 + r, y2 - r, 90),
        (x1 + r, y1 + r, 180),
    ]:
        for i in range(8):
            angle = math.radians(start + i * 90 / 7)
            points.extend([
                cx + r * math.cos(angle),
                cy + r * math.sin(angle),
            ])

    return points


class GlowButton(tk.Canvas):
    """Rounded button with a simulated neon glow and hover effect."""

    def __init__(
        self,
        parent,
        text,
        command,
        accent,
        width=180,
        height=48,
    ):
        super().__init__(
            parent,
            width=width,
            height=height,
            bg=parent.cget("bg"),
            highlightthickness=0,
            bd=0,
        )

        self.command = command
        self.accent = accent
        self.width = width
        self.height = height
        self.hovered = False
        self.pressed = False
        self.text = text

        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)

        self._draw()

    def _draw(self):
        self.delete("all")

        x1, y1 = 8, 8
        x2, y2 = self.width - 8, self.height - 8
        radius = 15

        if self.accent == GREEN:
            hover = GREEN_HOVER
            glow_1 = "#102919"
            glow_2 = "#153a24"
        else:
            hover = RED_HOVER
            glow_1 = "#2b1118"
            glow_2 = "#431924"

        active = hover if self.hovered else self.accent

        if self.pressed:
            active = "#ffffff"

        # Outer glow.
        self.create_polygon(
            rounded_points(x1 - 4, y1 - 4, x2 + 4, y2 + 4, radius + 4),
            fill="",
            outline=glow_1,
            width=5,
        )
        self.create_polygon(
            rounded_points(x1 - 2, y1 - 2, x2 + 2, y2 + 2, radius + 2),
            fill="",
            outline=glow_2,
            width=3,
        )

        # Main rounded button.
        self.create_polygon(
            rounded_points(x1, y1, x2, y2, radius),
            fill=active if not self.pressed else PANEL,
            outline=active,
            width=2,
        )

        self.create_text(
            self.width / 2,
            self.height / 2,
            text=self.text,
            fill=TEXT if not self.pressed else active,
            font=("Segoe UI", 10, "bold"),
        )

    def _enter(self, _event=None):
        self.hovered = True
        self._draw()

    def _leave(self, _event=None):
        self.hovered = False
        self.pressed = False
        self._draw()

    def _press(self, _event=None):
        self.pressed = True
        self._draw()

    def _release(self, event=None):
        was_pressed = self.pressed
        self.pressed = False
        self._draw()

        if (
            was_pressed
            and event is not None
            and 0 <= event.x <= self.width
            and 0 <= event.y <= self.height
        ):
            self.command()


class StatCard(tk.Frame):
    def __init__(self, parent, title, value, accent):
        super().__init__(
            parent,
            bg=CARD,
            highlightbackground=BORDER,
            highlightthickness=1,
            bd=0,
        )

        self.grid_columnconfigure(0, weight=1)

        tk.Label(
            self,
            text=title.upper(),
            bg=CARD,
            fg=MUTED,
            font=("Segoe UI", 8, "bold"),
        ).grid(
            row=0,
            column=0,
            sticky="w",
            padx=14,
            pady=(11, 2),
        )

        self.value_label = tk.Label(
            self,
            text=value,
            bg=CARD,
            fg=accent,
            font=("Segoe UI", 18, "bold"),
        )
        self.value_label.grid(
            row=1,
            column=0,
            sticky="w",
            padx=14,
            pady=(0, 11),
        )

    def set_value(self, value):
        self.value_label.config(text=value)


root = tk.Tk()
root.title("Roblox Username Checker")
root.geometry("1080x720")
root.minsize(900, 600)
root.configure(bg=BG)
root.protocol("WM_DELETE_WINDOW", on_close)

root.grid_columnconfigure(0, weight=1)
root.grid_rowconfigure(4, weight=1)

# -----------------------------
# ttk dark styling
# -----------------------------

style = ttk.Style(root)

try:
    style.theme_use("clam")
except tk.TclError:
    pass

style.configure(
    "Dark.TCombobox",
    fieldbackground=INPUT_BG,
    background=INPUT_BG,
    foreground=TEXT,
    arrowcolor=MUTED,
    bordercolor=BORDER,
    lightcolor=BORDER,
    darkcolor=BORDER,
    padding=7,
)

style.map(
    "Dark.TCombobox",
    fieldbackground=[("readonly", INPUT_BG)],
    foreground=[("readonly", TEXT)],
    bordercolor=[("focus", BLUE)],
)

# -----------------------------
# Header
# -----------------------------

header = tk.Frame(root, bg=BG)
header.grid(
    row=0,
    column=0,
    sticky="ew",
    padx=28,
    pady=(24, 12),
)
header.grid_columnconfigure(0, weight=1)

tk.Label(
    header,
    text="ROBLOX USERNAME CHECKER",
    bg=BG,
    fg=TEXT,
    font=("Segoe UI", 22, "bold"),
).grid(row=0, column=0, sticky="w")

tk.Label(
    header,
    text="Fast username validation  •  Clean results  •  Live status",
    bg=BG,
    fg=MUTED,
    font=("Segoe UI", 9),
).grid(row=1, column=0, sticky="w", pady=(3, 0))

status_pill = tk.Frame(
    header,
    bg=GREEN,
    bd=0,
)
status_pill.grid(
    row=0,
    column=1,
    rowspan=2,
    sticky="e",
    padx=(16, 0),
)

status_label = tk.Label(
    status_pill,
    text="  READY  ",
    bg=GREEN,
    fg="#06110a",
    font=("Segoe UI", 8, "bold"),
)
status_label.pack(padx=7, pady=5)

# -----------------------------
# Settings panel
# -----------------------------

settings = tk.Frame(
    root,
    bg=PANEL,
    highlightbackground=BORDER,
    highlightthickness=1,
    bd=0,
)
settings.grid(
    row=1,
    column=0,
    sticky="ew",
    padx=28,
    pady=8,
)

settings.grid_columnconfigure(3, weight=1)

tk.Label(
    settings,
    text="SCAN SETTINGS",
    bg=PANEL,
    fg=MUTED,
    font=("Segoe UI", 8, "bold"),
).grid(
    row=0,
    column=0,
    columnspan=5,
    sticky="w",
    padx=16,
    pady=(12, 8),
)


def add_field(parent, label, variable, values, column):
    tk.Label(
        parent,
        text=label,
        bg=PANEL,
        fg=TEXT,
        font=("Segoe UI", 9, "bold"),
    ).grid(
        row=1,
        column=column,
        sticky="w",
        padx=(16 if column == 0 else 10, 6),
        pady=(0, 5),
    )

    combo = ttk.Combobox(
        parent,
        textvariable=variable,
        values=values,
        state="readonly",
        width=10,
        style="Dark.TCombobox",
    )
    combo.grid(
        row=2,
        column=column,
        sticky="w",
        padx=(16 if column == 0 else 10, 6),
        pady=(0, 16),
    )

    return combo


length_var = tk.StringVar(value="3")
length_box = add_field(
    settings,
    "Length",
    length_var,
    ["3", "4", "5"],
    0,
)

workers_var = tk.StringVar(value="5")
workers_box = add_field(
    settings,
    "Threads",
    workers_var,
    ["1", "2", "3", "5"],
    1,
)

# Include numbers.
toggle_frame = tk.Frame(settings, bg=PANEL)
toggle_frame.grid(
    row=1,
    column=2,
    rowspan=2,
    sticky="w",
    padx=14,
    pady=(4, 0),
)

numbers_var = tk.BooleanVar(value=True)

tk.Checkbutton(
    toggle_frame,
    text="Include Numbers",
    variable=numbers_var,
    bg=PANEL,
    fg=TEXT,
    activebackground=PANEL,
    activeforeground=TEXT,
    selectcolor=INPUT_BG,
    highlightthickness=0,
    bd=0,
    font=("Segoe UI", 9, "bold"),
).pack(anchor="w", pady=(11, 0))

tk.Label(
    toggle_frame,
    text="a–z + 0–9",
    bg=PANEL,
    fg=MUTED,
    font=("Segoe UI", 8),
).pack(anchor="w", pady=(2, 0))

# Buttons.
button_frame = tk.Frame(settings, bg=PANEL)
button_frame.grid(
    row=1,
    column=4,
    rowspan=2,
    sticky="e",
    padx=14,
    pady=12,
)

start_button = GlowButton(
    button_frame,
    "START SCAN",
    start_scan,
    GREEN,
    width=150,
    height=44,
)
start_button.pack(side=tk.LEFT, padx=5)

stop_button = GlowButton(
    button_frame,
    "STOP",
    stop_scan,
    RED,
    width=110,
    height=44,
)
stop_button.pack(side=tk.LEFT, padx=5)

# -----------------------------
# Statistics
# -----------------------------

stats_frame = tk.Frame(root, bg=BG)
stats_frame.grid(
    row=2,
    column=0,
    sticky="ew",
    padx=28,
    pady=10,
)

for i in range(3):
    stats_frame.grid_columnconfigure(i, weight=1)

checked_card = StatCard(stats_frame, "Checked", "0", BLUE)
checked_card.grid(row=0, column=0, sticky="ew", padx=(0, 6))

found_card = StatCard(stats_frame, "Available", "0", GREEN)
found_card.grid(row=0, column=1, sticky="ew", padx=6)

error_card = StatCard(stats_frame, "Errors", "0", RED)
error_card.grid(row=0, column=2, sticky="ew", padx=(6, 0))

checked_label = checked_card.value_label
found_label = found_card.value_label
error_label = error_card.value_label

# -----------------------------
# Log header
# -----------------------------

log_header = tk.Frame(root, bg=BG)
log_header.grid(
    row=3,
    column=0,
    sticky="ew",
    padx=28,
    pady=(2, 5),
)
log_header.grid_columnconfigure(0, weight=1)

tk.Label(
    log_header,
    text="LIVE RESULTS",
    bg=BG,
    fg=TEXT,
    font=("Segoe UI", 10, "bold"),
).grid(row=0, column=0, sticky="w")

tk.Label(
    log_header,
    text="AVAILABLE  •  TAKEN  •  ERRORS",
    bg=BG,
    fg=MUTED,
    font=("Segoe UI", 8, "bold"),
).grid(row=0, column=1, sticky="e")

# -----------------------------
# Log area
# -----------------------------

log_frame = tk.Frame(
    root,
    bg=PANEL,
    highlightbackground=BORDER,
    highlightthickness=1,
    bd=0,
)
log_frame.grid(
    row=4,
    column=0,
    sticky="nsew",
    padx=28,
    pady=(0, 10),
)
log_frame.grid_columnconfigure(0, weight=1)
log_frame.grid_rowconfigure(0, weight=1)

log_box = scrolledtext.ScrolledText(
    log_frame,
    bg=LOG_BG,
    fg="#c9d4e5",
    insertbackground=TEXT,
    selectbackground="#23344b",
    selectforeground=TEXT,
    relief="flat",
    bd=0,
    highlightthickness=0,
    font=("Consolas", 9),
    padx=14,
    pady=12,
    wrap=tk.NONE,
)
log_box.grid(
    row=0,
    column=0,
    sticky="nsew",
    padx=1,
    pady=1,
)

log_box.tag_config("available", foreground=GREEN)
log_box.tag_config("taken", foreground="#8190a5")
log_box.tag_config("error", foreground=RED)
log_box.tag_config("rate", foreground=YELLOW)
log_box.tag_config("info", foreground=BLUE)


# Replace the queue processor with a color-aware version.
def process_ui_queue():
    try:
        while True:
            kind, value = ui_queue.get_nowait()

            if kind == "log":
                message = str(value)

                if "AVAILABLE" in message:
                    tag = "available"
                elif "TAKEN" in message:
                    tag = "taken"
                elif (
                    "TIMEOUT" in message
                    or "ERROR" in message
                    or "FAILED" in message
                ):
                    tag = "error"
                elif "RATE LIMITED" in message or "HTTP 429" in message:
                    tag = "rate"
                else:
                    tag = "info"

                log_box.insert(
                    tk.END,
                    message + "\n",
                    tag,
                )
                log_box.see(tk.END)

            elif kind == "checked":
                checked_label.config(text=f"{value:,}")

            elif kind == "available":
                found_label.config(text=f"{value:,}")

            elif kind == "errors":
                error_label.config(text=f"{value:,}")

            elif kind == "status":
                status_text = str(value).lower()

                status_label.config(
                    text=f"  {str(value).upper()}  "
                )

                if (
                    "connected" in status_text
                    or "ready" in status_text
                    or "finished" in status_text
                ):
                    status_pill.config(bg=GREEN)
                    status_label.config(
                        bg=GREEN,
                        fg="#06110a",
                    )

                elif (
                    "stopp" in status_text
                    or "fail" in status_text
                ):
                    status_pill.config(bg=RED)
                    status_label.config(
                        bg=RED,
                        fg="#19060a",
                    )

                elif (
                    "start" in status_text
                    or "test" in status_text
                ):
                    status_pill.config(bg=YELLOW)
                    status_label.config(
                        bg=YELLOW,
                        fg="#171105",
                    )

                else:
                    status_pill.config(bg=BLUE)
                    status_label.config(
                        bg=BLUE,
                        fg="#06111a",
                    )

    except queue.Empty:
        pass

    root.after(80, process_ui_queue)


root.after(80, process_ui_queue)
root.mainloop()
