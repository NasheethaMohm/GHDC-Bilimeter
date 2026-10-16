#!/usr/bin/env python3
"""
Button listener with LCD status display.
Raspberry Pi Zero 2 W
- Button on GPIO17 starts/stops sensor.py
- 16x2 I2C LCD shows current status
"""

from gpiozero import Button
from RPLCD.i2c import CharLCD
import subprocess
import threading
import time
import sys

# ── Configuration ─────────────────────────────────────────────────────
BUTTON_PIN   = 17                    # GPIO17 = physical Pin 11
SCRIPT_PATH  = "/home/pi/sensor.py" # full path to sensor script
DEBOUNCE     = 0.3                   # button debounce seconds
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
        self._lock          = threading.Lock()
        self._dot_thread    = None
        self._dot_running   = False

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

    def _start_script(self):
        """Start the sensor script."""
        with self._lock:
            if self._is_running():
                print("Already running — ignoring.")
                return

            print("Starting sensor script...")
            self.lcd.write(
                "Starting...",
                "Please wait"
            )
            time.sleep(0.5)

            try:
                self.script_process = subprocess.Popen(
                    ["python3", SCRIPT_PATH],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                print(f"Script started — PID {self.script_process.pid}")
                self.lcd.write(
                    "Sensor Running",
                    "PID:" + str(self.script_process.pid)
                )
                time.sleep(1)
                self.lcd.write(
                    "Sensor Running",
                    "Press to stop"
                )

                # Start dot animation to show it is alive
                self._start_dot_animation()

            except Exception as e:
                print(f"  [ERROR] Could not start script: {e}")
                self.lcd.write(
                    "Start Failed!",
                    str(e)[:LCD_COLS]
                )

    def _stop_script(self):
        """Stop the sensor script."""
        with self._lock:
            if not self._is_running():
                print("Not running — ignoring.")
                return

            print("Stopping sensor script...")
            self._stop_dot_animation()
            self.lcd.write(
                "Stopping...",
                "Please wait"
            )

            try:
                self.script_process.terminate()
                self.script_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.script_process.kill()
            except Exception as e:
                print(f"  [WARN] Stop error: {e}")

            self.script_process = None
            print("Script stopped.")
            self.lcd.write(
                "Stopped.",
                "Press to start"
            )

    def _on_press(self):
        """Called every time button is pressed."""
        if self._is_running():
            self._stop_script()
        else:
            self._start_script()

    # ── Dot animation ─────────────────────────────────────────────────
    # Shows dots cycling on line 2 to show the script is still running
    # Line 2 cycles through:
    # "Running .      "
    # "Running ..     "
    # "Running ...    "
    # "Running .      " (loops)

    def _start_dot_animation(self):
        self._dot_running = True
        self._dot_thread  = threading.Thread(
            target=self._dot_loop, daemon=True
        )
        self._dot_thread.start()

    def _stop_dot_animation(self):
        self._dot_running = False
        if self._dot_thread:
            self._dot_thread.join(timeout=2)
            self._dot_thread = None

    def _dot_loop(self):
        dots = 0
        while self._dot_running:
            dots = (dots % 3) + 1
            dot_str = "Running " + "." * dots
            # Only update line 2, keep line 1 as is
            if self.lcd.available:
                try:
                    with self.lcd._lock:
                        padded = dot_str[:LCD_COLS].ljust(LCD_COLS)
                        self.lcd.screen.cursor_pos = (1, 0)
                        self.lcd.screen.write_string(padded)
                except Exception:
                    pass
            time.sleep(0.6)

    def show_ready(self):
        """Show ready message on LCD at startup."""
        self.lcd.write(
            "  Ready!",
            "Press to start"
        )

    def close(self):
        """Clean shutdown."""
        self._stop_dot_animation()
        if self._is_running():
            self._stop_script()
        self.lcd.close()


# ── Main ──────────────────────────────────────────────────────────────
def main():
    print("=== Button Listener with LCD ===")
    print(f"  Button  : GPIO{BUTTON_PIN} (Pin 11)")
    print(f"  Script  : {SCRIPT_PATH}")
    print(f"  LCD     : 0x{LCD_ADDR:02X} on bus {LCD_PORT}\n")

    # Initialise LCD
    lcd = LCD(addr=LCD_ADDR, port=LCD_PORT)

    lcd.write(
        " Heart Rate 10",
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
            # Check if script ended on its own
            if (controller.script_process is not None and
                    controller.script_process.poll() is not None):
                print("Script ended on its own — resetting.")
                controller._stop_dot_animation()
                controller.script_process = None
                lcd.write(
                    "Script Ended",
                    "Press to start"
                )
            time.sleep(0.5)

    except KeyboardInterrupt:
        print("\nStopped by user.")
    finally:
        controller.close()
        print("Shutdown complete.")


if __name__ == "__main__":
    main()
