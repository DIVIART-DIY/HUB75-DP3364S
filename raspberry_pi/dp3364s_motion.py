"""Isolated white line/text motion test; no standalone panel application imports or edits."""
import argparse
import json
import signal
import hashlib
from pathlib import Path
import dp3364s_blue as panel
import dpi_transport as transport
from panel_frame import Encoder
from drm_presenter import Presenter, CompressedFrames

FRAME_COUNT = 314
TARGET_FPS = 16000000/((panel.WIDTH+3)*(panel.HEIGHT+3))/2


def text_origin(frame):
    # 314 positions at exactly one pixel per frame. Extra positions are outside
    # the right edge, so the loop resets only while the whole text is hidden.
    return 208-(frame % FRAME_COUNT)

GLYPHS = {
    'H': (17,17,17,31,17,17,17), 'U': (17,17,17,17,17,17,14),
    'B': (30,17,17,30,17,17,30), '7': (31,1,2,4,8,8,8),
    '5': (31,16,16,30,1,1,30), 'D': (30,17,17,17,17,17,30),
    'M': (17,27,21,21,17,17,17), 'A': (14,17,17,31,17,17,17)}


def picture(frame):
    rgb = bytearray(160*80*3)
    def put(x, y):
        if 0 < x < 159 and 0 < y < 79:
            offset = (y*160+x)*3
            rgb[offset:offset+3] = b'\xff\xff\xff'
    phase = frame % FRAME_COUNT
    line_x = 1 + min(phase, FRAME_COUNT-phase)
    for y in range(2, 78):
        put(line_x, y)
    # Two-pixel font straddles y=39/40, exercising both RGB halves.
    text_x = text_origin(frame)
    for char_index, char in enumerate('HUB75 DMA'):
        for row, bits in enumerate(GLYPHS.get(char, (0,)*7)):
            for col in range(5):
                if bits & (1 << (4-col)):
                    for dx in (0, 1):
                        for dy in (0, 1):
                            put(text_x+char_index*12+col*2+dx, 33+row*2+dy)
    return rgb


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--test-boot', action='store_true')
    parser.add_argument('--dp3364s-connected', action='store_true')
    parser.add_argument('--seconds', type=int, default=60, choices=range(1, 301))
    args = parser.parse_args()
    if args.run:
        if not args.test_boot or not args.dp3364s_connected:
            raise SystemExit('Require --test-boot --dp3364s-connected; never FM6363C')
        transport.guard_configuration(args)
    encoder = Encoder()
    def generated():
        for i in range(FRAME_COUNT):
            frame = encoder.encode(picture(i))
            if i % 32 == 31 or i == FRAME_COUNT-1:
                print('Prepared %d/%d motion frames' % (i+1, FRAME_COUNT), flush=True)
            yield frame
    root = Path(__file__).resolve().parent
    signature = hashlib.sha256(b''.join((root/name).read_bytes() for name in
        ('dp3364s_motion.py', 'dp3364s_blue.py', 'panel_frame.py'))).hexdigest()
    cache = root / '.motion_cache.zip'
    try:
        frames = CompressedFrames.load(cache, signature, FRAME_COUNT)
        print('Loaded verified motion cache: %d frames' % len(frames), flush=True)
    except (OSError, ValueError, KeyError):
        frames = CompressedFrames(generated())
        frames.save(cache, signature)
    print(json.dumps(dict(frames=len(frames), compressed_payload_bytes=frames.stored_bytes,
                          target_updates_per_second=TARGET_FPS,
                          line_step_pixels=1, hardware_verified=False)), flush=True)
    if not args.run:
        return
    transport.WIDTH, transport.HEIGHT = panel.WIDTH, panel.HEIGHT
    mode = transport.Mode(clock=16000, hdisplay=panel.WIDTH, hsync_start=panel.WIDTH+1,
                          hsync_end=panel.WIDTH+2, htotal=panel.WIDTH+3,
                          vdisplay=panel.HEIGHT, vsync_start=panel.HEIGHT+1,
                          vsync_end=panel.HEIGHT+2, vtotal=panel.HEIGHT+3,
                          flags=5, type=32, name=b'dp-motion')
    args.device, args.pattern, args.timing = '/dev/dri/card0', 'motion', 'confirmed-static-timing'
    def stop(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    transport.run(args, mode_override=mode, frame_pixels=frames[0],
                  before_enable=panel.initialise, after_disable=panel.blank_after_stop,
                  safety_check=transport.guard_configuration,
                  presenter=Presenter(frames, fps=TARGET_FPS))


if __name__ == '__main__':
    main()
