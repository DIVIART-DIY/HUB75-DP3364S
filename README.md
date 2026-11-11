# HUB75 P2 160 x 80 with DP3364S

A practical bring-up guide and test code for one **P2 160 x 80 HUB75 panel**
with **DP3364S** column drivers and **RUL5158E** serial row selection.
Two host implementations are included: ESP32-S3 LCD_CAM/GDMA and Raspberry Pi
Zero 2 W hardware DPI through the Adafruit Triple LED Matrix Bonnet, Port 1.

This is a panel-specific experimental driver, not a universal HUB75 library.
HUB75 describes the connector; it does not identify the panel protocol.

## Status

The ESP32-S3 and Raspberry Pi standalone colour, geometry, gradient and motion
tests were visually checked on the original panel. The Pi standalone files
come from the saved successful 2026-11-07 motion checkpoint, not a new demo.
Timing, pixel encoding and motion generation in that checkpoint are preserved.
Personal paths/comments and an unrelated application-service guard were removed.
Source hashes are recorded in [provenance](docs/SOURCE_PROVENANCE.json).

The later full-PWM Pi primitives are kept separately in
`raspberry_pi/full_pwm_reference`: these document the solution to parasitic
rows found later. They are not silently substituted into the standalone motion
test. See [validation](docs/VALIDATION.md) and [protocol](docs/PROTOCOL.md).

## Tested hardware

| Part | Tested configuration |
| --- | --- |
| Panel | P2 RGB, 160 x 80 pixels, approximately 320 x 160 mm, 1/40 scan |
| Column driver | DP3364S |
| Row selector | RUL5158E, A/B/C serial row selection |
| Input buffers | DP245D |
| ESP32 | ESP32-S3, PlatformIO `esp32-s3-devkitc-1` target |
| Raspberry Pi | Zero 2 W Rev 1.0, aarch64 |
| Pi adapter | Adafruit Triple LED Matrix Bonnet, **Port 1** |
| Recorded Pi software | Linux 6.18.50+rpt-rpi-v8; Python 3.13.5; full KMS |
| Power | Separate regulated 5 V panel supply; shared signal ground |

Power-supply current was not characterized. Do not power the panel from the
Pi GPIO header or ESP32 board. Connect/disconnect HUB75 with power removed.
The Bonnet buffers Pi outputs; an ESP32 direct-wiring setup must meet the
actual panel's input-level requirements. No universal 3.3 V compatibility is claimed.

## Connector and wiring

View into the panel **input header**, pin 1 identified by the board marker.
Confirm physical orientation; a cable plug viewed from its rear is mirrored.
Pins are numbered **across each pair**, not down one column:

```
 1 R1     2 G1
 3 B1     4 GND
 5 R2     6 G2
 7 B2     8 E
 9 A     10 B
11 C     12 D
13 CLK   14 LAT
15 OE    16 GND
```

| HUB75 pin | Signal | ESP32-S3 GPIO | Bonnet Port 1 BCM GPIO |
| --- | --- | --- | --- |
| 1 | R1 | 4 | 11 |
| 2 | G1 | 5 | 27 |
| 3 | B1 | 6 | 7 |
| 4 | GND | GND | GND |
| 5 | R2 | 7 | 8 |
| 6 | G2 | 8 | 9 |
| 7 | B2 | 9 | 10 |
| 8 | E | 14 | 15 |
| 9 | A, selector clock | 10 | 22 |
| 10 | B, selector /enable | 11 | 23 |
| 11 | C, selector data | 12 | 24 |
| 12 | D | 13 | 25 |
| 13 | CLK | 17 | 17 |
| 14 | LAT | 15 | 4 |
| 15 | OE / DP3364S ROW | 16 | 18 |
| 16 | GND | GND | GND |

BCM GPIO numbers are **not** physical Raspberry Pi header pin numbers.
BCM17 is physical header pin 11, and feeds HUB75 pin 13 through the Bonnet.
Check the adapter E-routing setting: the tested panel uses E on HUB75 pin 8.
The custom Pi overlay leaves GPIO2/3 for I2C and GPIO26 outside DPI ownership.
This is **Port 1 only**; do not attach panels to the other ports with this overlay.

Logical R/B are swapped by the tested panel relative to connector labels.
The source compensates for this; do not swap the table just to compensate twice.

## ESP32-S3 quick start

Open `esp32s3` in PlatformIO. Connect the panel as listed above and use:

```
cd esp32s3
pio run
pio run -t upload
pio device monitor -b 115200
```

Select one continuous test with `ACTIVE_TEST` in `src/main.cpp`, then rebuild:

```
FULL_BLUE, FULL_RED, FULL_GREEN, FULL_WHITE,
FULL_YELLOW, FULL_CYAN, FULL_MAGENTA, FULL_BLACK,
GEOMETRY_MAP, GRADIENT_MAP, MOTION_DEMO, COLOUR_MOTION_DEMO
```

Default: `COLOUR_MOTION_DEMO`, LEVEL=0x0400, requested DCLK=8 MHz,
MUX blank=3 clocks, serial A/B/C selection. Do not use the diagnostic ROW_MAP
with the DMA transport; the source deliberately rejects that combination.
Two colour rings share immutable packets to fit internal DMA memory. Arbitrary
images can exceed that capacity; the driver retains its last frame on allocation
failure. See [protocol and findings](docs/PROTOCOL.md) for limitations.

## Raspberry Pi quick start

Use a **dedicated test microSD** with Raspberry Pi OS and full KMS
(`dtoverlay=vc4-kms-v3d` already configured). Keep HUB75 disconnected while
preparing boot. No application or login name is hard-coded in the instructions.

Copy this repository to the Pi, then in its directory:

```
sudo apt update
sudo apt install device-tree-compiler libdrm2
cd raspberry_pi
python3 -B dp3364s_blue.py
```

The last command checks encoding in memory without enabling hardware.
Prepare the custom overlay on a dedicated test OS (HUB75 disconnected):

```
sudo python3 -B prepare_test_boot.py --apply
sudo reboot
```

The script backs up config.txt and cmdline.txt, enables I2C, masks the display
manager and disables DRM fbdev emulation. It does not stop other matrix drivers.
After reboot, shut down, remove power, connect **Port 1 to panel IN**, then power
up with separate panel power and common ground. From `raspberry_pi`:

```
sudo python3 -B dp3364s_blue.py --run --test-boot --dp3364s-connected --colour blue
```

Repeat for `red`, `green`, `white`, `yellow`, `cyan`, `magenta`, then `black`.

```
sudo python3 -B dp3364s_blue.py --run --test-boot --dp3364s-connected --pattern geometry
sudo python3 -B dp3364s_blue.py --run --test-boot --dp3364s-connected --pattern gradients
sudo python3 -B dp3364s_motion.py --run --test-boot --dp3364s-connected --seconds 60
```

Motion prepares and caches 314 frames. The confirmed snapshot uses LEVEL=0x0400,
DPI 1288 x 182 at a requested 16 MHz pixel clock, about 66.99 Hz scanout and
33.50 acknowledged motion updates/s. The line/text step is one pixel per frame.
All four outer edges are dark in this standalone checkpoint.

The later full-PWM reference uses DPI 1288 x 514, about 23.97 Hz and a native
maximum code of 0x0999. Its encoder leaves the right edge available. These are
different saved stages; do not mix their geometry, encoders or overlays blindly.
There is no new standalone runner requiring the original tester to reopen a
working device. The original hardware tests are complete.

Ctrl+C stops the standalone test. These early test scripts do not include the
later production black-frame shutdown sequence. For an interrupted CRTC, reboot
the dedicated test OS instead of forcing takeover. Restore boot configuration
by restoring both backed-up boot files, then reboot with HUB75 disconnected.

## What to look for

| Test | Expected result |
| --- | --- |
| RGB / white / CMY | Complete uniform fill in the usable viewport |
| Black | No persistent background, points, bars or flashes in low ambient light |
| Geometry | Thin white frame; red/green top corners, blue/white bottom corners; centered cyan cross |
| Gradients | Four bands: gray, red, green, blue; 16 increasing levels without reversals |
| Motion | White line and `HUB75 DMA` text; no tearing at the half-panel boundary |
| Stop/restart | Early scripts stop output; later reference documents black-scan cleanup |

## Troubleshooting

| Symptom | Finding on this panel |
| --- | --- |
| Every other row dark | Use RUL5158E serial A/B/C selection, not binary A-E |
| Dark seam or repeated geometry | Verify 40 scan positions and column/latch ordering |
| Ghost rows 0/39 on Pi | Finish all 64 x 40 ROW commands before the next VSYNC |
| Brightness/colour wraps at the top | 0x1000 was unsuitable; tested Pi cap is 0x0999 |
| Red appears blue | Check physical R/B swap and avoid double compensation |
| Motion beats or double images | Advance on acknowledged flips; do not derive position from a separate timer |
| Only 50 Hz noise on a scope | This does not confirm DCLK; verify the actual pin and short probe ground |

Requested active DCLK is **8 MHz**; standalone scanout is about **66.99 Hz**,
while the later full-cycle reference refresh is about **23.97 Hz**. They are different quantities. Electrical CLK frequency was not
independently measured. See [protocol details](docs/PROTOCOL.md).

## Source credits and release preparation

ESP32 LCD_CAM/GDMA setup was adapted from
[kkwestt/RaspberryPi-HUB75-DP3364S-Matrix](https://github.com/kkwestt/RaspberryPi-HUB75-DP3364S-Matrix).
Its MIT notice is retained in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
That project's panel and host are different; its settings must not be assumed
interchangeable with this 160 x 80 / RUL5158E profile.

Original contributions are licensed under the [MIT License](LICENSE).
Copyright (c) 2026 DIVIART. Upstream copyright and license notices are retained
in THIRD_PARTY_NOTICES.md. The saved hardware test results are documented in VALIDATION.md. No private configuration, keys or backup images belong here.
