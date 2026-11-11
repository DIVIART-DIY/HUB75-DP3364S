# Validation status

The original panel tests passed on ESP32-S3: solid RGB/white/CMY, geometry,
monotonic grayscale/RGB gradients, and DMA motion without visible tearing.
A faint red background/top-row trace remained in the ESP32 investigation.
Do not describe that implementation as having solved the Pi full-PWM issue.

The final Raspberry Pi Zero 2 W implementation passed colour, black, geometry,
gradient, motion and brightness tests. Full 64-group PWM scanning removed the
observed parasitic rows. Shutdown/reboot blanking was checked on that setup.
These were visual observations, not photometric or oscilloscope measurements.

This repository repackages those modules and adds a standalone `demo.py`.
Offline tests and an ESP32 build can check packaging. The new standalone runner
and clean-OS installation still require a fresh hardware acceptance run before
calling this exact package a hardware-verified release. Target DCLK is 8 MHz;
its electrical frequency has not been independently confirmed.

Acceptance sequence: blue, red, green, white, yellow, cyan, magenta, black;
geometry; gradients; motion; Ctrl+C; repeat start/stop; reboot. Check for dark
or repeated rows, colour swaps, ghosts, flicker, and tearing. Test black under
low ambient light. Record OS/kernel, board, panel markings and results.
