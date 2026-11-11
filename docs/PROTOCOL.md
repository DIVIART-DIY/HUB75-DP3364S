# Confirmed profile and findings

## Panel profile

160 columns, 80 rows, two RGB halves, 40 scan positions. DP3364S column drivers,
RUL5158E row-selection ICs and DP245D buffers were read from the tested board.
A/B/C use serial selection: A = shift clock, B = active-low enable,
C = row-token data. D/E are routed but not binary row address inputs here.
HUB75 OE is used as the DP3364S ROW command, not conventional brightness PWM.

Register words (address in high byte, data in low byte):

```
1100 0227 033F 043F 0504 0642 0700
08BF 0960 0ABE 0B8B 0C08 0D12
```

The tested initialization uses SDR (LAT 15 clocks), VSYNC (3), PRE_ACT (14),
WR_CFG (5), and repeated 16-bit configuration words across ten 16-column chips.
See `dp3364s_blue.initialise` for exact idle gaps and placement; command widths
alone are insufficient to reproduce the sequence. Initialization runs once,
not periodically during display.

ROW period: 128 DCLK clocks. First ROW pulse: 12 clocks; subsequent pulses:
4 clocks. MUX blank: 3 clocks. LAT data latch: 1 clock per 160 data clocks.
Pi image data uses 640 latches, column order `(i//16)*16 + latch%16`.

## Why complete PWM matters

Sending image data and immediately restarting VSYNC truncated the PWM scan.
The final Pi frame finishes all 64 x 40 = 2560 ROW pulses before clearing the
serial selector and starting the next VSYNC. This removed observed ghosts on
rows 0 and 39. Changing MUX blanking/relative phase alone did not fix them.
The exact internal electrical mechanism was not measured.

Pi full-cycle template: 514 scanlines, 644 DCLK slots each, represented by
1288 XRGB8888 DPI pixels per scanline. Horizontal/vertical porches are retained.
Requested pixel clock: 16 MHz; DCLK toggles every two active pixels, targeting
8 MHz during active scanout. Nominal complete refresh is about 23.97 Hz, NOT
8 MHz. Active clocks, PWM rate and application update rate are distinct.

## Other findings

- Serial A/B/C selection fixed missing alternating rows; binary A-E did not.
- Logical red and blue are physically swapped on this panel; both encoders
  compensate for this. Test another panel before assuming the same swap.
- PWM code 0x1000 produced brightness/colour wrap at the top setting. The final
  Pi profile caps at 0x0999. This is a tested operating choice, not an absolute
  datasheet rating or a guarantee of maximum brightness.
- DMA/DRM page-flip acknowledgement keeps the active buffer immutable. Motion
  advances after presentation, avoiding timing beats from unrelated timers.
- Before stop, scan black, hold row-enable inactive, disable scanout and park
  GPIOs. Repeated stop signals must not interrupt this cleanup.
- The published Pi encoder masks top y=0, bottom y=79 and left x=0. Right
  x=159 remains active. This is an explicit inherited viewport choice, not a
  requirement that all four edges must be disabled.
