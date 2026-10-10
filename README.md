# GHDC Bilimeter

A low-cost optical bilirubin meter for screening newborns for jaundice, built by the Global Health Design Collaborative (GHDC) at Case Western Reserve University for clinics with limited access to commercial transcutaneous bilirubin (TcB) meters.

The device shines light of four wavelengths into the skin, measures how much is reflected back, and converts the reflected intensities into absorbance. Bilirubin absorbs mainly blue light, while melanin and hemoglobin absorb both blue and green, so comparing green and blue absorbance is the basis for estimating bilirubin concentration.

> **Status:** research prototype under active development. Not a medical device and not validated for clinical use.

## Hardware

| Part | Notes |
|---|---|
| Raspberry Pi Zero 2 W | Runs the Python code; sensor on I2C bus 1 (`/dev/i2c-1`) |
| MikroE Heart Rate 10 Click (MAX86916) | 4-LED optical sensor (IR, red, green, blue) with a 19-bit ADC, I2C address `0x57` |

## What's in this repo

| File | What it does |
|---|---|
| `BiliMeter_Test.py` | Current test script. Drives all four LEDs, collects a baseline, and prints live raw counts and absorbance for each wavelength. |
| `TestCode.py` | Earlier version that uses only the IR and blue LEDs. |

## How it works

`BiliMeter_Test.py` includes a small driver class for the MAX86916, written directly against the datasheet's register map using `smbus2`:

1. **Initialize:** software reset, then check the part ID (expects `0x2B`) to confirm wiring.
2. **Configure:** Flex LED mode with all four LEDs in the sample sequence (IR → red → green → blue), 10 mA per LED, 100 samples/s, 120 µs pulse width, 16 µA ADC full scale. LED current is converted to the correct pulse-amplitude register value and current range automatically.
3. **Read:** pull samples from the sensor's 32-sample FIFO, parsing each LED's 3-byte reading into a 19-bit count.
4. **Baseline:** average 150 samples with the sensor uncovered to get the reference intensity *I₀* for each LED.
5. **Measure:** keep a rolling 50-sample average of each LED's intensity *I* and compute absorbance with the Beer–Lambert law, **A = log₁₀(I₀ / I)**.

## Running it

On the Raspberry Pi:

```bash
# Enable I2C: sudo raspi-config → Interface Options → I2C
pip install smbus2 numpy
i2cdetect -y 1          # the sensor should appear at 0x57
python3 BiliMeter_Test.py
```

Keep the sensor uncovered while the baseline is collected, then place it against the skin (a fingertip works for bench testing). Press `Ctrl+C` to stop; the sensor is put into shutdown mode on exit.

## Roadmap

- **Bilirubin concentration:** convert green and blue reflectance into bilirubin concentration (mg/dL) using the colorimetric model from Inamori et al., *Science Advances* 7:eabe3793 (2021). Its constants are fit separately for each device.
- **Calibration:** fit those constants against reference measurements from a commercial TcB meter or blood tests (TSB), and report agreement with a Bland–Altman analysis.
- **Standalone use:** button-triggered readings that run as a `systemd` service, so the device works without a keyboard or monitor.
- **Enclosure:** CAD housing that integrates the internal circuitry.

## Reference

Inamori, G. et al. "Neonatal wearable device for colorimetry-based real-time detection of jaundice with simultaneous sensing of vitals." *Science Advances* 7, eabe3793 (2021). https://doi.org/10.1126/sciadv.abe3793
