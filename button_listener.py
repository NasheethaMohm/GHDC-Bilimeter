#!/usr/bin/env python3
"""
Button listener with LCD status display.
Raspberry Pi Zero 2 W
- Button on GPIO17 starts/stops BiliMeter_Test_revised.py
- 16x2 I2C LCD shows current status and the final result

LCD ownership:
- While the measurement script runs, it owns the LCD; this listener
  does not write to the screen.
- After it exits, this listener shows the result (parsed from the
  script's "RESULT <value>" output line) until the next press.
"""

from gpiozero import Button
from RPLCD.i2c import CharLCD
import os
import signal
import subprocess
import threading
import time
import sys

# ── Configuration ─────────────────────────────────────────────────────
BUTTON_PIN   = 17                    # GPIO17 = physical Pin 11
# Measurement script: same folder as this file, unless overridden
SCRIPT_PATH  = os.environ.get(
    "BILIMETER_SCRIPT",
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "BiliMeter_Test_revised.py"),
)
DEBOUNCE     = 0.3                   # button debounce seconds
STOP_TIMEOUT = 10                    # seconds to wait for clean shutdown
LCD_ADDR     = 0x27                  # change to 0x3F if needed
LCD_PORT     = 1
LCD_COLS     = 16
LCD_ROWS     = 2


# ── LCD Manager ───────────────────────────────────────────────────────
class LCD:
    def __init__(self, addr=LCD_ADDR, port=LCD_PORT):
        self._lock = threading.Lock()
        try:
            self.screen = CharLCD(
                i2c_expander='PCF8574',
                address=addr,
                port=port,
                cols=LCD_COLS,
                rows=LCD_ROWS,
                dotsize=8,
                charmap='A02',
                auto_linebreaks=False,
                backlight_enabled=True,
            )
            self.screen.clear()
            print(f"  LCD initialised at 0x{addr:02X} ✓")
            self.available = True
        except Exception as e:
            print(f"  [WARN] LCD not found: {e}")
            self.available = False

    def write(self, line1, line2=""):
        """Write two lines to the LCD."""
        if not self.available:
            return
        with self._lock:
            try:
                # Measurement script may have changed the backlight
                self.screen.backlight_enabled = True
                # RPLCD skips characters it thinks are already on screen.
                # The measurement script writes to the same LCD, so this
                # cache is stale; clearing resets both screen and cache.
                self.screen.clear()
                # Line 1
                padded1 = line1[:LCD_COLS].ljust(LCD_COLS)
                self.screen.cursor_pos = (0, 0)
                self.screen.write_string(padded1)
                # Line 2
                padded2 = line2[:LCD_COLS].ljust(LCD_COLS)
                self.screen.cursor_pos = (1, 0)
                self.screen.write_string(padded2)
            except Exception as e:
                print(f"  [WARN] LCD write error: {e}")

    def clear(self):
        if not self.available:
            return
        with self._lock:
            try:
                self.screen.clear()
            except Exception as e:
                print(f"  [WARN] LCD clear error: {e}")

    def close(self):
        if not self.available:
            return
        try:
            self.screen.clear()
            self.screen.cursor_pos = (0, 0)
            self.screen.write_string("   Goodbye!     ")
            time.sleep(1)
            self.screen.backlight_enabled = False
            self.screen.close()
        except Exception:
            pass


# ── Button Controller ─────────────────────────────────────────────────
class ButtonController:
    def __init__(self, lcd):
        self.lcd            = lcd
        self.script_process = None
        self.result         = None
        self._lock          = threading.Lock()
        self._reader_thread = None

        # Setup button
        self.button = Button(
            BUTTON_PIN,
            pull_up=True,
            bounce_time=DEBOUNCE
        )
        self.button.when_pressed = self._on_press

    def _is_running(self):
        """Check if sensor script is currently running."""
        return (self.script_process is not None and
                self.script_process.poll() is None)

    def _read_output(self, proc):
        """
        Echo the script's output (goes to the systemd journal) and
        capture the final RESULT line. Reading continuously also keeps
        the pipe from filling up and blocking the script.
        """
        for line in proc.stdout:
            line = line.rstrip()
            print(f"  [sensor] {line}")
            if line.startswith("RESULT "):
                try:
                    self.result = float(line.split()[1])
                except (IndexError, ValueError):
                    pass

    def _start_script(self):
        """Start the sensor script."""
        with self._lock:
            if self._is_running():
                print("Already running — ignoring.")
                return

            print(f"Starting sensor script: {SCRIPT_PATH}")
            self.lcd.write(
                "Starting...",
                "Please wait"
            )
            self.result = None

            try:
                self.script_process = subprocess.Popen(
                    # Same Python as this listener, so systemd uses the
                    # same environment/packages
                    [sys.executable, SCRIPT_PATH],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env=dict(os.environ, PYTHONUNBUFFERED="1"),
                )
                print(f"Script started — PID {self.script_process.pid}")
                self._reader_thread = threading.Thread(
                    target=self._read_output,
                    args=(self.script_process,),
                    daemon=True,
                )
                self._reader_thread.start()
                # From here the sensor script owns the LCD

            except Exception as e:
                print(f"  [ERROR] Could not start script: {e}")
                self.script_process = None
                self.lcd.write(
                    "Start Failed!",
                    str(e)[:LCD_COLS]
                )

    def _wait_for_reader(self):
        if self._reader_thread:
            self._reader_thread.join(timeout=2)
            self._reader_thread = None

    def _stop_script(self):
        """Stop the sensor script."""
        with self._lock:
            if not self._is_running():
                print("Not running — ignoring.")
                return

            print("Stopping sensor script...")

            try:
                # SIGINT = Ctrl+C, so the script's cleanup code runs
                # (sensor shutdown, LCD release)
                self.script_process.send_signal(signal.SIGINT)
                self.script_process.wait(timeout=STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                print("  [WARN] Script did not stop — killing it.")
                self.script_process.kill()
                self.script_process.wait()
            except Exception as e:
                print(f"  [WARN] Stop error: {e}")

            self._wait_for_reader()
            self.script_process = None
            print("Script stopped.")
            self.lcd.write(
                "Stopped.",
                "Press to start"
            )

    def check_finished(self):
        """Show the result if the script ended on its own."""
        with self._lock:
            if (self.script_process is None or
                    self.script_process.poll() is None):
                return

            code = self.script_process.returncode
            self._wait_for_reader()
            self.script_process = None
            print(f"Script ended on its own (exit code {code}).")

            if self.result is not None:
                print(f"Result: {self.result:.2f} mg/dL")
                # e.g. "Bili  12.3 mg/dL" (16 chars)
                self.lcd.write(
                    f"Bili {self.result:5.1f} mg/dL",
                    "Press to start"
                )
            elif code != 0:
                self.lcd.write(
                    "Measure failed",
                    "Press to retry"
                )
            else:
                self.lcd.write(
                    "Script Ended",
                    "Press to start"
                )

    def _on_press(self):
        """Called every time button is pressed."""
        if self._is_running():
            self._stop_script()
        else:
            self._start_script()

    def show_ready(self):
        """Show ready message on LCD at startup."""
        self.lcd.write(
            "  Ready!",
            "Press to start"
        )

    def close(self):
        """Clean shutdown."""
        if self._is_running():
            self._stop_script()
        self.lcd.close()


# ── Main ──────────────────────────────────────────────────────────────
def _raise_keyboard_interrupt(signum, frame):
    raise KeyboardInterrupt


def main():
    # systemd stops services with SIGTERM; handle it like Ctrl+C so the
    # measurement script and LCD are shut down cleanly
    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)

    print("=== Button Listener with LCD ===")
    print(f"  Button  : GPIO{BUTTON_PIN} (Pin 11)")
    print(f"  Script  : {SCRIPT_PATH}")
    print(f"  Python  : {sys.executable}")
    print(f"  LCD     : 0x{LCD_ADDR:02X} on bus {LCD_PORT}\n")

    if not os.path.isfile(SCRIPT_PATH):
        print(f"  [WARN] Script not found: {SCRIPT_PATH}")

    # Initialise LCD
    lcd = LCD(addr=LCD_ADDR, port=LCD_PORT)

    lcd.write(
        "   Bilimeter",
        "  Initialising"
    )
    time.sleep(1)

    # Initialise button controller
    controller = ButtonController(lcd)
    controller.show_ready()

    print("Listening for button press...")
    print("Press Ctrl+C to exit.\n")

    try:
        while True:
            controller.check_finished()
            time.sleep(0.5)

    except KeyboardInterrupt:
        print("\nStopped by user.")
    finally:
        controller.close()
        print("Shutdown complete.")


if __name__ == "__main__":
    main()
