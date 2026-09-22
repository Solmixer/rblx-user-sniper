import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import itertools
import string
import requests
import threading
import concurrent.futures
import queue
import time
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
# Keep normal traffic reasonably fast while still leaving space between requests.
MAX_WORKERS = 3
MAX_RETRIES = 1
REQUEST_INTERVAL = 0.10

# Fixed global cooldown after HTTP 429.
RATE_LIMIT_COOLDOWN = 15.0

rate_lock = threading.Lock()
next_request_time = 0.0
rate_pause_until = 0.0

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


def wait_for_request_slot():
    """Allow one request every REQUEST_INTERVAL seconds globally."""
    global next_request_time

    while not stop_event.is_set():
        with rate_lock:
            now = time.monotonic()
            delay = max(0.0, next_request_time - now)

            if delay <= 0:
                next_request_time = now + REQUEST_INTERVAL
                return True

        if stop_event.wait(min(delay, 1.0)):
            return False

    return False


def wait_for_rate_pause():
    """Wait for the global cooldown created by an HTTP 429 response."""
    while not stop_event.is_set():
        with rate_lock:
            delay = max(0.0, rate_pause_until - time.monotonic())

        if delay <= 0:
            return True

        if stop_event.wait(min(delay, 1.0)):
            return False

    return False


def handle_rate_limit(response):
    """Pause every worker for exactly 15 seconds after HTTP 429."""
    global rate_pause_until

    with rate_lock:
        rate_pause_until = max(
            rate_pause_until,
            time.monotonic() + RATE_LIMIT_COOLDOWN,
        )

    queue_status("Rate limited - waiting 15s")
    queue_log(
        "HTTP 429 received. Pausing all requests for 15 seconds..."
    )

    return RATE_LIMIT_COOLDOWN


def relax_request_interval():
    """Compatibility helper; normal pacing is fixed."""
    return


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

        # Global cooldown first, then global request spacing.
        if not wait_for_rate_pause():
            return

        if not wait_for_request_slot():
            return

        try:
            response = session.get(
                VALIDATE_URL,
                params=params,
                timeout=(10, 20),
            )

            # ---------------------------------
            # HTTP 429: global cooldown
            # ---------------------------------
            if response.status_code == 429:
                handle_rate_limit(response)

                if attempt < MAX_RETRIES:
                    # All workers share the same 15-second cooldown.
                    continue

                with state_lock:
                    checked_count += 1
                    checked = checked_count

                queue_checked(checked)
                queue_log(
                    f"{username} - RATE LIMITED; skipped after retry"
                )
                return

            # ---------------------------------
            # Temporary 5xx server response
            # ---------------------------------
            if 500 <= response.status_code < 600:
                if attempt < MAX_RETRIES:
                    delay = min(
                        30.0,
                        (2 ** attempt) + random.uniform(0.5, 1.5),
                    )

                    queue_log(
                        f"{username} - HTTP {response.status_code}, "
                        f"retrying in {delay:.1f}s..."
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
                queue_log(
                    f"{username} - UNAVAILABLE ({message})"
                )

            else:
                with state_lock:
                    error_count += 1
                    errors = error_count

                queue_errors(errors)
                queue_log(
                    f"{username} - UNKNOWN RESPONSE: {message}"
                )

            relax_request_interval()

            with state_lock:
                checked_count += 1
                checked = checked_count

            queue_checked(checked)
            queue_status("Scanning")
            return

        except requests.exceptions.ConnectTimeout:
            if attempt < MAX_RETRIES:
                delay = min(
                    30.0,
                    2.0 + (2 ** attempt),
                )

                queue_log(
                    f"{username} - CONNECT TIMEOUT, "
                    f"retrying in {delay:.1f}s..."
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
                f"{username} - CONNECT TIMEOUT. "
                f"Check DNS/firewall/network."
            )
            return

        except requests.exceptions.ReadTimeout:
            if attempt < MAX_RETRIES:
                delay = min(
                    30.0,
                    2.0 + (2 ** attempt),
                )

                queue_log(
                    f"{username} - READ TIMEOUT, "
                    f"retrying in {delay:.1f}s..."
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
                f"{username} - READ TIMEOUT"
            )
            return

        except requests.exceptions.HTTPError as exc:
            with state_lock:
                error_count += 1
                checked_count += 1
                errors = error_count
                checked = checked_count

            queue_errors(errors)
            queue_checked(checked)
            queue_log(
                f"{username} - HTTP ERROR: {exc}"
            )
            return

        except requests.exceptions.RequestException as exc:
            with state_lock:
                error_count += 1
                checked_count += 1
                errors = error_count
                checked = checked_count

            queue_errors(errors)
            queue_checked(checked)
            queue_log(
                f"{username} - NETWORK ERROR: {exc}"
            )
            return

        except (ValueError, TypeError) as exc:
            with state_lock:
                error_count += 1
                checked_count += 1
                errors = error_count
                checked = checked_count

            queue_errors(errors)
            queue_checked(checked)
            queue_log(
                f"{username} - BAD API RESPONSE: {exc}"
            )
            return

        except Exception as exc:
            with state_lock:
                error_count += 1
                checked_count += 1
                errors = error_count
                checked = checked_count

            queue_errors(errors)
            queue_checked(checked)
            queue_log(
                f"{username} - ERROR: {exc}"
            )
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

    global next_request_time, rate_pause_until

    checked_count = 0
    available_count = 0
    error_count = 0

    with rate_lock:
        next_request_time = 0.0
        rate_pause_until = 0.0

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
# Clean modern rounded UI
# -----------------------------

import math

BG = "#080c13"
SURFACE = "#0f1724"
SURFACE_2 = "#131d2b"
INPUT_BG = "#0b121d"
LOG_BG = "#070b11"
TEXT = "#f3f7fc"
MUTED = "#8390a5"
BORDER = "#1c2a3c"

GREEN = "#27e47b"
GREEN_HOVER = "#42f18d"
GREEN_GLOW = "#15432d"

RED = "#ff5871"
RED_HOVER = "#ff7086"
RED_GLOW = "#451923"

BLUE = "#5ba9ff"
YELLOW = "#f6cf67"


def rounded_polygon_points(x1, y1, x2, y2, radius, steps=10):
    radius = min(radius, (x2 - x1) / 2, (y2 - y1) / 2)
    points = []

    corners = (
        (x2 - radius, y1 + radius, -90),
        (x2 - radius, y2 - radius, 0),
        (x1 + radius, y2 - radius, 90),
        (x1 + radius, y1 + radius, 180),
    )

    for cx, cy, start_angle in corners:
        for i in range(steps):
            angle = math.radians(
                start_angle + i * 90 / (steps - 1)
            )
            points.extend(
                (
                    cx + radius * math.cos(angle),
                    cy + radius * math.sin(angle),
                )
            )

    return points


class RoundedSurface(tk.Frame):
    """A simple rounded card using a canvas background."""

    def __init__(
        self,
        parent,
        bg_color=SURFACE,
        radius=24,
        border=BORDER,
        border_width=1,
        padding=1,
        **kwargs,
    ):
        outer_bg = parent.cget("bg")

        super().__init__(
            parent,
            bg=outer_bg,
            bd=0,
            highlightthickness=0,
            **kwargs,
        )

        self.radius = radius
        self.bg_color = bg_color
        self.border = border
        self.border_width = border_width
        self.padding = padding

        self.canvas = tk.Canvas(
            self,
            bg=outer_bg,
            bd=0,
            highlightthickness=0,
        )
        self.canvas.place(
            x=0,
            y=0,
            relwidth=1,
            relheight=1,
        )

        self.inner = tk.Frame(
            self,
            bg=bg_color,
            bd=0,
            highlightthickness=0,
        )
        self.inner.place(
            x=padding,
            y=padding,
            relwidth=1,
            relheight=1,
            width=-padding * 2,
            height=-padding * 2,
        )

        self.bind("<Configure>", self._redraw)

    def _redraw(self, event):
        self.canvas.delete("all")

        width = max(2, event.width)
        height = max(2, event.height)

        self.canvas.create_polygon(
            rounded_polygon_points(
                1,
                1,
                width - 1,
                height - 1,
                self.radius,
            ),
            fill=self.bg_color,
            outline=self.border,
            width=self.border_width,
        )

        self.canvas.lower("all")


class GlowButton(tk.Canvas):
    """Rounded neon button with hover and press states."""

    def __init__(
        self,
        parent,
        text,
        command,
        accent,
        width=170,
        height=50,
    ):
        super().__init__(
            parent,
            width=width,
            height=height,
            bg=parent.cget("bg"),
            bd=0,
            highlightthickness=0,
        )

        self.text = text
        self.command = command
        self.accent = accent
        self.width = width
        self.height = height
        self.hovered = False
        self.pressed = False

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

        self._draw()

    def _draw(self):
        self.delete("all")

        x1, y1 = 10, 10
        x2, y2 = self.width - 10, self.height - 10
        radius = 19

        if self.accent == GREEN:
            hover_color = GREEN_HOVER
            glow_1 = "#0b1d14"
            glow_2 = GREEN_GLOW
        else:
            hover_color = RED_HOVER
            glow_1 = "#210c12"
            glow_2 = RED_GLOW

        color = hover_color if self.hovered else self.accent

        if self.pressed:
            color = "#ffffff"

        # Two subtle glow rings.
        self.create_polygon(
            rounded_polygon_points(
                x1 - 5,
                y1 - 5,
                x2 + 5,
                y2 + 5,
                radius + 5,
            ),
            fill="",
            outline=glow_1,
            width=4,
        )

        self.create_polygon(
            rounded_polygon_points(
                x1 - 2,
                y1 - 2,
                x2 + 2,
                y2 + 2,
                radius + 2,
            ),
            fill="",
            outline=glow_2,
            width=3,
        )

        self.create_polygon(
            rounded_polygon_points(
                x1,
                y1,
                x2,
                y2,
                radius,
            ),
            fill=color if not self.pressed else SURFACE_2,
            outline=color,
            width=2,
        )

        self.create_text(
            self.width / 2,
            self.height / 2,
            text=self.text,
            fill=TEXT if not self.pressed else color,
            font=("Segoe UI", 10, "bold"),
        )

    def _on_enter(self, _event=None):
        self.hovered = True
        self._draw()

    def _on_leave(self, _event=None):
        self.hovered = False
        self.pressed = False
        self._draw()

    def _on_press(self, _event=None):
        self.pressed = True
        self._draw()

    def _on_release(self, event=None):
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


class PillToggle(tk.Canvas):
    """Compact iOS-style toggle for Include Numbers."""

    def __init__(self, parent, variable):
        super().__init__(
            parent,
            width=46,
            height=26,
            bg=parent.cget("bg"),
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )

        self.variable = variable
        self.bind("<Button-1>", self._toggle)
        self._draw()

    def _toggle(self, _event=None):
        self.variable.set(not self.variable.get())
        self._draw()

    def _draw(self):
        self.delete("all")

        enabled = bool(self.variable.get())

        track_color = GREEN if enabled else "#2b3748"
        knob_x = 33 if enabled else 13

        self.create_polygon(
            rounded_polygon_points(
                2,
                2,
                44,
                24,
                11,
                steps=8,
            ),
            fill=track_color,
            outline="",
        )

        self.create_oval(
            knob_x - 8,
            5,
            knob_x + 8,
            21,
            fill="#ffffff",
            outline="",
        )


class StatCard(RoundedSurface):
    def __init__(self, parent, title, value, accent):
        super().__init__(
            parent,
            bg_color=SURFACE,
            radius=22,
            border=BORDER,
            border_width=1,
            padding=1,
        )

        self.inner.grid_columnconfigure(0, weight=1)

        top = tk.Frame(
            self.inner,
            bg=SURFACE,
        )
        top.grid(
            row=0,
            column=0,
            sticky="ew",
            padx=16,
            pady=(13, 0),
        )

        tk.Label(
            top,
            text="●",
            bg=SURFACE,
            fg=accent,
            font=("Segoe UI", 7),
        ).pack(side=tk.LEFT, padx=(0, 6))

        tk.Label(
            top,
            text=title.upper(),
            bg=SURFACE,
            fg=MUTED,
            font=("Segoe UI", 8, "bold"),
        ).pack(side=tk.LEFT)

        self.value_label = tk.Label(
            self.inner,
            text=value,
            bg=SURFACE,
            fg=accent,
            font=("Segoe UI", 22, "bold"),
        )
        self.value_label.grid(
            row=1,
            column=0,
            sticky="w",
            padx=16,
            pady=(2, 13),
        )

    def set_value(self, value):
        self.value_label.config(text=value)


# -----------------------------
# Root
# -----------------------------

root = tk.Tk()
root.title("Roblox Username Checker")
root.geometry("1100x740")
root.minsize(920, 620)
root.configure(bg=BG)
root.protocol("WM_DELETE_WINDOW", on_close)

root.grid_columnconfigure(0, weight=1)
root.grid_rowconfigure(4, weight=1)

# -----------------------------
# Header
# -----------------------------

header = tk.Frame(root, bg=BG)
header.grid(
    row=0,
    column=0,
    sticky="ew",
    padx=30,
    pady=(25, 14),
)
header.grid_columnconfigure(0, weight=1)

title_wrap = tk.Frame(header, bg=BG)
title_wrap.grid(row=0, column=0, sticky="w")

tk.Label(
    title_wrap,
    text="Roblox Username Checker",
    bg=BG,
    fg=TEXT,
    font=("Segoe UI", 24, "bold"),
).pack(anchor="w")

tk.Label(
    title_wrap,
    text="Simple • Fast • Live username validation",
    bg=BG,
    fg=MUTED,
    font=("Segoe UI", 10),
).pack(anchor="w", pady=(2, 0))

# Status pill.
status_wrap = tk.Frame(header, bg=BG)
status_wrap.grid(
    row=0,
    column=1,
    sticky="e",
    padx=(20, 0),
)

status_label = tk.Label(
    status_wrap,
    text="  ●  READY  ",
    bg=SURFACE_2,
    fg=GREEN,
    font=("Segoe UI", 9, "bold"),
    padx=9,
    pady=7,
)
status_label.pack()

# -----------------------------
# Settings
# -----------------------------

settings = RoundedSurface(
    root,
    bg_color=SURFACE,
    radius=26,
    border=BORDER,
    border_width=1,
    padding=1,
    height=154,
)
settings.grid(
    row=1,
    column=0,
    sticky="ew",
    padx=30,
    pady=7,
)
settings.inner.grid_columnconfigure(3, weight=1)

tk.Label(
    settings.inner,
    text="SCAN SETTINGS",
    bg=SURFACE,
    fg=MUTED,
    font=("Segoe UI", 8, "bold"),
).grid(
    row=0,
    column=0,
    columnspan=5,
    sticky="w",
    padx=21,
    pady=(15, 9),
)

# Reusable pill-style field.
def make_pill_field(parent, label, variable, values, column):
    tk.Label(
        parent,
        text=label,
        bg=SURFACE,
        fg=TEXT,
        font=("Segoe UI", 9, "bold"),
    ).grid(
        row=1,
        column=column,
        sticky="w",
        padx=(21 if column == 0 else 8, 6),
    )

    field = tk.Frame(
        parent,
        bg=INPUT_BG,
        bd=0,
        highlightthickness=0,
        width=112,
        height=38,
    )
    field.grid(
        row=2,
        column=column,
        sticky="w",
        padx=(21 if column == 0 else 8, 6),
        pady=(5, 15),
    )
    field.pack_propagate(False)

    combo = ttk.Combobox(
        field,
        textvariable=variable,
        values=values,
        state="readonly",
        width=9,
    )
    combo.pack(
        fill="both",
        expand=True,
        padx=8,
        pady=5,
    )

    return combo


# Dark ttk style.
style = ttk.Style(root)

try:
    style.theme_use("clam")
except tk.TclError:
    pass

style.configure(
    "Modern.TCombobox",
    fieldbackground=INPUT_BG,
    background=INPUT_BG,
    foreground=TEXT,
    arrowcolor=MUTED,
    borderwidth=0,
    relief="flat",
    padding=6,
)

style.map(
    "Modern.TCombobox",
    fieldbackground=[
        ("readonly", INPUT_BG),
        ("focus", INPUT_BG),
    ],
    foreground=[
        ("readonly", TEXT),
        ("focus", TEXT),
    ],
)

# Reconfigure the style after creation.
style.configure(
    "TCombobox",
    fieldbackground=INPUT_BG,
    background=INPUT_BG,
    foreground=TEXT,
    arrowcolor=MUTED,
    borderwidth=0,
    relief="flat",
    padding=6,
)

length_var = tk.StringVar(value="3")
length_box = make_pill_field(
    settings.inner,
    "Length",
    length_var,
    ["3", "4", "5"],
    0,
)
length_box.configure(style="TCombobox")

workers_var = tk.StringVar(value="5")
workers_box = make_pill_field(
    settings.inner,
    "Threads",
    workers_var,
    ["1", "2", "3"],
    1,
)
workers_box.configure(style="TCombobox")

# Toggle.
numbers_var = tk.BooleanVar(value=True)

toggle_wrap = tk.Frame(
    settings.inner,
    bg=SURFACE,
)
toggle_wrap.grid(
    row=1,
    column=2,
    rowspan=2,
    sticky="w",
    padx=18,
)

toggle_row = tk.Frame(
    toggle_wrap,
    bg=SURFACE,
)
toggle_row.pack(anchor="w", pady=(18, 0))

numbers_toggle = PillToggle(
    toggle_row,
    numbers_var,
)
numbers_toggle.pack(side=tk.LEFT)

tk.Label(
    toggle_row,
    text="Include Numbers",
    bg=SURFACE,
    fg=TEXT,
    font=("Segoe UI", 9, "bold"),
).pack(side=tk.LEFT, padx=(9, 0))

tk.Label(
    toggle_wrap,
    text="letters + numbers",
    bg=SURFACE,
    fg=MUTED,
    font=("Segoe UI", 8),
).pack(anchor="w", padx=(55, 0), pady=(2, 0))

# Buttons.
button_wrap = tk.Frame(
    settings.inner,
    bg=SURFACE,
)
button_wrap.grid(
    row=1,
    column=4,
    rowspan=2,
    sticky="e",
    padx=(8, 18),
    pady=9,
)

start_button = GlowButton(
    button_wrap,
    "START SCAN",
    start_scan,
    GREEN,
    width=158,
    height=50,
)
start_button.pack(side=tk.LEFT, padx=4)

stop_button = GlowButton(
    button_wrap,
    "STOP",
    stop_scan,
    RED,
    width=112,
    height=50,
)
stop_button.pack(side=tk.LEFT, padx=4)

# -----------------------------
# Statistics
# -----------------------------

stats = tk.Frame(
    root,
    bg=BG,
)
stats.grid(
    row=2,
    column=0,
    sticky="ew",
    padx=30,
    pady=(11, 13),
)

for column in range(3):
    stats.grid_columnconfigure(column, weight=1)

checked_card = StatCard(stats, "Checked", "0", BLUE)
checked_card.grid(
    row=0,
    column=0,
    sticky="nsew",
    padx=(0, 6),
)
checked_card.configure(height=94)

found_card = StatCard(stats, "Available", "0", GREEN)
found_card.grid(
    row=0,
    column=1,
    sticky="nsew",
    padx=6,
)
found_card.configure(height=94)

error_card = StatCard(stats, "Errors", "0", RED)
error_card.grid(
    row=0,
    column=2,
    sticky="nsew",
    padx=(6, 0),
)
error_card.configure(height=94)

# Existing worker code expects these to be label-like widgets.
checked_label = checked_card.value_label
found_label = found_card.value_label
error_label = error_card.value_label

# -----------------------------
# Results title
# -----------------------------

results_header = tk.Frame(
    root,
    bg=BG,
)
results_header.grid(
    row=3,
    column=0,
    sticky="ew",
    padx=30,
    pady=(0, 8),
)
results_header.grid_columnconfigure(0, weight=1)

tk.Label(
    results_header,
    text="LIVE RESULTS",
    bg=BG,
    fg=TEXT,
    font=("Segoe UI", 10, "bold"),
).grid(row=0, column=0, sticky="w")

tk.Label(
    results_header,
    text="AVAILABLE  •  TAKEN  •  ERRORS",
    bg=BG,
    fg=MUTED,
    font=("Segoe UI", 8, "bold"),
).grid(row=0, column=1, sticky="e")

# -----------------------------
# Results box
# -----------------------------

results = RoundedSurface(
    root,
    bg_color=SURFACE,
    radius=26,
    border=BORDER,
    border_width=1,
    padding=1,
)
results.grid(
    row=4,
    column=0,
    sticky="nsew",
    padx=30,
    pady=(0, 22),
)

results.inner.grid_columnconfigure(0, weight=1)
results.inner.grid_rowconfigure(0, weight=1)

log_box = scrolledtext.ScrolledText(
    results.inner,
    bg=LOG_BG,
    fg="#c7d2e2",
    insertbackground=TEXT,
    selectbackground="#1b2b40",
    selectforeground=TEXT,
    relief="flat",
    bd=0,
    highlightthickness=0,
    font=("Consolas", 9),
    padx=16,
    pady=14,
    wrap=tk.NONE,
)
log_box.grid(
    row=0,
    column=0,
    sticky="nsew",
    padx=5,
    pady=5,
)

log_box.tag_config(
    "available",
    foreground=GREEN,
)
log_box.tag_config(
    "taken",
    foreground="#78869a",
)
log_box.tag_config(
    "error",
    foreground=RED,
)
log_box.tag_config(
    "rate",
    foreground=YELLOW,
)
log_box.tag_config(
    "info",
    foreground=BLUE,
)

# -----------------------------
# Queue processor
# -----------------------------

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
                elif (
                    "RATE LIMITED" in message
                    or "HTTP 429" in message
                    or "RATE LIMIT" in message
                ):
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
                checked_label.config(
                    text=f"{value:,}"
                )

            elif kind == "available":
                found_label.config(
                    text=f"{value:,}"
                )

            elif kind == "errors":
                error_label.config(
                    text=f"{value:,}"
                )

            elif kind == "status":
                status_text = str(value).upper()

                if (
                    "FAIL" in status_text
                    or "STOP" in status_text
                ):
                    status_label.config(
                        text=f"  ●  {status_text}  ",
                        fg=RED,
                        bg=SURFACE_2,
                    )
                elif (
                    "RATE" in status_text
                    or "TEST" in status_text
                    or "START" in status_text
                ):
                    status_label.config(
                        text=f"  ●  {status_text}  ",
                        fg=YELLOW,
                        bg=SURFACE_2,
                    )
                elif (
                    "CONNECT" in status_text
                    or "READY" in status_text
                    or "FINISHED" in status_text
                ):
                    status_label.config(
                        text=f"  ●  {status_text}  ",
                        fg=GREEN,
                        bg=SURFACE_2,
                    )
                else:
                    status_label.config(
                        text=f"  ●  {status_text}  ",
                        fg=BLUE,
                        bg=SURFACE_2,
                    )

    except queue.Empty:
        pass

    root.after(80, process_ui_queue)


root.after(80, process_ui_queue)
root.mainloop()
