"""Prepare the confirmed TEST SD clone only; never edits clock application files.

Run with sudo and --apply on the clone. Saves original boot files beside this
script before installing the isolated DPI overlay. Does not reboot or stop services.
"""
import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if not args.apply or os.geteuid() != 0:
        raise SystemExit('On TEST clone only: sudo python3 prepare_test_boot.py --apply')
    if b'Raspberry Pi Zero 2 W' not in Path('/proc/device-tree/model').read_bytes():
        raise SystemExit('Unexpected Pi model')
    here = Path(__file__).resolve().parent
    boot = Path('/boot/firmware')
    config, cmdline = boot / 'config.txt', boot / 'cmdline.txt'
    cfg = config.read_text()
    cmd = cmdline.read_text().strip()
    if 'dtoverlay=dp3364s-dpi-test' in cfg:
        raise SystemExit('Test overlay already configured; originals not overwritten')
    if len(cmd.splitlines()) != 1:
        raise SystemExit('cmdline.txt must contain exactly one line')
    if 'vc4-kms-v3d' not in cfg:
        raise SystemExit('Expected existing full KMS configuration')
    # First compile; do not touch boot files if compilation fails.
    with tempfile.TemporaryDirectory(prefix='dp-dpi-boot-') as tmp:
        overlay = Path(tmp) / 'test.dtbo'
        subprocess.run(['dtc', '-I', 'dts', '-O', 'dtb', '-o', str(overlay),
                        str(here / 'dp3364s-dpi-test-overlay.dts')], check=True)
        backup = here / ('boot_backup_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
        backup.mkdir()
        shutil.copy2(config, backup / 'config.txt')
        shutil.copy2(cmdline, backup / 'cmdline.txt')
        target = boot / 'overlays/dp3364s-dpi-test.dtbo'
        if target.exists():
            shutil.copy2(target, backup / target.name)
        shutil.copyfile(overlay, target)
        target.chmod(0o644)
        flags = ['systemd.mask=display-manager.service',
                 'drm_kms_helper.fbdev_emulation=0']
        for flag in flags:
            if flag not in cmd.split():
                cmd += ' ' + flag
        config.write_text(cfg.rstrip() + '\n\n[all]\n# Isolated DPI transport test on SD clone\ndtparam=i2c_arm=on\ndtoverlay=dp3364s-dpi-test\n')
        cmdline.write_text(cmd + '\n')
    print('Prepared TEST boot. Application files and running services unchanged.')
    print('Original boot files:', backup)
    print('Keep ALL Bonnet HUB75 cables disconnected, then reboot the test clone.')
    print('Rollback on clone: restore config.txt and cmdline.txt from that directory and reboot.')


if __name__ == '__main__':
    main()
