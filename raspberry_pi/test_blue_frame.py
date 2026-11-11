import struct
import unittest
from unittest.mock import patch
import dp3364s_blue as blue


class FrameChecks(unittest.TestCase):
    def test_gradients_decode_and_monotonic_levels(self):
        result = blue.validate(blue.build_words(pattern='gradients'), pattern='gradients')
        self.assertEqual(result['pattern'], 'gradients')
        for y, active in ((10, (0, 1, 2)), (30, (0,)), (50, (1,)), (70, (2,))):
            levels = [blue.expected_pixel(x, y, pattern='gradients') for x in range(1, 159)]
            for channel in range(3):
                values = [v[channel] for v in levels]
                if channel in active:
                    self.assertEqual(values, sorted(values))
                    self.assertEqual(len(set(values)), 16)
                    self.assertEqual((values[0], values[-1]), (0, blue.LEVEL))
                else:
                    self.assertEqual(set(values), {0})
    def test_geometry_corners_centre_and_dark_edge(self):
        result = blue.validate(blue.build_words(pattern='geometry'), pattern='geometry')
        self.assertEqual(result['pattern'], 'geometry')
        pixel = lambda x, y: blue.expected_pixel(x, y, pattern='geometry')
        level = blue.LEVEL
        self.assertEqual(pixel(3, 3), (level, 0, 0))
        self.assertEqual(pixel(152, 3), (0, level, 0))
        self.assertEqual(pixel(3, 72), (0, 0, level))
        self.assertEqual(pixel(152, 72), (level, level, level))
        self.assertEqual(pixel(79, 39), (0, level, level))
        self.assertEqual(pixel(80, 40), (0, level, level))
        self.assertEqual(pixel(1, 20), (level, level, level))
        self.assertEqual(pixel(0, 20), (0, 0, 0))
        self.assertEqual(pixel(20, 79), (0, 0, 0))
    def test_red_preserves_timing_and_uses_only_physical_b_lines(self):
        reference = blue.build_words()
        red = blue.build_words('red')
        rgb = sum(blue.transport.bit(n) for n in blue.transport.DATA)
        control = lambda lines: [[w & ~rgb for w in line] for line in lines]
        self.assertEqual(control(reference), control(red))
        allowed = blue.transport.bit('B1') | blue.transport.bit('B2')
        self.assertTrue(any(w & allowed for line in red for w in line))
        self.assertFalse(any(w & rgb & ~allowed for line in red for w in line))
        self.assertEqual(blue.validate(red, 'red')['colour'], 'red')
    def test_frame_and_independent_pixel_decoder(self):
        result = blue.validate(blue.build_words())
        self.assertEqual(result['row_pulses'], 900)
        self.assertEqual(result['original_protocol_words'], 115980)
        self.assertFalse(result['hardware_verified'])

    def test_horizontal_and_vertical_blanks_do_not_add_clock_edges(self):
        lines = blue.build_words()
        encoded = blue.pixels(lines)
        expected = [w for line in lines for w in line]
        clock = blue.transport.bit('CLK')
        previous, received = 0, []
        for y in range(blue.HEIGHT):
            row = encoded[y*blue.WIDTH*4:(y+1)*blue.WIDTH*4]
            # Model the conservative all-zero data output in each porch.
            states = list(struct.unpack('<'+'I'*blue.WIDTH, row)) + [0]*3
            if y == blue.HEIGHT-1:
                states += [0]*((blue.WIDTH+3)*3)
            for value in states:
                if value & clock and not previous & clock:
                    received.append(value & ~clock)
                previous = value
        self.assertEqual(received, expected)
        self.assertEqual(len(received), 117208)

    def test_corrupt_pixel_is_rejected(self):
        lines = blue.build_words()
        lines[1][0] ^= blue.R1
        with self.assertRaises(RuntimeError):
            blue.validate(lines)

    def test_initialisation_matches_register_commands(self):
        captured = []
        class FakeGPIO:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def write(self, _): pass
            def clock(self, state): captured.append(state)
        with patch.object(blue, 'GPIO', FakeGPIO), patch.object(blue, 'pin_output'), \
             patch.object(blue.subprocess, 'run'), patch('builtins.print'):
            blue.initialise()
        b, lat = 1 << blue.PINS['B'], 1 << blue.PINS['LAT']
        rgb = sum(1 << blue.PINS[n] for n in blue.transport.DATA)
        self.assertEqual(captured[:16], [b|lat]*15+[b])
        self.assertEqual(len(captured), 16+196*len(blue.REGISTERS))
        self.assertTrue(all(w & b for w in captured))
        self.assertTrue(all(not w & (1 << blue.PINS['ROW']) for w in captured))
        for index, register in enumerate(blue.REGISTERS):
            words = captured[16+index*196:16+(index+1)*196]
            self.assertEqual(words[:20], [b|lat]*3+[b]*17)
            self.assertEqual(words[20:35], [b|lat]*14+[b])
            for i, w in enumerate(words[35:195]):
                self.assertEqual(w & rgb, rgb if register & (1 << (15-i % 16)) else 0)
                self.assertEqual(bool(w & lat), i >= 155)
            self.assertFalse(words[-1] & lat)


if __name__ == '__main__':
    unittest.main()
