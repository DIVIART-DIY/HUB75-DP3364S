"""DPI transport experiment, NOT a DP3364S display driver.

Default: in-memory self-test only. --run requires an isolated test boot,
inactive HUB75 demo service and ALL Bonnet HUB75 cables disconnected.
No GPIO library, application imports, boot edits or service control.
"""
import argparse
import ctypes as C
import ctypes.util
import json
import mmap
import os
from pathlib import Path
import signal
import struct
import subprocess
import time

WIDTH, HEIGHT, PIXEL_HZ = 1920, 128, 16000000
PINS = dict(R1=11, G1=27, B1=7, R2=8, G2=9, B2=10,
            CLK=17, LAT=4, ROW=18, A=22, B=23, C=24, D=25, E=15)
DATA = ('R1', 'G1', 'B1', 'R2', 'G2', 'B2')


def bit(name):
    return 1 << (PINS[name] - 4)


def image():
    """XRGB8888 little endian. CLK low/high; one RGB wire high per 64 us.

    B=/enable remains high, ROW/LAT/A/C/D/E remain low. No initialization.
    This pattern is for scope/logic analyzer, NEVER a connected LED panel.
    """
    pixels = bytearray(WIDTH * HEIGHT * 4)
    for clock in range(WIDTH * HEIGHT // 2):
        word = bit('B') | bit(DATA[(clock // 512) % len(DATA)])
        struct.pack_into('<II', pixels, clock * 8, word, word | bit('CLK'))
    return pixels


def self_test():
    pixels = image()
    data_mask = sum(bit(n) for n in DATA)
    allowed = data_mask | bit('B') | bit('CLK')
    counts = {n: 0 for n in DATA}
    for i in range(0, len(pixels), 8):
        low, high = struct.unpack_from('<II', pixels, i)
        if low & bit('CLK') or high != low | bit('CLK'):
            raise RuntimeError('CLK encoding')
        if low & ~allowed or not low & bit('B'):
            raise RuntimeError('Control encoding')
        selected = [n for n in DATA if low & bit(n)]
        if len(selected) != 1:
            raise RuntimeError('RGB walking bit')
        counts[selected[0]] += 1
    if any(n == 0 for n in counts.values()):
        raise RuntimeError('Missing RGB wire')
    # Explicitly test byte order (D0 is blue bit0, D23 red bit7).
    if struct.pack('<I', 0x800001) != bytes((1, 0, 128, 0)):
        raise RuntimeError('XRGB byte order')
    return dict(logical_encoding_test='PASS', width=WIDTH, height=HEIGHT,
                bytes=len(pixels), requested_pixel_hz=PIXEL_HZ,
                requested_dclk_hz=PIXEL_HZ // 2,
                active_payload_rate_hz=PIXEL_HZ / (WIDTH * HEIGHT),
                hardware_validated=False,
                gpio_output_enabled=False)


# Public libdrm ABI: xf86drmMode.h. Pointer-owned objects are always freed.
U32, U64 = C.c_uint32, C.c_uint64


class Mode(C.Structure):
    _fields_ = [('clock', U32)] + [(n, C.c_uint16) for n in
                ('hdisplay', 'hsync_start', 'hsync_end', 'htotal', 'hskew',
                 'vdisplay', 'vsync_start', 'vsync_end', 'vtotal', 'vscan')] + [
                ('vrefresh', U32), ('flags', U32), ('type', U32), ('name', C.c_char * 32)]


class Resources(C.Structure):
    _fields_ = [('count_fbs', C.c_int), ('fbs', C.POINTER(U32)),
                ('count_crtcs', C.c_int), ('crtcs', C.POINTER(U32)),
                ('count_connectors', C.c_int), ('connectors', C.POINTER(U32)),
                ('count_encoders', C.c_int), ('encoders', C.POINTER(U32)),
                ('min_width', U32), ('max_width', U32), ('min_height', U32), ('max_height', U32)]


class Connector(C.Structure):
    _fields_ = [(n, U32) for n in ('connector_id', 'encoder_id', 'connector_type', 'connector_type_id')] + [
        ('connection', C.c_int), ('mmWidth', U32), ('mmHeight', U32), ('subpixel', C.c_int),
        ('count_modes', C.c_int), ('modes', C.POINTER(Mode)), ('count_props', C.c_int),
        ('props', C.POINTER(U32)), ('prop_values', C.POINTER(U64)),
        ('count_encoders', C.c_int), ('encoders', C.POINTER(U32))]


class Encoder(C.Structure):
    _fields_ = [(n, U32) for n in ('encoder_id', 'encoder_type', 'crtc_id', 'possible_crtcs', 'possible_clones')]


class Crtc(C.Structure):
    _fields_ = [(n, U32) for n in ('crtc_id', 'buffer_id', 'x', 'y', 'width', 'height')] + [
        ('mode_valid', C.c_int), ('mode', Mode), ('gamma_size', C.c_int)]


class CreateDumb(C.Structure):
    _fields_ = [(n, U32) for n in ('height', 'width', 'bpp', 'flags', 'handle', 'pitch')] + [('size', U64)]


class MapDumb(C.Structure):
    _fields_ = [('handle', U32), ('pad', U32), ('offset', U64)]


def library():
    lib = C.CDLL(ctypes.util.find_library('drm') or 'libdrm.so.2', use_errno=True)
    def api(name, result, *args):
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = result, args
    api('drmModeGetResources', C.POINTER(Resources), C.c_int)
    api('drmModeFreeResources', None, C.POINTER(Resources))
    for kind, cls in (('Connector', Connector), ('Encoder', Encoder), ('Crtc', Crtc)):
        api('drmModeGet' + kind, C.POINTER(cls), C.c_int, U32)
        api('drmModeFree' + kind, None, C.POINTER(cls))
    api('drmSetMaster', C.c_int, C.c_int)
    api('drmDropMaster', C.c_int, C.c_int)
    api('drmIoctl', C.c_int, C.c_int, C.c_ulong, C.c_void_p)
    api('drmModeAddFB', C.c_int, C.c_int, U32, U32, C.c_uint8, C.c_uint8, U32, U32, C.POINTER(U32))
    api('drmModeRmFB', C.c_int, C.c_int, U32)
    api('drmModeSetCrtc', C.c_int, C.c_int, U32, U32, U32, U32, C.POINTER(U32), C.c_int, C.POINTER(Mode))
    api('drmCrtcGetSequence', C.c_int, C.c_int, U32, C.POINTER(U64), C.POINTER(U64))
    return lib


def inspect_drm(device):
    """Read resources only; no DRM master or display configuration requests."""
    lib = library()
    fd = os.open(device, os.O_RDONLY | os.O_CLOEXEC)
    resources = None
    try:
        resources = lib.drmModeGetResources(fd)
        if not resources:
            raise RuntimeError('Cannot read DRM resources')
        return dict(mode_struct_bytes=C.sizeof(Mode),
                    create_dumb_bytes=C.sizeof(CreateDumb),
                    connectors=resources.contents.count_connectors,
                    crtcs=resources.contents.count_crtcs,
                    output_enabled=False)
    finally:
        if resources:
            lib.drmModeFreeResources(resources)
        os.close(fd)


def ok(result, action):
    if result != 0:
        err = C.get_errno()
        raise RuntimeError('%s: errno=%s %s' % (action, err, os.strerror(err)))


def timing_mode(timing):
    if timing == 'continuous':
        raise RuntimeError('Zero-blanking mode disabled: observed VC4 commit/flip timeouts on Zero 2 W')
    if timing != 'porches':
        raise ValueError('Unknown timing profile')
    hfp, hs, hbp, vfp, vs, vbp = 16, 32, 32, 3, 2, 3
    return Mode(clock=PIXEL_HZ // 1000, hdisplay=WIDTH, hsync_start=WIDTH+hfp,
                hsync_end=WIDTH+hfp+hs, htotal=WIDTH+hfp+hs+hbp,
                vdisplay=HEIGHT, vsync_start=HEIGHT+vfp,
                vsync_end=HEIGHT+vfp+vs, vtotal=HEIGHT+vfp+vs+vbp,
                flags=5, type=32, name=b'dp-transport')


def verify_frame_progress(lib, fd, crtc_id, mode, wait=time.sleep):
    """Measure DRM frame timestamps, never infer MHz DCLK from sampled GPIO."""
    captures = []
    for i in range(6):
        if i:
            wait(0.06)
        sequence, ns = U64(), U64()
        ok(lib.drmCrtcGetSequence(fd, crtc_id, C.byref(sequence), C.byref(ns)),
           'Read DRM frame sequence')
        captures.append((sequence.value, ns.value))
    for previous, current in zip(captures, captures[1:]):
        if current[0] < previous[0] or current[1] < previous[1]:
            raise RuntimeError('DRM frame sequence/timestamp went backwards')
    frames = captures[-1][0] - captures[0][0]
    elapsed_ns = captures[-1][1] - captures[0][1]
    if frames < 2 or elapsed_ns <= 0:
        raise RuntimeError('KMS accepted mode but frame progress NOT confirmed')
    measured = frames * 1e9 / elapsed_ns
    expected = mode.clock * 1000 / (mode.htotal * mode.vtotal)
    if abs(measured / expected - 1) > 0.10:
        raise RuntimeError('DRM frame rate %.3f Hz differs from requested %.3f Hz' % (measured, expected))
    return dict(drm_frame_progress=True, drm_frame_hz=measured,
                requested_frame_hz=expected, captures=captures,
                connector_clock_measured=False, bonnet_output_verified=False)


def save_run_report(report):
    target = Path(os.environ.get('DP3364S_RUN_REPORT',
                                 '/tmp/hub75-dp3364s-dpi_run_latest.json'))
    target.write_text(json.dumps(report, indent=2))
    target.chmod(0o644)
    print('Run report saved: ' + str(target), flush=True)


def guard(args):
    if not args.hub75_disconnected or not args.test_boot:
        raise RuntimeError('Require --test-boot --hub75-disconnected on separate test boot')
    guard_configuration(args)


def guard_configuration(args):
    if not args.test_boot:
        raise RuntimeError('Require isolated test boot')
    model = Path('/proc/device-tree/model').read_bytes()
    if b'Raspberry Pi Zero 2 W' not in model:
        raise RuntimeError('This experiment is restricted to Raspberry Pi Zero 2 W')
    # Dedicated test OS only: no competing display manager or matrix process.
    state = subprocess.run(['systemctl','is-active','display-manager.service'],
                           capture_output=True,text=True).stdout.strip()
    if state in ('active','activating'):
        raise RuntimeError('Stop the display manager before running the demo')
    # Verify the booted overlay and all its claimed pins, not just a CLI promise.
    root = Path('/proc/device-tree')
    actual = (root / 'soc/gpio@7e200000/dp-test-pins/brcm,pins').read_bytes()
    pins = set(struct.unpack('>' + 'I' * (len(actual) // 4), actual))
    boot_blank = root/'soc/gpio@7e200000/dp-blank-boot'
    if boot_blank.is_dir():
        if ((boot_blank/'pins').read_bytes() != b'gpio23\0' or
                (boot_blank/'function').read_bytes() != b'gpio_out\0' or
                not (boot_blank/'output-high').exists()):
            raise RuntimeError('Unexpected boot blanking pin configuration')
        pins.add(PINS['B'])
    if pins != set(PINS.values()):
        raise RuntimeError('Unexpected overlay pin set; sensors must remain excluded')
    for name, gpio in PINS.items():
        if gpio in (2, 3, 26):
            raise RuntimeError('Sensor conflict: ' + name)
    functions = subprocess.run(['pinctrl', 'get', '0-27'], capture_output=True,
                               text=True, check=True, timeout=5).stdout
    import re
    modes = dict((int(gpio), mode) for gpio, mode in
                 re.findall(r'^\s*(\d+):\s+(\w+)', functions, re.M))
    b_is_held = bool(re.search(r'^\s*23:\s+op\s+.*\|\s+hi\b', functions, re.M))
    parked = b_is_held and all(
        re.search(r'^\s*%d:\s+op\s+.*\|\s+lo\b' % gpio, functions, re.M)
        for gpio in set(PINS.values())-{PINS['B']})
    if not parked and any(modes.get(gpio) != 'a2' and not (gpio == PINS['B'] and b_is_held)
           for gpio in PINS.values()):
        raise RuntimeError('Port 1 pins must already be ALT2 from test overlay')
    if modes.get(2) != 'a0' or modes.get(3) != 'a0' or modes.get(26) == 'a2':
        raise RuntimeError('Sensor pin functions not preserved')


def run(args, *, mode_override=None, frame_pixels=None, before_enable=None,
        after_disable=None, safety_check=None, presenter=None,
        after_enable=None, before_disable=None):
    # Reject the known failing profile BEFORE opening DRM or touching outputs.
    mode = mode_override if mode_override is not None else timing_mode(args.timing)
    (safety_check or guard)(args)
    if C.sizeof(Mode) != 68 or C.sizeof(CreateDumb) != 32 or C.sizeof(MapDumb) != 16:
        raise RuntimeError('DRM ABI layout mismatch')
    lib = library()
    fd = os.open(args.device, os.O_RDWR | os.O_CLOEXEC)
    resources = old = None
    connectors, fb, dumb = [], U32(), CreateDumb()
    mapping = None
    master = changed = False
    report = dict(timing=args.timing, pattern=args.pattern,
                  drm_frame_progress=False, connector_clock_measured=False,
                  bonnet_output_verified=False, outcome='not_started')
    try:
        resources = lib.drmModeGetResources(fd)
        if not resources:
            raise RuntimeError('Cannot read DRM resources')
        chosen = None
        for i in range(resources.contents.count_connectors):
            p = lib.drmModeGetConnector(fd, resources.contents.connectors[i])
            if not p:
                continue
            connectors.append(p)
            if p.contents.connector_type == 17 and p.contents.connection == 1:  # DPI
                chosen = p.contents
        if chosen is None:
            raise RuntimeError('No connected DPI connector; nothing enabled')
        possible = 0
        for i in range(chosen.count_encoders):
            e = lib.drmModeGetEncoder(fd, chosen.encoders[i])
            if e:
                possible |= e.contents.possible_crtcs
                lib.drmModeFreeEncoder(e)
        crtc_id = next((resources.contents.crtcs[i] for i in range(resources.contents.count_crtcs)
                        if possible & (1 << i)), None)
        if crtc_id is None:
            raise RuntimeError('No DPI CRTC')
        old = lib.drmModeGetCrtc(fd, crtc_id)
        if not old:
            raise RuntimeError('Cannot save CRTC')
        # Do not take over any existing display; only a dedicated test scanout.
        if old.contents.mode_valid or old.contents.buffer_id:
            raise RuntimeError('CRTC already active; refusing takeover')
        ok(lib.drmSetMaster(fd), 'DRM master')
        master = True
        dumb.width, dumb.height, dumb.bpp = WIDTH, HEIGHT, 32
        ok(lib.drmIoctl(fd, 0xC02064B2, C.byref(dumb)), 'CREATE_DUMB')
        mapped = MapDumb(handle=dumb.handle)
        ok(lib.drmIoctl(fd, 0xC01064B3, C.byref(mapped)), 'MAP_DUMB')
        mapping = mmap.mmap(fd, dumb.size, offset=mapped.offset,
                            flags=mmap.MAP_SHARED, prot=mmap.PROT_READ | mmap.PROT_WRITE)
        pixels = (frame_pixels if frame_pixels is not None else
                  image() if args.pattern in ('walking', 'fastcheck') else bytes(WIDTH * HEIGHT * 4))
        if len(pixels) != WIDTH * HEIGHT * 4:
            raise RuntimeError('Framebuffer dimensions mismatch')
        for y in range(HEIGHT):
            mapping[y * dumb.pitch:y * dumb.pitch + WIDTH * 4] = pixels[y * WIDTH * 4:(y + 1) * WIDTH * 4]
        ok(lib.drmModeAddFB(fd, WIDTH, HEIGHT, 24, 32, dumb.pitch, dumb.handle, C.byref(fb)), 'AddFB')
        # Zero-blanking mode produced kernel commit/flip timeouts on Zero 2 W.
        # First establish a real output with conventional sync intervals.
        # Porches interrupt CLK: this is NOT a usable DP3364S waveform yet.
        connector = U32(chosen.connector_id)
        if before_enable is not None:
            before_enable()
        started = time.monotonic()
        ok(lib.drmModeSetCrtc(fd, crtc_id, fb, 0, 0, C.byref(connector), 1, C.byref(mode)), 'SetCrtc')
        changed = True
        elapsed = time.monotonic() - started
        report['commit_seconds'] = elapsed
        print('KMS accepted: timing=%s, commit=%.3f s, requested active CLK=8 MHz, frame=%.3f Hz.' %
              (args.timing, elapsed, PIXEL_HZ / (mode.htotal * mode.vtotal)), flush=True)
        if elapsed > 5:
            raise RuntimeError('KMS commit too slow; inspect kernel log. Physical output NOT confirmed.')
        report.update(verify_frame_progress(lib, fd, crtc_id, mode))
        print('DRM frames advancing: %.3f Hz; connector output remains unverified.' %
              report['drm_frame_hz'], flush=True)
        if after_enable is not None:
            after_enable()
        save_run_report(report)
        print('Physical output NOT confirmed. Measure CLK/B and check kernel log.', flush=True)
        if presenter is not None:
            report['presentation'] = presenter.run(lib, fd, crtc_id, fb.value,
                                                   mapping, dumb.pitch, args.seconds)
        elif args.pattern == 'fastcheck':
            # Read-only pad sampling, deliberately no frequency claim: Python
            # sampling is asynchronous and much slower than the desired DCLK.
            time.sleep(0.2)
            report.update(dict(timing=args.timing, commit_seconds=elapsed,
                               pixel_readback=bytes(mapping[:32]).hex(),
                               requested_active_dclk_hz=PIXEL_HZ//2,
                               frequency_measured=False, gpio_captures=[]))
            gpio_fd = os.open('/dev/gpiomem', os.O_RDONLY | os.O_CLOEXEC)
            try:
                with mmap.mmap(gpio_fd, 4096, flags=mmap.MAP_SHARED,
                               prot=mmap.PROT_READ) as gpio:
                    for capture in range(5):
                        highs = {17: 0, 23: 0}
                        changes = {17: 0, 23: 0}
                        previous = None
                        start = time.monotonic()
                        for _ in range(100000):
                            value = struct.unpack_from('<I', gpio, 0x34)[0]
                            for pin in (17, 23):
                                highs[pin] += bool(value & (1 << pin))
                                if previous is not None:
                                    changes[pin] += bool((value ^ previous) & (1 << pin))
                            previous = value
                        item = dict(samples=100000, seconds=time.monotonic()-start,
                                    high_samples=highs, observed_changes=changes)
                        report['gpio_captures'].append(item)
                        print(json.dumps(item), flush=True)
            finally:
                os.close(gpio_fd)
            state = subprocess.run(['pinctrl', 'get', '0-27'], capture_output=True,
                                   text=True, check=True, timeout=5)
            report['pin_functions'] = state.stdout
            target = Path('/tmp/hub75-dp3364s-dpi_fastcheck.json')
            target.write_text(json.dumps(report, indent=2))
            target.chmod(0o644)
            print('Report saved: ' + str(target), flush=True)
        elif args.pattern == 'diagnose':
            # Capture actual pin levels and driver state; no manual bit tracking.
            import glob
            report.update(dict(timing=args.timing, pitch=dumb.pitch,
                               buffer_size=dumb.size, samples=[], driver={}))
            patterns = [0, 0xffffff, 0, 0xff0000, 0x00ff00, 0x0000ff]
            patterns += [1 << i for i in range(24)] + [0xffffff, 0]
            for value in patterns:
                row = struct.pack('<I', value) * WIDTH
                for y in range(HEIGHT):
                    mapping[y*dumb.pitch:y*dumb.pitch+WIDTH*4] = row
                time.sleep(0.08)
                levels = []
                for _ in range(3):
                    p = subprocess.run(['pinctrl', 'get', '0-27'], capture_output=True,
                                       text=True, check=True, timeout=5)
                    levels.append(p.stdout)
                report['samples'].append(dict(word=hex(value), readback=bytes(mapping[:4]).hex(),
                                               gpio=levels))
                print('Captured buffer word 0x%06x' % value, flush=True)
            for pattern in ('/sys/kernel/debug/dri/*/state',
                            '/sys/kernel/debug/dri/*/dpi_regs',
                            '/sys/kernel/debug/dri/*/hvs_regs',
                            '/sys/kernel/debug/dri/*/crtc*/crc/control'):
                for path in glob.glob(pattern):
                    try:
                        report['driver'][path] = Path(path).read_text()[:32000]
                    except OSError as exc:
                        report['driver'][path] = str(exc)
            target = Path('/tmp/hub75-dp3364s-dpi_diagnostic.json')
            target.write_text(json.dumps(report, indent=2))
            target.chmod(0o644)
            print('Report saved: ' + str(target), flush=True)
        elif args.pattern == 'bitwalk':
            # Identify actual framebuffer bit -> GPIO mapping at one fixed probe.
            # Each bit stays high for 3 seconds, then all bits low for 1 second.
            deadline = time.monotonic() + args.seconds
            index = 0
            while time.monotonic() < deadline:
                for value, delay in ((1 << index, 3), (0, 1)):
                    row = struct.pack('<I', value) * WIDTH
                    for y in range(HEIGHT):
                        mapping[y*dumb.pitch:y*dumb.pitch+WIDTH*4] = row
                    print(('BIT %d HIGH; assumed GPIO%d' % (index, index+4))
                          if value else 'ALL LOW', flush=True)
                    time.sleep(min(delay, max(0, deadline-time.monotonic())))
                index = (index + 1) % 24
        elif args.pattern in ('slow', 'clockslow'):
            # Diagnostic only: intentionally modify the scanned buffer at 1 Hz.
            # All 24 DPI data bits change, but pinctrl routes only Port 1 pins.
            # This is NOT the future immutable DP3364S frame update mechanism.
            deadline = time.monotonic() + args.seconds
            high = True
            while time.monotonic() < deadline:
                value = ((bit('CLK') if args.pattern == 'clockslow' else 0xffffff)
                         if high else 0)
                row = struct.pack('<I', value) * WIDTH
                for y in range(HEIGHT):
                    mapping[y*dumb.pitch:y*dumb.pitch+WIDTH*4] = row
                print('SLOW: all routed DPI bits ' + ('HIGH' if high else 'LOW'), flush=True)
                if args.pattern == 'clockslow':
                    time.sleep(0.1)
                    p = subprocess.run(['pinctrl', 'get', '17'], capture_output=True,
                                       text=True, check=True, timeout=5)
                    print('CLOCK ONLY word=0x%06x: %s' % (value, p.stdout.strip()), flush=True)
                high = not high
                time.sleep(3 if args.pattern == 'clockslow' else 1)
        else:
            time.sleep(args.seconds)
        # Completed diagnostics does not mean a working DP3364S transport.
        report['outcome'] = 'diagnostic_completed'
    except BaseException as exc:
        report['outcome'] = 'failed'
        report['error'] = str(exc) or type(exc).__name__
        raise
    finally:
        cleanup_errors = []
        def cleanup(action, *values):
            try:
                action(*values)
            except Exception as exc:
                cleanup_errors.append(str(exc))
        if changed:
            if before_disable is not None:
                cleanup(before_disable)
            # It was inactive before us. Disable scanout before freeing its buffer.
            cleanup(lambda: ok(lib.drmModeSetCrtc(fd, crtc_id, 0, 0, 0, None, 0, None),
                               'Disable test CRTC'))
        if presenter is not None:
            cleanup(presenter.close)
        if mapping is not None:
            cleanup(mapping.close)
        if fb.value:
            cleanup(lambda: ok(lib.drmModeRmFB(fd, fb), 'Remove framebuffer'))
        if dumb.handle:
            handle = U32(dumb.handle)
            cleanup(lambda: ok(lib.drmIoctl(fd, 0xC00464B4, C.byref(handle)), 'Destroy dumb buffer'))
        if old:
            cleanup(lib.drmModeFreeCrtc, old)
        for p in connectors:
            cleanup(lib.drmModeFreeConnector, p)
        if resources:
            cleanup(lib.drmModeFreeResources, resources)
        if master:
            cleanup(lambda: ok(lib.drmDropMaster(fd), 'Drop DRM master'))
        cleanup(os.close, fd)
        if after_disable is not None:
            cleanup(after_disable)
        if cleanup_errors:
            report['cleanup_errors'] = cleanup_errors
            report['outcome'] = 'failed'
        cleanup(save_run_report, report)
        if cleanup_errors:
            raise RuntimeError('Test cleanup/report failed: ' + '; '.join(cleanup_errors))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--timing', choices=('porches', 'continuous'), default='porches',
                        help='porches: transport diagnosis only; continuous is rejected after observed timeouts')
    parser.add_argument('--pattern', choices=('walking', 'fastcheck', 'slow', 'clockslow', 'bitwalk', 'diagnose'), default='walking',
                        help='slow: alternate all routed DPI bits once per second, no panel attached')
    parser.add_argument('--inspect', action='store_true', help='read-only libdrm ABI/resource check')
    parser.add_argument('--test-boot', action='store_true')
    parser.add_argument('--hub75-disconnected', action='store_true')
    parser.add_argument('--device', default='/dev/dri/card0')
    parser.add_argument('--seconds', type=int, default=15, choices=range(1, 301))
    args = parser.parse_args()
    print(json.dumps(self_test(), indent=2))
    if args.inspect:
        print(json.dumps(inspect_drm(args.device), indent=2))
    if args.run:
        def stop(_signum, _frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, stop)
        run(args)


if __name__ == '__main__':
    main()
