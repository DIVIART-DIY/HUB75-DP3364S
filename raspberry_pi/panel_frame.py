"""Encode a 160x80 RGB byte image using the confirmed Pi DP3364S timing.

No hardware access. Outer perimeter is always forced black. Linear 8-bit
channel values map to codes 0..0x0400, matching the successful static tests.
"""
import struct
import dp3364s_blue as panel


class Encoder:
    def __init__(self):
        self.template = bytes(panel.pixels(panel.build_words('black')))
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
            for x in range(1, 159):
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
