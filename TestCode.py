import smbus2
import time
import numpy as np

# MAX86916 I2C address
MAX86916_ADDR = 0x57  # 7-bit address (0xAE write, 0xAF read)

# Register addresses
REG_INT_STATUS1   = 0x00
REG_INT_ENABLE1   = 0x02
REG_FIFO_WR_PTR   = 0x04
REG_OVF_COUNTER   = 0x05
REG_FIFO_RD_PTR   = 0x06
REG_FIFO_DATA     = 0x07
REG_FIFO_CONFIG   = 0x08
REG_MODE_CONFIG1  = 0x09
REG_MODE_CONFIG2  = 0x0A
REG_LED1_PA       = 0x0C  # IR
REG_LED2_PA       = 0x0D  # RED
REG_LED3_PA       = 0x0E  # GREEN
REG_LED4_PA       = 0x0F  # BLUE
REG_LED_RANGE     = 0x11
REG_PILOT_PA      = 0x12
REG_LED_SEQ1      = 0x13
REG_LED_SEQ2      = 0x14
REG_PART_ID       = 0xFF


class MAX86916:
    def __init__(self, bus_num=1):
        self.bus = smbus2.SMBus(bus_num)
        self.addr = MAX86916_ADDR

    def write_reg(self, reg, value):
        self.bus.write_byte_data(self.addr, reg, value)

    def read_reg(self, reg):
        return self.bus.read_byte_data(self.addr, reg)

    def read_regs(self, reg, length):
        return self.bus.read_i2c_block_data(self.addr, reg, length)

    def verify_part_id(self):
        part_id = self.read_reg(REG_PART_ID)
        print(f"Part ID: 0x{part_id:02X} (expected 0x2B)")
        return part_id == 0x2B

    def reset(self):
        self.write_reg(REG_MODE_CONFIG1, 0x40)  # Set RESET bit
        time.sleep(0.1)

    def setup(self,
              led_ir_current=0x3F,    # ~12mA for IR (LED1), adjust as needed
              led_blue_current=0x3F,  # ~12mA for Blue (LED4)
              sample_rate=1,          # 100 sps
              pulse_width=1,          # 120 µs
              adc_range=2):           # 16µA full scale
        """
        Configure sensor for IR + Blue LED measurement.
        LEDs: LED1=IR, LED4=Blue
        """
        self.reset()

        # FIFO config: no averaging, no rollover, almost-full at 17 samples
        self.write_reg(REG_FIFO_CONFIG, 0x0F)

        # Mode config 2: ADC range, sample rate, pulse width
        # ADC_RGE[1:0] | SR[2:0] | LED_PW[1:0]
        mode2 = ((adc_range & 0x3) << 5) | ((sample_rate & 0x7) << 2) | (pulse_width & 0x3)
        self.write_reg(REG_MODE_CONFIG2, mode2)

        # Mode config 1: Flex LED mode (MODE=11)
        self.write_reg(REG_MODE_CONFIG1, 0x03)

        # LED current amplitudes
        self.write_reg(REG_LED1_PA, led_ir_current)    # IR
        self.write_reg(REG_LED4_PA, led_blue_current)  # Blue

        # LED range: all at 0x0 (0-50mA range)
        self.write_reg(REG_LED_RANGE, 0x00)

        # LED sequence: slot1=LED1(IR), slot2=LED4(Blue), rest=OFF
        # REG_LED_SEQ1: LEDC2[3:0] | LEDC1[3:0]
        self.write_reg(REG_LED_SEQ1, (0x04 << 4) | 0x01)  # LEDC2=LED4(Blue), LEDC1=LED1(IR)
        self.write_reg(REG_LED_SEQ2, 0x00)                 # LEDC3=OFF, LEDC4=OFF

        # Enable sample-ready interrupt
        self.write_reg(REG_INT_ENABLE1, 0x40)

        print("MAX86916 configured: IR (LED1) + Blue (LED4), Flex LED mode")

    def clear_fifo(self):
        self.write_reg(REG_FIFO_WR_PTR, 0x00)
        self.write_reg(REG_OVF_COUNTER, 0x00)
        self.write_reg(REG_FIFO_RD_PTR, 0x00)

    def read_fifo_samples(self, num_samples=1):
        """
        Read samples from FIFO. Each sample = 2 data items (IR + Blue), 3 bytes each = 6 bytes.
        Returns list of (ir, blue) tuples.
        """
        results = []
        bytes_per_sample = 6  # 2 LEDs x 3 bytes

        for _ in range(num_samples):
            raw = self.read_regs(REG_FIFO_DATA, bytes_per_sample)
            # Each channel is 3 bytes, MSB at [18], mask upper 5 bits
            ir   = ((raw[0] << 16) | (raw[1] << 8) | raw[2]) & 0x7FFFF
            blue = ((raw[3] << 16) | (raw[4] << 8) | raw[5]) & 0x7FFFF
            results.append((ir, blue))

        return results

    def get_num_available_samples(self):
        wr_ptr = self.read_reg(REG_FIFO_WR_PTR) & 0x1F
        rd_ptr = self.read_reg(REG_FIFO_RD_PTR) & 0x1F
        ovf    = self.read_reg(REG_OVF_COUNTER) & 0x1F

        if ovf > 0:
            return 32
        if wr_ptr >= rd_ptr:
            return wr_ptr - rd_ptr
        return wr_ptr + 32 - rd_ptr

    def close(self):
        self.bus.close()


def calculate_absorbance(i0, i):
    """
    Beer-Lambert: A = log10(I0 / I)
    i0: reference (baseline) intensity
    i:  measured intensity through tissue
    Avoid division by zero or log of zero.
    """
    if i <= 0 or i0 <= 0:
        return float('nan')
    return np.log10(i0 / i)


def collect_baseline(sensor, num_samples=100):
    """Collect baseline (reference) readings with no finger on sensor."""
    print(f"Collecting baseline ({num_samples} samples)... Keep sensor uncovered.")
    sensor.clear_fifo()
    ir_vals, blue_vals = [], []

    while len(ir_vals) < num_samples:
        n = sensor.get_num_available_samples()
        if n > 0:
            samples = sensor.read_fifo_samples(min(n, num_samples - len(ir_vals)))
            for ir, blue in samples:
                ir_vals.append(ir)
                blue_vals.append(blue)
        time.sleep(0.01)

    baseline_ir   = np.mean(ir_vals)
    baseline_blue = np.mean(blue_vals)
    print(f"Baseline IR: {baseline_ir:.1f}, Baseline Blue: {baseline_blue:.1f}")
    return baseline_ir, baseline_blue


def main():
    sensor = MAX86916(bus_num=1)

    if not sensor.verify_part_id():
        print("WARNING: Unexpected Part ID. Check wiring.")

    # Configure: ~12mA IR and Blue, 100sps, 120µs pulse, 16µA ADC range
    sensor.setup(
        led_ir_current=0x3F,
        led_blue_current=0x3F,
        sample_rate=1,    # 100 sps
        pulse_width=1,    # 120 µs
        adc_range=2       # 16µA full scale
    )

    # Collect baseline (no finger)
    baseline_ir, baseline_blue = collect_baseline(sensor, num_samples=100)

    print("\nPlace finger on sensor. Press Ctrl+C to stop.\n")
    sensor.clear_fifo()

    try:
        ir_buffer, blue_buffer = [], []
        window = 50  # rolling average window

        while True:
            n = sensor.get_num_available_samples()
            if n > 0:
                samples = sensor.read_fifo_samples(n)
                for ir, blue in samples:
                    ir_buffer.append(ir)
                    blue_buffer.append(blue)

            if len(ir_buffer) >= window:
                ir_mean   = np.mean(ir_buffer[-window:])
                blue_mean = np.mean(blue_buffer[-window:])

                abs_ir   = calculate_absorbance(baseline_ir,   ir_mean)
                abs_blue = calculate_absorbance(baseline_blue, blue_mean)

                print(f"IR: {ir_mean:8.1f} counts | Blue: {blue_mean:8.1f} counts | "
                      f"Abs_IR: {abs_ir:.4f} | Abs_Blue: {abs_blue:.4f}")

            time.sleep(0.1)

    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        sensor.close()


if __name__ == "__main__":
    main()