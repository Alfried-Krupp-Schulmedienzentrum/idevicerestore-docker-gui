#!/usr/bin/env python3
import argparse
import curses
import datetime
import errno
import fcntl
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path


DEFAULT_RESTORE_ARGS = ["--erase", "--latest", "-d"]

ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
PROGRESS_RE = re.compile(
    r"^(?P<label>.*?)\s*\[[^\]]+\]\s*(?P<percent>\d+(?:\.\d+)?)%"
)

USBMUXD_UNITS = ["usbmuxd.service", "usbmuxd.socket"]


class RestoreGui:
    def __init__(
        self,
        stdscr,
        command,
        log_dir,
        manage_usbmuxd=True,
        mask_usbmuxd=True,
        ignore_usbmuxd_failure=False,
    ):
        self.stdscr = stdscr
        self.command = command
        self.log_dir = Path(log_dir)

        self.manage_usbmuxd = manage_usbmuxd
        self.mask_usbmuxd = mask_usbmuxd
        self.ignore_usbmuxd_failure = ignore_usbmuxd_failure
        self.usbmuxd_masked_by_us = []

        self.proc = None
        self.master_fd = None
        self.log_file = None
        self.log_path = None

        self.lines = []
        self.partial_line = ""

        self.progress_label = None
        self.progress_percent = None
        self.progress_raw = None
        self.last_progress_update = 0.0

        self.status = "Bereit"
        self.confirm_reset = False
        self.abort_requested_at = None

        self.autoscroll = True
        self.scroll_offset = 0

        self.height = 0
        self.width = 0

        self.dirty = True
        self.last_draw = 0.0

    # ------------------------------------------------------------
    # Curses helpers
    # ------------------------------------------------------------

    def setup(self):
        try:
            curses.curs_set(0)
        except curses.error:
            pass

        curses.noecho()
        curses.cbreak()

        self.stdscr.keypad(True)
        self.stdscr.timeout(50)
        self.stdscr.leaveok(True)

        try:
            curses.use_default_colors()
        except curses.error:
            pass

        self.height, self.width = self.stdscr.getmaxyx()
        self.dirty = True

    def safe_addstr(self, y, x, text, attr=0):
        try:
            h, w = self.stdscr.getmaxyx()

            if y < 0 or y >= h or x < 0 or x >= w:
                return

            text = str(text)
            max_len = max(0, w - x - 1)

            if max_len <= 0:
                return

            self.stdscr.addstr(y, x, text[:max_len], attr)
        except curses.error:
            pass

    def clear_line(self, y):
        try:
            h, _ = self.stdscr.getmaxyx()

            if 0 <= y < h:
                self.stdscr.move(y, 0)
                self.stdscr.clrtoeol()
        except curses.error:
            pass

    def draw_border(self, top, left, height, width):
        bottom = top + height - 1
        right = left + width - 1

        if height < 2 or width < 2:
            return

        try:
            self.stdscr.hline(top, left + 1, curses.ACS_HLINE, width - 2)
            self.stdscr.hline(bottom, left + 1, curses.ACS_HLINE, width - 2)
            self.stdscr.vline(top + 1, left, curses.ACS_VLINE, height - 2)
            self.stdscr.vline(top + 1, right, curses.ACS_VLINE, height - 2)

            self.stdscr.addch(top, left, curses.ACS_ULCORNER)
            self.stdscr.addch(top, right, curses.ACS_URCORNER)
            self.stdscr.addch(bottom, left, curses.ACS_LLCORNER)
            self.stdscr.addch(bottom, right, curses.ACS_LRCORNER)

        except curses.error:
            pass

    # ------------------------------------------------------------
    # Host command helpers
    # ------------------------------------------------------------

    def root_prefix(self):
        if os.geteuid() == 0:
            return []
        return ["sudo", "-n"]

    def run_host_command(self, args, need_root=False, timeout=10):
        cmd = list(args)

        if need_root:
            cmd = self.root_prefix() + cmd

        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
            )
            return result.returncode, result.stdout.strip(), result.stderr.strip()

        except FileNotFoundError as exc:
            return 127, "", str(exc)

        except subprocess.TimeoutExpired:
            return 124, "", f"Timeout bei Befehl: {' '.join(cmd)}"

    def systemctl_available(self):
        return shutil.which("systemctl") is not None

    def systemctl_show_value(self, unit, prop):
        rc, stdout, stderr = self.run_host_command(
            ["systemctl", "show", "-p", prop, "--value", unit],
            need_root=False,
            timeout=5,
        )

        if rc != 0:
            return None

        return stdout.strip()

    def systemctl_is_active(self, unit):
        rc, stdout, stderr = self.run_host_command(
            ["systemctl", "is-active", unit],
            need_root=False,
            timeout=5,
        )
        return stdout.strip() or stderr.strip() or "unknown"

    def systemctl_is_enabled(self, unit):
        rc, stdout, stderr = self.run_host_command(
            ["systemctl", "is-enabled", unit],
            need_root=False,
            timeout=5,
        )
        return stdout.strip() or stderr.strip() or "unknown"

    def prepare_host_usbmuxd(self):
        if not self.manage_usbmuxd:
            self.add_line("Host-usbmuxd-Prüfung deaktiviert.")
            return True

        self.add_line("Prüfe Host-usbmuxd …")

        if not self.systemctl_available():
            self.add_line("Warnung: systemctl nicht gefunden, usbmuxd wird nicht automatisch verwaltet.")
            return True

        available_units = []

        for unit in USBMUXD_UNITS:
            load_state = self.systemctl_show_value(unit, "LoadState")

            if load_state in (None, "", "not-found"):
                self.add_line(f"{unit}: nicht gefunden")
                continue

            active_state = self.systemctl_is_active(unit)
            enabled_state = self.systemctl_is_enabled(unit)

            self.add_line(
                f"{unit}: LoadState={load_state}, Active={active_state}, Enabled={enabled_state}"
            )

            available_units.append(
                {
                    "unit": unit,
                    "load_state": load_state,
                    "active_state": active_state,
                    "enabled_state": enabled_state,
                }
            )

        if not available_units:
            self.add_line("Keine systemd-Units für usbmuxd gefunden.")
            return True

        unit_names = [entry["unit"] for entry in available_units]

        # Erst stoppen, damit kein Host-usbmuxd mehr aktiv am USB-Gerät hängt.
        self.add_line("Stoppe Host-usbmuxd service/socket …")
        rc, stdout, stderr = self.run_host_command(
            ["systemctl", "stop"] + unit_names,
            need_root=True,
            timeout=10,
        )

        if rc != 0:
            self.add_line("Fehler: Host-usbmuxd konnte nicht gestoppt werden.")
            if stderr:
                self.add_line(stderr)
            if stdout:
                self.add_line(stdout)

            if "sudo" in stderr.lower() or "password" in stderr.lower():
                self.add_line("Hinweis: Bitte vorher `sudo -v` ausführen oder das Script mit sudo starten.")

            if not self.ignore_usbmuxd_failure:
                self.status = "Abbruch: Host-usbmuxd konnte nicht gestoppt werden."
                self.dirty = True
                return False

        else:
            self.add_line("Host-usbmuxd wurde gestoppt.")

        # Dann temporär maskieren. Das verhindert, dass usbmuxd.socket
        # durch udev oder Socket-Aktivierung sofort wieder gestartet wird.
        if self.mask_usbmuxd:
            for entry in available_units:
                unit = entry["unit"]
                enabled_state = entry["enabled_state"]
                load_state = entry["load_state"]

                if enabled_state == "masked" or load_state == "masked":
                    self.add_line(f"{unit}: bereits maskiert, wird später nicht verändert.")
                    continue

                self.add_line(f"Maskiere {unit} temporär bis zum nächsten Unmask/Reboot …")
                rc, stdout, stderr = self.run_host_command(
                    ["systemctl", "mask", "--runtime", unit],
                    need_root=True,
                    timeout=10,
                )

                if rc != 0:
                    self.add_line(f"Fehler: {unit} konnte nicht temporär maskiert werden.")
                    if stderr:
                        self.add_line(stderr)
                    if stdout:
                        self.add_line(stdout)

                    if not self.ignore_usbmuxd_failure:
                        self.status = f"Abbruch: {unit} konnte nicht maskiert werden."
                        self.dirty = True
                        return False

                else:
                    self.usbmuxd_masked_by_us.append(unit)
                    self.add_line(f"{unit}: temporär maskiert.")

        # Zusätzlich eventuell noch laufenden Prozess entfernen.
        # Das ist absichtlich nach systemctl stop/mask, damit er nicht direkt wiederkommt.
        self.add_line("Prüfe laufenden usbmuxd-Prozess …")
        rc, stdout, stderr = self.run_host_command(
            ["pgrep", "-x", "usbmuxd"],
            need_root=False,
            timeout=5,
        )

        if rc == 0:
            self.add_line("Laufender usbmuxd-Prozess gefunden, beende ihn …")
            rc, stdout, stderr = self.run_host_command(
                ["pkill", "-x", "usbmuxd"],
                need_root=True,
                timeout=5,
            )

            if rc != 0:
                self.add_line("Warnung: usbmuxd-Prozess konnte nicht beendet werden.")
                if stderr:
                    self.add_line(stderr)

                if not self.ignore_usbmuxd_failure:
                    self.status = "Abbruch: laufender usbmuxd-Prozess konnte nicht beendet werden."
                    self.dirty = True
                    return False
            else:
                self.add_line("Laufender usbmuxd-Prozess wurde beendet.")
        else:
            self.add_line("Kein laufender Host-usbmuxd-Prozess gefunden.")

        self.add_line("Host-usbmuxd ist für den Restore vorbereitet.")
        return True

    def restore_host_usbmuxd(self):
        if not self.usbmuxd_masked_by_us:
            return

        if not self.systemctl_available():
            self.usbmuxd_masked_by_us.clear()
            return

        units = list(dict.fromkeys(self.usbmuxd_masked_by_us))
        self.add_line("Hebe temporäre usbmuxd-Maskierung wieder auf …")

        rc, stdout, stderr = self.run_host_command(
            ["systemctl", "unmask", "--runtime"] + units,
            need_root=True,
            timeout=10,
        )

        if rc != 0:
            self.add_line("Warnung: Temporäre usbmuxd-Maskierung konnte nicht automatisch aufgehoben werden.")
            if stderr:
                self.add_line(stderr)
            self.add_line("Manuell möglich mit:")
            self.add_line("sudo systemctl unmask --runtime usbmuxd.service usbmuxd.socket")
        else:
            self.add_line("Temporäre usbmuxd-Maskierung wurde aufgehoben.")

        self.usbmuxd_masked_by_us.clear()
        self.dirty = True

    # ------------------------------------------------------------
    # Log/progress handling
    # ------------------------------------------------------------

    def clean_text(self, text):
        text = ANSI_RE.sub("", text)
        text = text.replace("\t", "    ")
        return text

    def detect_progress(self, text):
        match = PROGRESS_RE.match(text.strip())
        if not match:
            return False

        label = match.group("label").strip() or "Fortschritt"
        percent = float(match.group("percent"))

        self.progress_label = label
        self.progress_percent = max(0.0, min(100.0, percent))
        self.progress_raw = text.strip()
        self.last_progress_update = time.time()
        self.dirty = True
        return True

    def add_line(self, text):
        text = self.clean_text(text).strip()

        # Leerzeilen konsequent entfernen.
        if not text:
            return

        # Fortschrittszeilen nicht ins Log schreiben,
        # sondern nur die Progress-Bar aktualisieren.
        if self.detect_progress(text):
            return

        # Doppelte direkt aufeinanderfolgende Logzeilen vermeiden.
        if self.lines and self.lines[-1] == text:
            return

        self.lines.append(text)

        if len(self.lines) > 8000:
            self.lines = self.lines[-8000:]

        self.dirty = True

    def finish_partial_line(self):
        if self.partial_line:
            self.add_line(self.partial_line)
            self.partial_line = ""

    def feed_output(self, data):
        if self.log_file:
            self.log_file.write(data)
            self.log_file.flush()

        text = data.decode(errors="replace")
        text = self.clean_text(text)

        # Normale PTY-Zeilen kommen oft als CRLF.
        # Das darf nicht als Progress-CR gewertet werden.
        text = text.replace("\r\n", "\n").replace("\n\r", "\n")

        for char in text:
            if char == "\n":
                self.finish_partial_line()

            elif char == "\r":
                # Echte Fortschrittsanzeigen werden mit Carriage Return überschrieben.
                # Wir übernehmen sie nur in die separate Progress-Bar.
                if self.partial_line:
                    line = self.partial_line
                    self.partial_line = ""

                    if not self.detect_progress(line):
                        self.add_line(line)

            elif char == "\b":
                self.partial_line = self.partial_line[:-1]
                self.dirty = True

            else:
                self.partial_line += char

                # Falls eine Progresszeile ohne CR lange steht,
                # trotzdem die Bar aktualisieren.
                stripped = self.partial_line.strip()
                if PROGRESS_RE.match(stripped):
                    self.detect_progress(stripped)

        if text:
            self.dirty = True

    # ------------------------------------------------------------
    # Process handling
    # ------------------------------------------------------------

    def apply_pty_size(self):
        if self.master_fd is None:
            return

        try:
            h, w = self.stdscr.getmaxyx()
            winsize = struct.pack("HHHH", h, w, 0, 0)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
        except OSError:
            pass

    def start_restore(self):
        if self.proc is not None:
            return

        run_sh = self.command[1] if self.command and self.command[0] == "sudo" else self.command[0]

        if run_sh.startswith("./") or run_sh.startswith("/"):
            if not Path(run_sh).exists():
                self.status = f"Nicht gefunden: {run_sh}"
                self.add_line(f"Fehler: {run_sh} wurde nicht gefunden.")
                self.dirty = True
                return

        self.log_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_path = self.log_dir / f"idevicerestore_{timestamp}.log"
        self.log_file = open(self.log_path, "wb")

        self.lines.clear()
        self.partial_line = ""
        self.progress_label = None
        self.progress_percent = None
        self.progress_raw = None
        self.scroll_offset = 0
        self.autoscroll = True
        self.abort_requested_at = None
        self.usbmuxd_masked_by_us.clear()

        self.add_line("Starte Restore-Befehl:")
        self.add_line(" ".join(self.command))

        if not self.prepare_host_usbmuxd():
            self.add_line("Restore wurde nicht gestartet.")
            self.dirty = True
            return

        self.status = "Restore läuft … Auswahl wie 1 + Enter wird an idevicerestore weitergegeben."

        master_fd, slave_fd = pty.openpty()

        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        self.proc = subprocess.Popen(
            self.command,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
            close_fds=True,
        )

        os.close(slave_fd)

        self.master_fd = master_fd
        self.apply_pty_size()
        self.dirty = True

    def request_abort(self):
        if self.proc is None:
            return

        if self.abort_requested_at is None:
            self.abort_requested_at = time.time()
            self.status = "Abbruch angefordert … erneut Ctrl+A erzwingt SIGKILL."
            self.add_line("Sende SIGTERM an Restore-Prozess …")

            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

        else:
            self.add_line("Sende SIGKILL an Restore-Prozess …")

            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        self.dirty = True

    def send_to_child(self, payload):
        if self.proc is None or self.master_fd is None:
            return

        try:
            os.write(self.master_fd, payload)
        except OSError:
            pass

    def read_process_output(self):
        if self.proc is None or self.master_fd is None:
            return

        while True:
            try:
                ready, _, _ = select.select([self.master_fd], [], [], 0)

                if not ready:
                    break

                data = os.read(self.master_fd, 8192)

                if not data:
                    break

                self.feed_output(data)

            except OSError as e:
                if e.errno == errno.EIO:
                    break
                raise

    def check_process_exit(self):
        if self.proc is None:
            return

        rc = self.proc.poll()

        if rc is None:
            if self.abort_requested_at and time.time() - self.abort_requested_at > 5:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

            return

        self.read_process_output()
        self.finish_partial_line()

        if rc == 0:
            self.status = f"Restore beendet: Erfolg. Log: {self.log_path}"
            self.progress_label = "Fertig"
            self.progress_percent = 100.0
            self.add_line("Restore erfolgreich beendet.")
        else:
            self.status = f"Restore beendet mit Fehlercode {rc}. Log: {self.log_path}"
            self.add_line(f"Restore beendet mit Fehlercode {rc}.")

        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass

        if self.log_file:
            self.log_file.close()

        self.proc = None
        self.master_fd = None
        self.log_file = None
        self.abort_requested_at = None

        self.restore_host_usbmuxd()
        self.dirty = True

    def cleanup_running_process(self):
        if self.proc is None:
            self.restore_host_usbmuxd()
            return

        self.request_abort()

        deadline = time.time() + 2

        while time.time() < deadline and self.proc and self.proc.poll() is None:
            self.read_process_output()
            time.sleep(0.05)

        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        self.restore_host_usbmuxd()

    # ------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------

    def get_display_lines(self):
        display_lines = list(self.lines)

        # Prompt-Zeilen ohne Zeilenumbruch sichtbar machen,
        # aber Progresszeilen nicht doppelt anzeigen.
        if self.partial_line and not PROGRESS_RE.match(self.partial_line.strip()):
            text = self.clean_text(self.partial_line).strip()
            if text:
                display_lines.append(text)

        return display_lines

    def draw_progress(self, y, width):
        self.clear_line(y)

        if self.progress_percent is None:
            text = "Fortschritt: -"
            self.safe_addstr(y, 2, text)
            return

        label = self.progress_label or "Fortschritt"
        percent = self.progress_percent
        percent_text = f"{percent:5.1f}%"
        prefix = f"{label}: "

        available = width - len(prefix) - len(percent_text) - 8
        bar_width = max(10, min(60, available))
        filled = int(round(bar_width * percent / 100.0))
        filled = max(0, min(bar_width, filled))

        bar = "[" + "#" * filled + " " * (bar_width - filled) + "]"
        text = f"{prefix}{bar} {percent_text}"

        self.safe_addstr(y, 2, text)

    def draw(self, force=False):
        now = time.time()

        if not force and not self.dirty and now - self.last_draw < 0.2:
            return

        if not force and now - self.last_draw < 0.05:
            return

        self.last_draw = now

        h, w = self.stdscr.getmaxyx()

        if h != self.height or w != self.width:
            self.height = h
            self.width = w
            self.apply_pty_size()
            self.stdscr.erase()
            force = True

        if h < 14 or w < 70:
            self.stdscr.erase()
            self.safe_addstr(
                0,
                0,
                "Terminal zu klein. Bitte Fenster größer ziehen. Mindestgröße ca. 70x14.",
                curses.A_BOLD,
            )
            self.stdscr.refresh()
            self.dirty = False
            return

        # Header
        for y in range(0, 5):
            self.clear_line(y)

        self.safe_addstr(0, 2, "idevicerestore Docker GUI", curses.A_BOLD)
        self.safe_addstr(1, 2, "Befehl: " + " ".join(self.command))
        self.safe_addstr(2, 2, "Status: " + self.status)
        self.draw_progress(3, w)

        if self.confirm_reset:
            help_text = "Wirklich iPad löschen/zurücksetzen?  [j] Ja   [n] Nein"
            help_attr = curses.A_REVERSE | curses.A_BOLD
        elif self.proc is None:
            help_text = "[r] Zurücksetzen starten   [q] Beenden   [↑/↓/Bild↑/Bild↓] Log scrollen   [Ende] Auto-Scroll"
            help_attr = curses.A_REVERSE
        else:
            help_text = "Restore läuft: Zahlen/Text + Enter gehen an idevicerestore   [Ctrl+A] Abbrechen   [Ctrl+L] Neu zeichnen"
            help_attr = curses.A_REVERSE

        self.safe_addstr(4, 0, help_text.ljust(w - 1), help_attr)

        # Log-Bereich
        log_top = 5
        log_left = 0
        log_height = h - 6
        log_width = w

        self.draw_border(log_top, log_left, log_height, log_width)
        self.safe_addstr(log_top, 2, " Log-Ausgabe ")

        inner_top = log_top + 1
        inner_left = 1
        inner_height = log_height - 2
        inner_width = log_width - 2

        display_lines = self.get_display_lines()

        if self.autoscroll:
            self.scroll_offset = max(0, len(display_lines) - inner_height)

        max_offset = max(0, len(display_lines) - inner_height)
        self.scroll_offset = max(0, min(self.scroll_offset, max_offset))

        visible = display_lines[self.scroll_offset : self.scroll_offset + inner_height]

        for idx in range(inner_height):
            y = inner_top + idx
            self.safe_addstr(y, inner_left, " " * (inner_width - 1))

            if idx < len(visible):
                self.safe_addstr(y, inner_left, visible[idx])

        position = f" {min(len(display_lines), self.scroll_offset + inner_height)}/{len(display_lines)} "
        self.safe_addstr(log_top, max(2, w - len(position) - 2), position)

        # Footer
        self.clear_line(h - 1)

        footer = f"Logdatei: {self.log_path}" if self.log_path else "Noch keine Logdatei erstellt"

        if self.usbmuxd_masked_by_us:
            footer += "   |   usbmuxd temporär maskiert"

        if not self.autoscroll:
            footer += "   |   Auto-Scroll aus, [Ende] aktiviert ihn wieder"

        self.safe_addstr(h - 1, 2, footer)

        self.stdscr.refresh()
        self.dirty = False

    # ------------------------------------------------------------
    # Key handling
    # ------------------------------------------------------------

    def handle_scroll_key(self, key):
        display_len = len(self.get_display_lines())
        log_height = max(3, self.height - 8)
        page = max(3, log_height - 1)
        max_offset = max(0, display_len - log_height)

        if key == curses.KEY_UP:
            self.autoscroll = False
            self.scroll_offset = max(0, self.scroll_offset - 1)

        elif key == curses.KEY_DOWN:
            self.autoscroll = False
            self.scroll_offset = min(max_offset, self.scroll_offset + 1)

        elif key == curses.KEY_PPAGE:
            self.autoscroll = False
            self.scroll_offset = max(0, self.scroll_offset - page)

        elif key == curses.KEY_NPAGE:
            self.autoscroll = False
            self.scroll_offset = min(max_offset, self.scroll_offset + page)

        elif key == curses.KEY_END:
            self.autoscroll = True

        self.dirty = True

    def handle_key(self, key):
        if key == -1:
            return True

        if key == curses.KEY_RESIZE:
            self.height, self.width = self.stdscr.getmaxyx()
            self.apply_pty_size()
            self.stdscr.erase()
            self.dirty = True
            return True

        if self.confirm_reset:
            if key in (ord("j"), ord("J")):
                self.confirm_reset = False
                self.start_restore()

            elif key in (ord("n"), ord("N"), 27):
                self.confirm_reset = False
                self.status = "Zurücksetzen abgebrochen"
                self.dirty = True

            return True

        if self.proc is not None:
            # Ctrl+A = Abbruch
            if key == 1:
                self.request_abort()
                return True

            # Ctrl+L = kompletter Neuaufbau
            if key == 12:
                self.stdscr.erase()
                self.dirty = True
                self.draw(force=True)
                return True

            if key in (
                curses.KEY_UP,
                curses.KEY_DOWN,
                curses.KEY_PPAGE,
                curses.KEY_NPAGE,
                curses.KEY_END,
            ):
                self.handle_scroll_key(key)
                return True

            # Enter weitergeben, wichtig für iPadOS-Auswahl
            if key in (10, 13, curses.KEY_ENTER):
                self.send_to_child(b"\n")
                return True

            # Backspace weitergeben
            if key in (curses.KEY_BACKSPACE, 127, 8):
                self.send_to_child(b"\x7f")
                return True

            # ESC nicht weitergeben
            if key == 27:
                return True

            # Normale Zeichen weitergeben, z. B. 1, 2, 3 für Firmware-Auswahl
            if 0 <= key <= 255:
                char = chr(key)

                if char.isprintable():
                    self.send_to_child(char.encode())

                return True

            return True

        # Keine laufende Wiederherstellung
        if key in (ord("q"), ord("Q")):
            return False

        if key in (ord("r"), ord("R")):
            self.confirm_reset = True
            self.dirty = True
            return True

        if key in (
            curses.KEY_UP,
            curses.KEY_DOWN,
            curses.KEY_PPAGE,
            curses.KEY_NPAGE,
            curses.KEY_END,
        ):
            self.handle_scroll_key(key)
            return True

        return True

    def run(self):
        self.setup()
        self.draw(force=True)

        running = True

        while running:
            self.read_process_output()
            self.check_process_exit()

            self.draw()

            key = self.stdscr.getch()
            running = self.handle_key(key)

        self.cleanup_running_process()


def curses_main(stdscr, args):
    command = []

    if args.sudo:
        command.append("sudo")

    command.append(args.run_sh)

    restore_args = args.restore_args

    if restore_args and restore_args[0] == "--":
        restore_args = restore_args[1:]

    if not restore_args:
        restore_args = DEFAULT_RESTORE_ARGS

    command.extend(restore_args)

    gui = RestoreGui(
        stdscr=stdscr,
        command=command,
        log_dir=args.log_dir,
        manage_usbmuxd=not args.no_usbmuxd_check,
        mask_usbmuxd=not args.no_usbmuxd_mask,
        ignore_usbmuxd_failure=args.ignore_usbmuxd_failure,
    )

    gui.run()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Curses-GUI für libimobiledevice/idevicerestore docker/run.sh"
    )

    parser.add_argument(
        "--run-sh",
        default="./run.sh",
        help="Pfad zur run.sh, Standard: ./run.sh",
    )

    parser.add_argument(
        "--sudo",
        action="store_true",
        help="run.sh mit sudo starten. Für die usbmuxd-Prüfung trotzdem vorher sudo -v ausführen oder das ganze Script mit sudo starten.",
    )

    parser.add_argument(
        "--log-dir",
        default="./logs",
        help="Ordner für Logdateien, Standard: ./logs",
    )

    parser.add_argument(
        "--no-usbmuxd-check",
        action="store_true",
        help="Host-usbmuxd vor dem Restore nicht prüfen/stoppen.",
    )

    parser.add_argument(
        "--no-usbmuxd-mask",
        action="store_true",
        help="usbmuxd.service/socket nur stoppen, aber nicht temporär maskieren.",
    )

    parser.add_argument(
        "--ignore-usbmuxd-failure",
        action="store_true",
        help="Restore trotzdem starten, wenn usbmuxd nicht gestoppt/maskiert werden konnte.",
    )

    parser.add_argument(
        "restore_args",
        nargs=argparse.REMAINDER,
        help="Argumente für run.sh, Standard: --erase --latest -d",
    )

    return parser.parse_args()


if __name__ == "__main__":
    try:
        curses.wrapper(curses_main, parse_args())

    except KeyboardInterrupt:
        print("Abgebrochen.", file=sys.stderr)
