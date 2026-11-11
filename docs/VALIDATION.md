# Validation status

Original standalone hardware tests are complete. No new panel test is requested.

ESP32-S3: RGB/white/CMY, geometry, monotonic grayscale/RGB and DMA motion passed
visual checks. A faint background/top-row trace remained in that investigation.

Pi Zero 2 W + Triple Bonnet Port 1: colour fills, black, geometry and gradients
passed on 2026-11-06. Motion initially showed doubling; the saved 2026-11-07
checkpoint with a strict one-pixel text step passed the final visual check:
smooth readable text and line, no visible flicker or jumps.

The repository uses that saved standalone checkpoint. No new demo.py is included.
Only personal paths/comments and a guard for an unrelated application service
were removed. Protocol, timing and image generation are preserved; original
and published file hashes are listed in SOURCE_PROVENANCE.json.

Later final Pi full-PWM modules are separate reference material. Finishing all
64 x 40 ROW commands removed the observed parasitic rows in that later stage.
Its clean shutdown behaviour must not be attributed to the earlier standalone
motion scripts. Different saved stages are explicitly distinguished.

Visual acceptance does not constitute photometric or electrical measurement.
8 MHz is the requested active DCLK, not an independently measured frequency.
