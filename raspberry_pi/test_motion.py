import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch
import dp3364s_blue as panel
from panel_frame import Encoder
from dp3364s_motion import picture, FRAME_COUNT, text_origin
import drm_presenter as presenter


class MotionTests(unittest.TestCase):
    def test_text_advances_one_pixel_without_repeated_positions(self):
        positions = [text_origin(i) for i in range(FRAME_COUNT)]
        self.assertEqual(set(b-a for a, b in zip(positions, positions[1:])), {-1})
        self.assertGreaterEqual(positions[0], 159)
        self.assertLessEqual(positions[-1]+105, 0)
    def test_motion_line_never_jumps_more_than_one_pixel(self):
        # Row 2 contains only the line, allowing an independent raster check.
        positions = []
        for frame in range(FRAME_COUNT):
            rgb = picture(frame)
            lit = [x for x in range(160) if rgb[(2*160+x)*3]]
            self.assertEqual(len(lit), 1)
            positions.append(lit[0])
        self.assertEqual((min(positions), max(positions)), (1, 158))
        for a, b in zip(positions, positions[1:]+positions[:1]):
            self.assertEqual(abs(a-b), 1)

    def test_compressed_frames_are_lossless_and_small(self):
        raw = [bytes([i])*4096 for i in range(4)]
        compressed = presenter.CompressedFrames(iter(raw))
        self.assertEqual(len(compressed), 4)
        self.assertEqual([compressed[i] for i in range(4)], raw)
        self.assertLess(compressed.stored_bytes, sum(map(len, raw))//10)
        with self.assertRaises(IndexError):
            compressed[4]
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'frames.zip'
            compressed.save(path, 'version1')
            restored = presenter.CompressedFrames.load(path, 'version1', 4)
            self.assertEqual([restored[i] for i in range(4)], raw)
            with self.assertRaises(ValueError):
                presenter.CompressedFrames.load(path, 'stale-version', 4)
    def test_general_rgb_encoder_matches_verified_geometry_waveform(self):
        rgb = bytearray()
        for y in range(80):
            for x in range(160):
                rgb.extend(255 if code else 0 for code in
                           panel.expected_pixel(x, y, pattern='geometry'))
        actual = Encoder().encode(rgb)
        expected = bytes(panel.pixels(panel.build_words(pattern='geometry')))
        self.assertEqual(actual, expected)

    def test_outer_edge_forced_black_and_rgb_input_not_modified(self):
        encoder = Encoder()
        rgb = bytes([255])*(160*80*3)
        self.assertEqual(encoder.encode(rgb), bytes(panel.pixels(panel.build_words('white'))))
        self.assertEqual(rgb, bytes([255])*(160*80*3))
        with self.assertRaises(ValueError):
            encoder.encode(b'bad size')

    def test_motion_moves_and_keeps_perimeter_dark(self):
        first, later = picture(0), picture(32)
        self.assertNotEqual(first, later)
        for image in (first, later, picture(63)):
            for y in range(80):
                for x in range(160):
                    if x in (0, 159) or y in (0, 79):
                        offset = (y*160+x)*3
                        self.assertEqual(image[offset:offset+3], b'\0\0\0')

    def test_presenter_never_writes_active_buffer_and_waits_for_ack(self):
        state = dict(active=10, pending=None, now=0.0, flips=[])
        class Memory(bytearray):
            def __init__(self, identity):
                super().__init__(32)
                self.identity = identity
            def __setitem__(self, key, value):
                if state['active'] == self.identity:
                    raise AssertionError('Writing active framebuffer')
                super().__setitem__(key, value)
            def close(self):
                if state['active'] == self.identity:
                    raise AssertionError('Closing active framebuffer')
        def ioctl(fd, request, pointer):
            obj = pointer._obj
            if request == 0xC02064B2:
                obj.handle, obj.pitch, obj.size = 77, 16, 32
            return 0
        def addfb(*args):
            args[-1]._obj.value = 20
            return 0
        def flip(fd, crtc, fb, flags, cookie):
            self.assertNotEqual(fb, state['active'])
            self.assertIsNone(state['pending'])
            state['pending'] = (fb, cookie.value)
            return 0
        def ack(fd):
            fb, cookie = state['pending']
            state['active'], state['pending'] = fb, None
            state['flips'].append(fb)
            state['now'] += 0.01
            return (cookie, 0, 0, len(state['flips']), 75)
        def sleep(seconds): state['now'] += seconds
        lib = Mock()
        lib.drmIoctl.side_effect = ioctl
        lib.drmModeAddFB.side_effect = addfb
        lib.drmModePageFlip.side_effect = flip
        lib.drmModeRmFB.return_value = 0
        obj = presenter.Presenter([bytes([i])*32 for i in range(4)])
        with patch.object(presenter.drm, 'WIDTH', 4), patch.object(presenter.drm, 'HEIGHT', 2), \
             patch.object(presenter.mmap, 'MAP_SHARED', 1, create=True), \
             patch.object(presenter.mmap, 'PROT_READ', 1, create=True), \
             patch.object(presenter.mmap, 'PROT_WRITE', 2, create=True), \
             patch.object(presenter.mmap, 'mmap', return_value=Memory(20)), \
             patch.object(presenter, 'wait_flip', side_effect=ack), \
             patch.object(presenter.time, 'monotonic', side_effect=lambda: state['now']), \
             patch.object(presenter.time, 'sleep', side_effect=sleep):
            result = obj.run(lib, 9, 75, 10, Memory(10), 16, 0.35)
            self.assertEqual(state['flips'], [20, 10, 20])
            self.assertEqual(result['flips_acknowledged'], 3)
            self.assertIsNone(state['pending'])
            state['active'] = None  # transport disables CRTC before close
            obj.close()


if __name__ == '__main__':
    unittest.main()
