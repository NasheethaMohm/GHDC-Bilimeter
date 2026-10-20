#!/usr/bin/env python3
"""
MAX86916 via MikroE Heart Rate 10 Click
Raspberry Pi Zero 2 W — I2C bus 1
All 4 LEDs: IR, Red, Green, Blue
Calculates absorbance using Beer-Lambert law
"""
import sys
import time
import signal
import threading
from collections import deque

import smbus2
import numpy as np
from RPLCD.i2c import CharLCD

# ── I2C ──────────────────────────────────────────────────────────────
I2C_BUS       = 1
MAX86916_ADDR  = 0x57

# ── Registers ────────────────────────────────────────────────────────
REG_INT_STATUS1  = 0x00
REG_INT_ENABLE1  = 0x02
REG_FIFO_WR_PTR  = 0x04
REG_OVF_COUNTER  = 0x05
REG_FIFO_RD_PTR  = 0x06
REG_FIFO_DATA    = 0x07
REG_FIFO_CONFIG  = 0x08
REG_MODE_CFG1    = 0x09
REG_MODE_CFG2    = 0x0A
REG_LED1_PA      = 0x0C
REG_LED2_PA      = 0x0D
REG_LED3_PA      = 0x0E
REG_LED4_PA      = 0x0F
REG_LED_RANGE    = 0x11
REG_PILOT_PA     = 0x12
REG_LED_SEQ1     = 0x13
REG_LED_SEQ2     = 0x14
REG_PART_ID      = 0xFF

LED_OFF   = 0x0
LED_IR    = 0x1
LED_RED   = 0x2
LED_GREEN = 0x3
LED_BLUE  = 0x4

# ── Calibration (PLACEHOLDER until fitted to reference data) ──────────
CAL_D      = 1.0   # scale (mg/dL per absorbance unit)
CAL_ALPHA  = 1.0   # green weighting (corrects for melanin / hemoglobin)
CAL_OFFSET = 0.0   # mg/dL

class MAX86916:
    def __init__(self, bus_num=I2C_BUS):
        self.bus  = smbus2.SMBus(bus_num)
        self.addr = MAX86916_ADDR
        self._num_active_leds = 4

    # ── Low-level I2C ────────────────────────────────────────────────
    def _write(self, reg, val):
        self.bus.write_byte_data(self.addr, reg, val & 0xFF)

    def _read(self, reg):
        return self.bus.read_byte_data(self.addr, reg)

    def _read_block(self, reg, length):
        return self.bus.read_i2c_block_data(self.addr, reg, length)

    # ── Setup ────────────────────────────────────────────────────────
    def check_part_id(self):
        pid = self._read(REG_PART_ID)
        print(f"  Part ID: 0x{pid:02X} (expected 0x2B) — "
              f"{'OK ✓' if pid == 0x2B else 'MISMATCH ✗'}")
        return pid == 0x2B

    def reset(self):
        """Software reset — all registers return to default."""
        self._write(REG_MODE_CFG1, 0x40)
        time.sleep(0.05)
        # Wait for RESET bit to self-clear
        for _ in range(10):
            if not (self._read(REG_MODE_CFG1) & 0x40):
                break
            time.sleep(0.01)

    def setup(self,
              ir_ma=10.0,
              red_ma=10.0,
              green_ma=10.0,
              blue_ma=10.0,
              sample_rate=1,    # 0=50, 1=100, 2=200, 3=400, 4=800, 5=1000, 6=1600, 7=3200 sps
              pulse_width=1,    # 0=70µs, 1=120µs, 2=220µs, 3=420µs
              adc_range=2):     # 0=4µA, 1=8µA, 2=16µA, 3=32µA full-scale
        """
        Configure for all 4 LEDs in Flex LED mode.
        Current range auto-selected based on requested mA.
        """
        print("\nInitialising MAX86916...")
        self.reset()
        self.check_part_id()

        # ── LED range + PA calculation ────────────────────────────────
        # Range 0=50mA, 1=100mA, 2=150mA, 3=200mA max
        # PA byte: current = PA * (0.2 * (range+1) * 0.5)
        # Simplified: PA = round(mA / lsb), lsb = 0.2mA at range 0
        def ma_to_pa_range(ma):
            if   ma <= 50.4:  rge, lsb = 0, 0.2
            elif ma <= 100.8: rge, lsb = 1, 0.4
            elif ma <= 151.2: rge, lsb = 2, 0.6
            else:             rge, lsb = 3, 0.8
            pa = min(255, max(0, round(ma / lsb)))
            return pa, rge

        ir_pa,    ir_rge    = ma_to_pa_range(ir_ma)
        red_pa,   red_rge   = ma_to_pa_range(red_ma)
        green_pa, green_rge = ma_to_pa_range(green_ma)
        blue_pa,  blue_rge  = ma_to_pa_range(blue_ma)

        # ── Write registers ──────────────────────────────────────────
        # FIFO: no sample averaging, no rollover, almost-full at 17 samples
        self._write(REG_FIFO_CONFIG, 0x0F)

        # Mode config 2: ADC range | sample rate | pulse width
        mode2 = ((adc_range & 0x3) << 5) | ((sample_rate & 0x7) << 2) | (pulse_width & 0x3)
        self._write(REG_MODE_CFG2, mode2)

        # Flex LED mode (MODE=0b11)
        self._write(REG_MODE_CFG1, 0x03)

        # LED pulse amplitudes
        self._write(REG_LED1_PA, ir_pa)
        self._write(REG_LED2_PA, red_pa)
        self._write(REG_LED3_PA, green_pa)
        self._write(REG_LED4_PA, blue_pa)

        # LED range register: [LED4_RGE | LED3_RGE | LED2_RGE | LED1_RGE]
        range_reg = ((blue_rge & 0x3) << 6) | ((green_rge & 0x3) << 4) | \
                    ((red_rge  & 0x3) << 2) | (ir_rge    & 0x3)
        self._write(REG_LED_RANGE, range_reg)

        # LED sequence — all 4 LEDs active:
        # Slot1=IR(LED1), Slot2=Red(LED2), Slot3=Green(LED3), Slot4=Blue(LED4)
        self._write(REG_LED_SEQ1, (LED_RED   << 4) | LED_IR)
        self._write(REG_LED_SEQ2, (LED_BLUE  << 4) | LED_GREEN)

        # Enable sample-ready interrupt
        self._write(REG_INT_ENABLE1, 0x40)

        self._num_active_leds = 4
        self.clear_fifo()

        sr_map = {0:50, 1:100, 2:200, 3:400, 4:800, 5:1000, 6:1600, 7:3200}
        pw_map = {0:70, 1:120, 2:220, 3:420}
        print(f"  LEDs   : IR={ir_ma}mA  Red={red_ma}mA  "
              f"Green={green_ma}mA  Blue={blue_ma}mA")
        print(f"  Rate   : {sr_map.get(sample_rate, '?')} sps")
        print(f"  PW     : {pw_map.get(pulse_width, '?')} µs")
        print(f"  ADC FS : {[4,8,16,32][adc_range]} µA")
        print("  Ready.\n")

    # ── FIFO ─────────────────────────────────────────────────────────
    def clear_fifo(self):
        self._write(REG_FIFO_WR_PTR, 0x00)
        self._write(REG_OVF_COUNTER, 0x00)
        self._write(REG_FIFO_RD_PTR, 0x00)

    def available_samples(self):
        wr  = self._read(REG_FIFO_WR_PTR) & 0x1F
        rd  = self._read(REG_FIFO_RD_PTR) & 0x1F
        ovf = self._read(REG_OVF_COUNTER) & 0x1F
        if ovf:
            return 32
        return (wr - rd) % 32

    def read_samples(self, n=1):
        """
        Returns list of dicts: {'ir', 'red', 'green', 'blue'}
        Each value is a 19-bit ADC count (0–524287).
        """
        results = []
        bytes_per_led    = 3
        bytes_per_sample = self._num_active_leds * bytes_per_led  # 12

        for _ in range(n):
            raw = self._read_block(REG_FIFO_DATA, bytes_per_sample)
            def parse(i):
                return ((raw[i]   << 16) |
                        (raw[i+1] <<  8) |
                         raw[i+2]) & 0x7FFFF
            results.append({
                'ir'   : parse(0),
                'red'  : parse(3),
                'green': parse(6),
                'blue' : parse(9),
            })
        return results

    # ── Shutdown / cleanup ────────────────────────────────────────────
    def shutdown(self):
        self._write(REG_MODE_CFG1, 0x80)

    def close(self):
        # Don't let a failed shutdown (e.g. sensor unplugged) raise a
        # second error that hides the original one
        try:
            self.shutdown()
        except OSError as e:
            print(f"  [WARN] Sensor shutdown failed: {e}")
        self.bus.close()


# ── Absorbance calculation ────────────────────────────────────────────
def absorbance(i0, i):
    """Beer-Lambert: A = log10(I0 / I)"""
    if i0 <= 0 or i <= 0:
        return float('nan')
    return np.log10(i0 / i)


# ── Baseline collection ───────────────────────────────────────────────
def collect_baseline(sensor, n=150):
    print(f"Collecting baseline ({n} samples) — keep sensor uncovered...")
    sensor.clear_fifo()
    vals = {k: [] for k in ('ir', 'red', 'green', 'blue')}

    while len(vals['ir']) < n:
        avail = sensor.available_samples()
        if avail:
            take = min(avail, n - len(vals['ir']))
            for s in sensor.read_samples(take):
                for k in vals:
                    vals[k].append(s[k])
        time.sleep(0.01)

    baseline = {k: float(np.mean(vals[k])) for k in vals}
    print("  Baseline counts:")
    for k, v in baseline.items():
        print(f"    {k.upper():5s}: {v:,.0f}")
    return baseline

# ── LCD Display using RPLCD ───────────────────────────────────────────
class LCDDisplay:
    """
    16x2 I2C LCD using RPLCD library.
    Cycles between IR/Red and Green/Blue every 3 seconds.

    Screen 0:              Screen 1:
    ┌────────────────┐     ┌────────────────┐
    │IR : +0.1234    │     │GR : +0.2345    │
    │RED: +0.5678    │     │BLU: +0.6789    │
    └────────────────┘     └────────────────┘
    """

    def __init__(self,
                 addr=0x27,
                 port=1,
                 cols=16,
                 rows=2):
        self.cols             = cols
        self._lock            = threading.Lock()
        self._screen          = 0
        self._switch_interval = 3.0
        self._last_switch     = time.time()
        self._last_abs        = {
            'ir'   : float('nan'),
            'red'  : float('nan'),
            'green': float('nan'),
            'blue' : float('nan'),
        }
        self._running = False
        self._thread  = None

        try:
            self.lcd = CharLCD(
                i2c_expander='PCF8574',
                address=addr,
                port=port,
                cols=cols,
                rows=rows,
                dotsize=8,
                charmap='A02',
                auto_linebreaks=False,
                backlight_enabled=True,
            )
            self.lcd.clear()
            self._write_line(0, "   Bilimeter    ")
            self._write_line(1, "  Initialising..")
            print(f"  LCD initialised at 0x{addr:02X} ✓")
        except Exception as e:
            print(f"  [WARN] LCD init failed: {e}")
            print(f"  Check address (try 0x3F if 0x27 fails) and wiring.")
            self.lcd = None

    def _write_line(self, row, text):
        """Write text to a row, padded to full width."""
        if self.lcd is None:
            return
        padded = text[:self.cols].ljust(self.cols)
        self.lcd.cursor_pos = (row, 0)
        self.lcd.write_string(padded)

    def _format_abs(self, label, value):
        """Format one absorbance reading for one LCD line."""
        if value != value:    # nan check
            return f"{label}: ------"
        return f"{label}: {value:+.4f}"

    def _refresh(self):
        """Redraw LCD with latest absorbance values."""
        if self.lcd is None:
            return

        abs_ = self._last_abs
        now  = time.time()

        if now - self._last_switch >= self._switch_interval:
            self._screen      = 1 - self._screen
            self._last_switch = now

        screens = {
            0: [
                ("IR ", abs_['ir']),
                ("RED", abs_['red']),
            ],
            1: [
                ("GR ", abs_['green']),
                ("BLU", abs_['blue']),
            ],
        }

        with self._lock:
            lines = screens[self._screen]
            self._write_line(
                0, self._format_abs(lines[0][0], lines[0][1])
            )
            self._write_line(
                1, self._format_abs(lines[1][0], lines[1][1])
            )

    def _update_loop(self):
        while self._running:
            try:
                self._refresh()
            except Exception as e:
                print(f"  [WARN] LCD update error: {e}")
            time.sleep(0.5)

    def start(self):
        """Start background LCD refresh thread."""
        self._running = True
        self._thread  = threading.Thread(
            target=self._update_loop, daemon=True
        )
        self._thread.start()

    def update(self, abs_ir, abs_red, abs_green, abs_blue):
        """Call from main loop with latest absorbance values."""
        self._last_abs = {
            'ir'   : abs_ir,
            'red'  : abs_red,
            'green': abs_green,
            'blue' : abs_blue,
        }

    def show_message(self, line1, line2=""):
        """Display a custom status message."""
        if self.lcd is None:
            return
        with self._lock:
            self._write_line(0, line1)
            self._write_line(1, line2)

    def stop(self):
        """Stop background refresh thread (leaves the screen as is)."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    def close(self):
        """
        Stop thread and release the LCD without clearing it.
        Screen and backlight stay on so the final result remains
        visible; button_listener.py takes over the display after exit.
        """
        self.stop()
        if self.lcd:
            try:
                self.lcd.close(clear=False)
            except Exception:
                pass


# ── Main ──────────────────────────────────────────────────────────────
N_SKIP = 4    # first concentration readings to discard while buffers fill


def _raise_keyboard_interrupt(signum, frame):
    raise KeyboardInterrupt


def main():
    # Treat SIGTERM (systemd stop / listener fallback) like Ctrl+C so the
    # finally block always shuts the sensor down
    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)

    sensor = MAX86916(bus_num=1)

    lcd = LCDDisplay(addr=0x27)

    try:
        sensor.setup(
            ir_ma=10.0,
            red_ma=10.0,
            green_ma=10.0,
            blue_ma=10.0,
            sample_rate=1,    # 100 sps
            pulse_width=1,    # 120 µs
            adc_range=2,      # 16 µA full scale
        )

        lcd.show_message("Collecting", "baseline...")

        baseline = collect_baseline(sensor, n=150)
        sensor.clear_fifo()

        print("Calibration complete! Measurement in 5 seconds...\n")
        lcd.show_message("Place sensor", "on skin...")
        time.sleep(5)
        # FIFO filled and stopped during the wait; drop those samples
        # (taken before the sensor was placed on skin)
        sensor.clear_fifo()

        lcd.start()

        print("Place sensor on skin. Press Ctrl+C to stop.\n")
        print(f"{'IR':>10} {'RED':>10} {'GREEN':>10} {'BLUE':>10} "
              f"{'Abs_IR':>8} {'Abs_RED':>8} {'Abs_GRN':>8} {'Abs_BLU':>8}"
              f" {'Conc' :>10}")
        print("-" * 95)

        # Rolling buffers for smoothing
        buf = {k: deque(maxlen=50) for k in ('ir', 'red', 'green', 'blue')}

        t = 0
        Conc = []
        while t < 100:
            avail = sensor.available_samples()
            if avail:
                for s in sensor.read_samples(avail):
                    for k in buf:
                        buf[k].append(s[k])

            if len(buf['ir']) >= 10:
                mean = {k: float(np.mean(buf[k])) for k in buf}
                abs_ = {k: absorbance(baseline[k], mean[k]) for k in mean}
                
                lcd.update(
                    abs_['ir'],
                    abs_['red'],
                    abs_['green'],
                    abs_['blue'],
                )

                # Baseline-normalised form of C = D*log10(E * G^alpha / B)
                bili_index = abs_['blue'] - CAL_ALPHA * abs_['green']

                print(
                    f"{mean['ir']:>10.2f} "
                    f"{mean['red']:>10.2f} "
                    f"{mean['green']:>10.2f} "
                    f"{mean['blue']:>10.2f}  "
                    f"{abs_['ir']:>8.4f} "
                    f"{abs_['red']:>8.4f} "
                    f"{abs_['green']:>8.4f} "
                    f"{abs_['blue']:>8.4f} "
                    f"{bili_index:>10.4f}"
                )
                
                Conc.append(bili_index)
            

            time.sleep(0.1)
            t += 1
            
        lcd.stop()    # stop absorbance refresh so it can't overwrite the result

        if len(Conc) <= N_SKIP:
            print("Not enough readings to estimate concentration.")
            lcd.show_message("Too few readings", "Try again")
            sys.exit(1)

        # C = D * (A_blue - alpha * A_green) + offset
        # CAL_* constants are PLACEHOLDERS until fitted to reference data,
        # so this is not a real mg/dL value yet.
        conc = CAL_D * sum(Conc[N_SKIP:]) / (len(Conc) - N_SKIP) + CAL_OFFSET
        print("Conc Average:" + f"{conc:>8.4f}" + " mg/dL")
        # Machine-readable line parsed by button_listener.py
        print(f"RESULT {conc:.4f}", flush=True)
        lcd.show_message("Est. bilirubin:", f"{conc:.2f} mg/dL")

    except KeyboardInterrupt:
        print("\nStopped.")
        lcd.stop()
        lcd.show_message("Stopped.")
    finally:
        lcd.close()
        sensor.close()
        print("Sensor shut down.")


if __name__ == "__main__":
    main()

