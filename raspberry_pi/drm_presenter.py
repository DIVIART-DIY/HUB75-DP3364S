"""Two DRM buffers, page flips acknowledged at vblank, no active-buffer writes."""
import ctypes as C
import mmap
import os
import select
import struct
import time
import zlib
import json
import zipfile
import dpi_transport as drm


class CompressedFrames:
    """Keep a long diagnostic loop in RAM without hundreds of MB of payload."""
    def __init__(self, frames):
        self.packed = [zlib.compress(frame, 1) for frame in frames]

    def __len__(self):
        return len(self.packed)

    def __getitem__(self, index):
        return zlib.decompress(self.packed[index])

    @property
    def stored_bytes(self):
        return sum(map(len, self.packed))

    def save(self, path, signature):
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_STORED) as archive:
            archive.writestr('meta.json', json.dumps(dict(signature=signature, count=len(self))))
            for i, frame in enumerate(self.packed):
                archive.writestr('%04d.z' % i, frame)

    @classmethod
    def load(cls, path, signature, count):
        with zipfile.ZipFile(path) as archive:
            meta = json.loads(archive.read('meta.json'))
            if meta != dict(signature=signature, count=count):
                raise ValueError('Motion cache version mismatch')
            result = cls.__new__(cls)
            result.packed = [archive.read('%04d.z' % i) for i in range(count)]
        return result


def wait_flip(fd, timeout=2):
    deadline = time.monotonic()+timeout
    while True:
        remaining = deadline-time.monotonic()
        if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
            raise RuntimeError('DRM page flip acknowledgement timed out')
        packet = os.read(fd, 4096)
        if not packet:
            raise RuntimeError('DRM event fd closed')
        offset = 0
        while offset+8 <= len(packet):
            kind, length = struct.unpack_from('<II', packet, offset)
            if length < 8 or offset+length > len(packet):
                raise RuntimeError('Malformed DRM event')
            if kind == 2:
                if length < 32:
                    raise RuntimeError('Short page flip event')
                return struct.unpack_from('<QIIII', packet, offset+8)
            offset += length


class Presenter:
    def __init__(self, frames, fps=8):
        if not frames:
            raise ValueError('No frames')
        self.frames, self.fps = frames, fps
        self.lib = self.fd = self.mapping = None
        self.fb, self.dumb = drm.U32(), drm.CreateDumb()

    def prepare(self, lib, fd, crtc, initial_fb, initial_mapping, initial_pitch):
        self.lib, self.fd = lib, fd
        self.crtc = crtc
        page_flip = lib.drmModePageFlip
        page_flip.restype = C.c_int
        page_flip.argtypes = [C.c_int, drm.U32, drm.U32, drm.U32, C.c_void_p]
        self.dumb.width, self.dumb.height, self.dumb.bpp = drm.WIDTH, drm.HEIGHT, 32
        drm.ok(lib.drmIoctl(fd, 0xC02064B2, C.byref(self.dumb)), 'CREATE second buffer')
        mapped = drm.MapDumb(handle=self.dumb.handle)
        drm.ok(lib.drmIoctl(fd, 0xC01064B3, C.byref(mapped)), 'MAP second buffer')
        self.mapping = mmap.mmap(fd, self.dumb.size, offset=mapped.offset,
                                 flags=mmap.MAP_SHARED, prot=mmap.PROT_READ | mmap.PROT_WRITE)
        drm.ok(lib.drmModeAddFB(fd, drm.WIDTH, drm.HEIGHT, 24, 32, self.dumb.pitch,
                               self.dumb.handle, C.byref(self.fb)), 'Add second framebuffer')
        self.buffers = [(initial_fb, initial_mapping, initial_pitch),
                   (self.fb.value, self.mapping, self.dumb.pitch)]
        self.active, self.completed = 0, 0

    def present(self, frame):
        row_bytes = drm.WIDTH*4
        if len(frame) != row_bytes*drm.HEIGHT:
            raise ValueError('Incorrect frame size')
        other = 1-self.active
        framebuffer, memory, pitch = self.buffers[other]
        for y in range(drm.HEIGHT):
            memory[y*pitch:y*pitch+row_bytes] = frame[y*row_bytes:(y+1)*row_bytes]
        cookie = self.completed+1
        drm.ok(self.lib.drmModePageFlip(self.fd, self.crtc, framebuffer, 1,
                                       C.c_void_p(cookie)), 'Queue page flip')
        event = wait_flip(self.fd)
        if event[0] != cookie:
            raise RuntimeError('Unexpected page flip event cookie')
        self.active, self.completed = other, self.completed+1

    def run(self, lib, fd, crtc, initial_fb, initial_mapping, initial_pitch, seconds):
        self.prepare(lib, fd, crtc, initial_fb, initial_mapping, initial_pitch)
        completed = 0
        start, next_log = time.monotonic(), time.monotonic()+5
        while time.monotonic()-start < seconds:
            frame = self.frames[(completed+1) % len(self.frames)]
            due = start+(completed+1)/self.fps
            delay = due-time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self.present(frame)
            completed = self.completed
            if time.monotonic() >= next_log:
                print('Motion: %d flips acknowledged, %.2f updates/s' %
                      (completed, completed/(time.monotonic()-start)), flush=True)
                next_log = time.monotonic()+5
        return dict(flips_acknowledged=completed,
                    updates_per_second=completed/(time.monotonic()-start),
                    active_buffer_writes=0)

    def close(self):
        # Called by transport AFTER disabling scanout and BEFORE closing fd.
        if self.mapping is not None:
            self.mapping.close()
            self.mapping = None
        if self.fb.value:
            drm.ok(self.lib.drmModeRmFB(self.fd, self.fb), 'Remove second framebuffer')
            self.fb = drm.U32()
        if self.dumb.handle:
            handle = drm.U32(self.dumb.handle)
            drm.ok(self.lib.drmIoctl(self.fd, 0xC00464B4, C.byref(handle)), 'Destroy second buffer')
            self.dumb = drm.CreateDumb()
