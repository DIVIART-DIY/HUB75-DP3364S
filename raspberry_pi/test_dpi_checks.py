"""Hardware-independent regression checks for KMS success criteria."""
import unittest
from unittest.mock import patch
import dpi_transport as dpi


class Sequence:
    def __init__(self, pairs, error=0):
        self.pairs = iter(pairs)
        self.error = error

    def drmCrtcGetSequence(self, fd, crtc, sequence, timestamp):
        if self.error:
            return self.error
        sequence._obj.value, timestamp._obj.value = next(self.pairs)
        return 0


class Checks(unittest.TestCase):
    def test_no_sync_profile_rejected_before_hardware_access(self):
        args = type('Args', (), {'timing': 'continuous'})()
        with patch.object(dpi, 'guard') as guard, patch.object(dpi, 'library') as library:
            with self.assertRaisesRegex(RuntimeError, 'disabled'):
                dpi.run(args)
            guard.assert_not_called()
            library.assert_not_called()

    def test_porches_mode_has_all_sync_intervals(self):
        m = dpi.timing_mode('porches')
        self.assertEqual((m.hdisplay, m.hsync_start, m.hsync_end, m.htotal),
                         (1920, 1936, 1968, 2000))
        self.assertEqual((m.vdisplay, m.vsync_start, m.vsync_end, m.vtotal),
                         (128, 131, 133, 136))

    def test_valid_frame_progress_never_claims_bonnet_or_clock_verified(self):
        pairs = [(100+i*3, 1_000_000_000+i*51_000_000) for i in range(6)]
        result = dpi.verify_frame_progress(Sequence(pairs), 1, 75,
                                           dpi.timing_mode('porches'), wait=lambda _: None)
        self.assertAlmostEqual(result['drm_frame_hz'], 58.823529, places=5)
        self.assertTrue(result['drm_frame_progress'])
        self.assertFalse(result['connector_clock_measured'])
        self.assertFalse(result['bonnet_output_verified'])

    def test_setcrtc_success_without_frames_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'NOT confirmed'):
            dpi.verify_frame_progress(Sequence([(10, 1000)]*6), 1, 75,
                                      dpi.timing_mode('porches'), wait=lambda _: None)

    def test_backwards_frame_sequence_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'backwards'):
            dpi.verify_frame_progress(Sequence([(10, 1000), (9, 2000)]*3), 1, 75,
                                      dpi.timing_mode('porches'), wait=lambda _: None)

    def test_incorrect_frame_rate_rejected(self):
        pairs = [(i, 1_000_000_000+i*1_000_000_000) for i in range(6)]
        with self.assertRaisesRegex(RuntimeError, 'differs'):
            dpi.verify_frame_progress(Sequence(pairs), 1, 75,
                                      dpi.timing_mode('porches'), wait=lambda _: None)

    def test_ioctl_failure_not_accepted(self):
        with self.assertRaisesRegex(RuntimeError, 'Read DRM'):
            dpi.verify_frame_progress(Sequence([], -1), 1, 75,
                                      dpi.timing_mode('porches'), wait=lambda _: None)


if __name__ == '__main__':
    unittest.main()
