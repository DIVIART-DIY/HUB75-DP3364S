"""Encode a 160x80 RGB byte image using the confirmed Pi DP3364S timing.

No hardware access. Left, top and bottom edges are forced black; x=159
remains available to the image. Linear 8-bit
channel values map to codes 0..0x0400, matching the successful static tests.
"""
import struct
import ctypes
from pathlib import Path
import dp3364s_blue as panel


class Encoder:
    def __init__(self, mux_blank=3, scan_delay=0, full_pwm_cycle=False):
        self.template = bytes(panel.pixels(panel.build_words('black', mux_blank=mux_blank,
                                                            scan_delay=scan_delay,
                                                            full_pwm_cycle=full_pwm_cycle)))
        self.masks = [tuple(panel.transport.bit(c+str(half)) for c in ('B', 'G', 'R'))
                      for half in (1, 2)]
        self.codes = [v*panel.LEVEL//255 for v in range(256)]

    def encode(self, rgb):
        if len(rgb) != 160*80*3:
            raise ValueError('Expected 160x80 RGB image')
        result = bytearray(self.template)
        for y in range(1, 79):
            masks = self.masks[y//40]
            row_base = panel.WORDS_PER_LINE + (y % 40)*16*161
            for x in range(1, 160):
                pixel = (y*160+x)*3
                first_word = row_base + (x % 16)*161 + (x//16)*16
                for channel, mask in enumerate(masks):
                    code = self.codes[rgb[pixel+channel]]
                    while code:
                        bit = code.bit_length()-1
                        offset = (first_word + 15-bit)*8
                        lo, hi = struct.unpack_from('<II', result, offset)
                        struct.pack_into('<II', result, offset, lo | mask, hi | mask)
                        code ^= 1 << bit
        return bytes(result)


class NativeEncoder:
    def __init__(self, max_level=panel.LEVEL, mux_blank=3, scan_delay=0,
                 full_pwm_cycle=False):
        if not 0 <= max_level <= 65535:
            raise ValueError('Maximum PWM code must fit 16 bits')
        self.max_level = max_level
        self.template = Encoder(mux_blank, scan_delay, full_pwm_cycle).template
        self.output = ctypes.create_string_buffer(len(self.template))
        self.lib = ctypes.CDLL(str(Path(__file__).with_name('libpanel_encode.so')))
        self.function = self.lib.panel_encode_v2
        self.function.restype = ctypes.c_int
        self.function.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
            ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t,
            ctypes.c_uint, ctypes.c_uint]

    def encode(self, rgb, brightness=100):
        rgb = bytes(rgb)
        if len(rgb) != 160*80*3 or not 0 <= brightness <= 100:
            raise ValueError('Invalid RGB frame or brightness')
        result = self.function(rgb, len(rgb), self.template, len(self.template),
                               self.output, len(self.output), int(brightness), self.max_level)
        if result:
            raise RuntimeError('Native frame encoding failed')
        return self.output.raw
