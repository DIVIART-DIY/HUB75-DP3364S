"""Offline DP3364S/DPI encoding check and read-only Raspberry Pi inventory.

No GPIO access, DRM device opens, application imports, installs or file writes.
This is NOT a display driver and does not enable DPI.
"""
import argparse
import glob
import json
import platform
from pathlib import Path
import subprocess
import time

# Physical HUB75 signal -> BCM GPIO, Triple Bonnet Port 1.
PINS = dict(R1=11, G1=27, B1=7, R2=8, G2=9, B2=10,
            CLK=17, LAT=4, ROW=18, A=22, B=23, C=24, D=25, E=15)
REGISTERS = (0x1100, 0x0227, 0x033F, 0x043F, 0x0504, 0x0642,
             0x0700, 0x08BF, 0x0960, 0x0ABE, 0x0B8B, 0x0C08, 0x0D12)


def mask(*signals):
    # RGB888 DPI D0..23 are GPIO4..27. This is a logical 24-bit word,
    # NOT yet a verified DRM framebuffer byte layout.
    return sum(1 << (PINS[s] - 4) for s in signals)


def blue_frame():
    """Port of buildDmaFrame timing for full blue, black 1-pixel perimeter."""
    words = []
    for _ in range(40):
        words.extend((mask('B'), mask('B', 'A'), mask('B')))
    words.extend(mask('B') | (mask('LAT') if i < 3 else 0) for i in range(20))
    for p in range(115200):
        phase, row = p % 128, (p // 128) % 40
        mux = (mask('C') if row == 0 else 0)
        mux |= mask('B') if phase < 3 else 0
        mux |= mask('A') if phase == 1 else 0
        word = mux
        if 3 <= phase < 3 + (12 if p < 128 else 4):
            word |= mask('ROW')
        if p < 102400:
            latch, i = divmod(p, 160)
            y = latch // 16
            x = (i // 16) * 16 + latch % 16
            if (0x0400 >> (15 - i % 16)) & 1 and 0 < x < 159:
                # Confirmed R/B swap: physical R1/R2 lights logical BLUE.
                if y != 0:
                    word |= mask('R1')
                if y != 39:
                    word |= mask('R2')
        words.append(word)
        if p < 102400 and (p + 1) % 160 == 0:
            words.append(mux | mask('LAT'))
    return words


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def check():
    start = time.perf_counter()
    require(len(set(PINS.values())) == len(PINS), 'GPIO overlap')
    for gpio in PINS.values():
        require(4 <= gpio <= 27, 'GPIO outside DPI data bus')
    # Exercise all combinations of the 14 routed signals, clock included.
    names = tuple(PINS)
    for value in range(1 << len(names)):
        packed = mask(*(s for i, s in enumerate(names) if value & (1 << i)))
        decoded = sum((1 << i) for i, s in enumerate(names)
                      if packed & (1 << (PINS[s] - 4)))
        require(decoded == value, 'DPI mapping round-trip')
    words = blue_frame()
    require(len(words) == 115980, 'frame length')
    require(all(not w & mask('CLK', 'D', 'E') for w in words), 'reserved bit')
    # Inspect command pulses, including wrap between repeated frames.
    def widths(bit):
        runs, n = [], 0
        for w in words + [words[0]]:
            if w & bit:
                n += 1
            elif n:
                runs.append(n)
                n = 0
        return runs
    require(widths(mask('LAT')) == [3] + [1] * 640, 'LAT commands')
    require(widths(mask('ROW')) == [12] + [4] * 899, 'ROW commands')
    # Independently decode every pixel code from its sixteen serial bits.
    for y in range(40):
        for x in range(160):
            latch = y * 16 + x % 16
            base = 140 + latch * 161 + (x // 16) * 16
            for signal, yy in (('R1', y), ('R2', y + 40)):
                code = 0
                for bit in range(16):
                    code = (code << 1) | bool(words[base + bit] & mask(signal))
                expected = 0x0400 if 0 < x < 159 and 0 < yy < 79 else 0
                require(code == expected, 'pixel decoding')
    # CLK will be encoded as two DPI pixels per DP3364S clock.
    for w in words:
        low, high = w, w | mask('CLK')
        require(low ^ high == mask('CLK'), 'CLK changes other signals')
    return dict(status='PASS', words=len(words), dpi_pixels=2 * len(words),
                requested_dclk_hz=8000000, requested_dpi_pixel_hz=16000000,
                ideal_frame_hz=8000000 / len(words),
                seconds=round(time.perf_counter() - start, 3),
                limitation='Logical encoding only; no DPI, DMA or electrical timing tested')


def read(path):
    try:
        return Path(path).read_text(errors='replace').replace('\x00', '').strip()
    except OSError as exc:
        return 'unavailable: ' + str(exc)


