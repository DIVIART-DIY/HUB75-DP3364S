"""DP3364S register, scan-word and GPIO primitives. Reference primitives for the later full-PWM stage."""
import argparse
import json
import mmap
import os
import signal
import struct
import subprocess
from collections import Counter
import dpi_transport as transport
from protocol_reference import REGISTERS, blue_frame

PINS = transport.PINS
B, A, C = (transport.bit(n) for n in ('B', 'A', 'C'))
LAT, ROW = (transport.bit(n) for n in ('LAT', 'ROW'))
R1, R2 = (transport.bit(n) for n in ('R1', 'R2'))
WORDS_PER_LINE = 644
WIDTH, HEIGHT = WORDS_PER_LINE * 2, 182
LEVEL = 0x0400
COLOURS = {'blue': ('B',), 'red': ('R',), 'green': ('G',),
           'white': ('R', 'G', 'B'), 'yellow': ('R', 'G'),
           'cyan': ('G', 'B'), 'magenta': ('R', 'B'), 'black': ()}


def expected_pixel(x, y, colour='blue', pattern='solid'):
    if not (0 < x < 159 and 0 < y < 79):
        return (0, 0, 0)
    channels = COLOURS[colour]
    if pattern == 'gradients':
        step = min(15, (x-1)*16//158)
        value = LEVEL*step//15
        band = min(3, (y-1)*4//78)
        return ((value, value, value) if band == 0 else
                (value, 0, 0) if band == 1 else
                (0, value, 0) if band == 2 else (0, 0, value))
    if pattern == 'geometry':
        channels = ()
        if x in (1, 158) or y in (1, 78):
            channels = COLOURS['white']
        if 3 <= x <= 7 and 3 <= y <= 7:
            channels = COLOURS['red']
        if 152 <= x <= 156 and 3 <= y <= 7:
            channels = COLOURS['green']
        if 3 <= x <= 7 and 72 <= y <= 76:
            channels = COLOURS['blue']
        if 152 <= x <= 156 and 72 <= y <= 76:
            channels = COLOURS['white']
        if (x in (79, 80) and 34 <= y <= 45) or (y in (39, 40) and 74 <= x <= 85):
            channels = COLOURS['cyan']
    return tuple(LEVEL if channel in channels else 0 for channel in ('R', 'G', 'B'))


def colour_word(word, colour):
    # Blue carrier data comes from the verified generator. R/B are physically
    # swapped on this panel; green stays on G1/G2.
    result = word & ~(R1 | R2)
    for half, carrier in ((1, R1), (2, R2)):
        if word & carrier:
            for channel in COLOURS[colour]:
                physical = {'R': 'B', 'G': 'G', 'B': 'R'}[channel]
                result |= transport.bit(physical + str(half))
    return result


def clear_words():
    return [w for _ in range(40) for w in (B, B | A, B)]


def scan_word(p, painter=None, mux_blank=3, scan_delay=0):
    scan = p - scan_delay
    phase, row = scan % 128, (scan // 128) % 40
    mux = (C if row == 0 else 0) | (B if phase < mux_blank else 0) | (A if phase == 1 else 0)
    word = mux | (ROW if mux_blank <= phase < mux_blank + (12 if scan < 128 else 4) else 0)
    if scan < 0:
        mux = word = B  # Hold the cleared selector until the delayed scan starts.
    if p < 102400:
        latch, i = divmod(p, 160)
        y, x = latch // 16, (i // 16) * 16 + latch % 16
        if painter is not None:
            for half, yy in ((1, y), (2, y+40)):
                values = painter(x, yy)
                for physical, code in zip(('B', 'G', 'R'), values):
                    if (code >> (15-i % 16)) & 1:
                        word |= transport.bit(physical + str(half))
        elif (LEVEL >> (15-i % 16)) & 1 and 0 < x < 159:
            word |= R1 if y > 0 else 0
            word |= R2 if y < 39 else 0
    return word, mux


def build_words(colour='blue', pattern='solid', mux_blank=3, scan_delay=0,
                full_pwm_cycle=False):
    if mux_blank not in (3, 8):
        raise ValueError('Supported mux blanking: 3 or 8 clocks')
    if scan_delay not in (0, 16):
        raise ValueError('Supported scan delays: 0 or 16 clocks')
    painter = (lambda x, y: expected_pixel(x, y, colour, pattern)) if pattern != 'solid' else None
    # Header fits one whole scanline: blank/clear, idle, then VSYNC.
    lines = [clear_words() + [B]*504 + [B | LAT]*3 + [B]*17]
    # 640 scan clocks = 5 row periods and 4 complete DATA_LATCH groups.
    # Each horizontal blank begins with ROW low, never splits W12 or W4.
    for block in range(160):
        words = []
        for p in range(block*640, (block+1)*640):
            word, mux = scan_word(p, painter, mux_blank, scan_delay)
            words.append(word)
            if (p+1) % 160 == 0:
                words.append(mux | LAT)
        lines.append(words)
    # Preserve the confirmed 12800 scan-clock tail; fill the unused 4 slots.
    tail_lines = 352 if full_pwm_cycle else 20
    for block in range(tail_lines):
        first = 102400 + block*640
        lines.append([scan_word(p, mux_blank=mux_blank, scan_delay=scan_delay)[0]
                      for p in range(first, first+640)] + [B]*4)
    # Clear ALL row tokens before vertical blanking forces DPI data to zero.
    lines.append(clear_words() + [B]*524)
    if len(lines) != (514 if full_pwm_cycle else HEIGHT) or any(len(line) != WORDS_PER_LINE for line in lines):
        raise RuntimeError('DPI scanline geometry')
    return ([[colour_word(w, colour) for w in line] for line in lines]
            if colour != 'blue' and pattern == 'solid' else lines)


def validate(lines, colour='blue', pattern='solid'):
    # Remove only the deliberate padding and compare with the confirmed
    # ESP32 protocol port. No source ESP32 firmware is changed.
    restored = lines[0][:120] + lines[0][-20:]
    restored += [w for line in lines[1:161] for w in line]
    restored += [w for line in lines[161:181] for w in line[:640]]
    reference = blue_frame()
    if colour != 'blue':
        reference = [colour_word(w, colour) for w in reference]
    rgb = sum(transport.bit(n) for n in transport.DATA)
    compare = (lambda values: [w & ~rgb for w in values]) if pattern != 'solid' else list
    if compare(restored) != compare(reference):
        raise RuntimeError('Protocol differs from confirmed frame')
    words = [w for line in lines for w in line]
    allowed = sum(transport.bit(n) for n in PINS if n != 'CLK')
    if any(w & ~allowed for w in words):
        raise RuntimeError('Unexpected GPIO bit')
    # Parse rising edges and pulse widths, also across the circular boundary.
    selector, previous, row_width, lat_width = 0, words[-1], 0, 0
    rows, latches, edges = [], [], 0
    for w in words + [words[0]]:
        if w & A and not previous & A:
            selector = ((selector << 1) | bool(w & C)) & ((1 << 40)-1)
            edges += 1
        if w & ROW:
            if w & B or not selector or selector & (selector-1):
                raise RuntimeError('Invalid row enable/token')
            row_width += 1
        elif row_width:
            rows.append(row_width)
            row_width = 0
        if w & LAT:
            lat_width += 1
        elif lat_width:
            latches.append(lat_width)
            lat_width = 0
        previous = w
    if rows != [12]+[4]*899 or latches != [3]+[1]*640 or edges != 980 or selector:
        raise RuntimeError('Command widths, token reset or row count')
    if any(line[-1] & (ROW | A) for line in lines):
        raise RuntimeError('Horizontal gap splits ROW or mux clock')
    # Independently decode all 12800 pixel channel codes.
    data = [w for line in lines[1:161] for w in line]
    for y in range(40):
        for x in range(160):
            base = (y*16+x % 16)*161 + (x//16)*16
            for half, yy in ((1, y), (2, y+40)):
                expected_codes = expected_pixel(x, yy, colour, pattern)
                for physical, index in (('R', 2), ('G', 1), ('B', 0)):
                    signal = transport.bit(physical + str(half))
                    code = 0
                    for w in data[base:base+16]:
                        code = (code << 1) | bool(w & signal)
                    expected = expected_codes[index]
                    if code != expected:
                        raise RuntimeError('Pixel coordinate/colour decoding')
    return dict(logical_frame_test='PASS', dma_words=len(words),
                original_protocol_words=len(restored), row_pulses=len(rows),
                lat_width_counts=dict(Counter(latches)), width=WIDTH, height=HEIGHT,
                requested_active_dclk_hz=8000000,
                requested_frame_hz=16000000/((WIDTH+3)*(HEIGHT+3)),
                horizontal_gap_ns=3/16000000*1e9,
                vertical_gap_us=3*(WIDTH+3)/16000000*1e6,
                colour=colour, pattern=pattern, level=LEVEL, hardware_verified=False)


def pixels(lines):
    result = bytearray(sum(len(line) for line in lines)*8)
    clock = transport.bit('CLK')
    offset = 0
    for line in lines:
        for word in line:
            struct.pack_into('<II', result, offset, word, word | clock)
            offset += 8
    return result


class GPIO:
    def __enter__(self):
        self.fd = os.open('/dev/gpiomem', os.O_RDWR | os.O_CLOEXEC)
        self.mem = mmap.mmap(self.fd, 4096, flags=mmap.MAP_SHARED,
                             prot=mmap.PROT_READ | mmap.PROT_WRITE)
        self.bus = sum(1 << pin for pin in PINS.values())
        return self

    def write(self, state):
        struct.pack_into('<I', self.mem, 0x28, self.bus & ~state)
        struct.pack_into('<I', self.mem, 0x1c, self.bus & state)

    def clock(self, state):
        self.write(state & ~(1 << PINS['CLK']))
        struct.pack_into('<I', self.mem, 0x1c, 1 << PINS['CLK'])
        struct.pack_into('<I', self.mem, 0x28, 1 << PINS['CLK'])

    def __exit__(self, *_):
        self.mem.close()
        os.close(self.fd)


def pin_output():
    # Blank row switches first; sensor pins are never touched.
    subprocess.run(['pinctrl', 'set', str(PINS['B']), 'op', 'dh'], check=True)
    for name, pin in PINS.items():
        if name != 'B':
            subprocess.run(['pinctrl', 'set', str(pin), 'op', 'dl'], check=True)


def clear_mux(gpio):
    b, a = 1 << PINS['B'], 1 << PINS['A']
    for _ in range(40):
        for state in (b, b | a, b):
            gpio.write(state)


def register_profile(remove_bad_spots=False):
    # DP3364S Rev1.0 EN, section 10.9: REG0C bit 1 enables removal
    # of open-circuit bad-spot crosses. Preserve every other register bit.
    return tuple(reg | 2 if remove_bad_spots and reg >> 8 == 0x0c else reg
                 for reg in REGISTERS)


def hold_row_disabled():
    subprocess.run(['pinctrl', 'set', str(PINS['B']), 'op', 'dh'], check=True)


def release_row_to_dma():
    subprocess.run(['pinctrl', 'set', str(PINS['B']), 'a2'], check=True)
    print('Row switches released to verified black DMA frame', flush=True)


def initialise(remove_bad_spots=False, hold_blank=False):
    pin_output()
    try:
        with GPIO() as gpio:
            clear_mux(gpio)
            blank, lat = 1 << PINS['B'], 1 << PINS['LAT']
            rgb = sum(1 << PINS[n] for n in transport.DATA)
            def clocks(state, count):
                for _ in range(count):
                    gpio.clock(blank | state)
            # Same SDR / VSYNC / PRE_ACT / WR_CFG as src/main.cpp.
            clocks(lat, 15)
            clocks(0, 1)
            for reg in register_profile(remove_bad_spots):
                clocks(lat, 3)
                clocks(0, 17)
                clocks(lat, 14)
                clocks(0, 1)
                for i in range(160):
                    state = rgb if (reg >> (15-i % 16)) & 1 else 0
                    if i >= 155:
                        state |= lat
                    clocks(state, 1)
                clocks(rgb if reg & 1 else 0, 1)
            clear_mux(gpio)
            gpio.write(blank)
    finally:
        for pin in PINS.values():
            if hold_blank and pin == PINS['B']:
                continue
            subprocess.run(['pinctrl', 'set', str(pin), 'a2'], check=True)
    print('DP3364S initialised; row tokens cleared; GPIO returned to DPI', flush=True)


def blank_after_stop(hold_blank=False):
    pin_output()
    with GPIO() as gpio:
        clear_mux(gpio)
        gpio.write(1 << PINS['B'])
    # Production stop: park ALL panel pins as static GPIO. Returning data,
    # CLK/LAT/ROW to an inactive DPI peripheral can expose shutdown transitions.
    if not hold_blank:
        for pin in PINS.values():
            subprocess.run(['pinctrl', 'set', str(pin), 'a2'], check=True)
    print('Output stopped; all row tokens cleared; ' +
          ('panel pins parked as GPIO' if hold_blank else 'pins returned to DPI'), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--test-boot', action='store_true')
    parser.add_argument('--dp3364s-connected', action='store_true')
    parser.add_argument('--seconds', type=int, default=30, choices=range(1, 301))
    parser.add_argument('--colour', choices=tuple(COLOURS), default='blue')
    parser.add_argument('--pattern', choices=('solid', 'geometry', 'gradients'), default='solid')
    args = parser.parse_args()
    lines = build_words(args.colour, args.pattern)
    print(json.dumps(validate(lines, args.colour, args.pattern), indent=2))
    if not args.run:
        return
    if not args.dp3364s_connected or not args.test_boot:
        raise SystemExit('Require --test-boot --dp3364s-connected; NEVER connect FM6363C')
    transport.WIDTH, transport.HEIGHT = WIDTH, HEIGHT
    mode = transport.Mode(clock=16000, hdisplay=WIDTH, hsync_start=WIDTH+1,
                          hsync_end=WIDTH+2, htotal=WIDTH+3,
                          vdisplay=HEIGHT, vsync_start=HEIGHT+1,
                          vsync_end=HEIGHT+2, vtotal=HEIGHT+3,
                          flags=5, type=32, name=b'dp-blue')
    args.timing, args.pattern, args.device = ('blue-bounded-gaps',
        args.colour if args.pattern == 'solid' else args.pattern, '/dev/dri/card0')
    # Hardware configuration checks remain mandatory. The explicit panel flag
    # replaces transport-probe's requirement to have NO panel attached.
    def stop(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    transport.run(args, mode_override=mode, frame_pixels=pixels(lines),
                  before_enable=initialise, after_disable=blank_after_stop,
                  safety_check=transport.guard_configuration)


if __name__ == '__main__':
    main()
