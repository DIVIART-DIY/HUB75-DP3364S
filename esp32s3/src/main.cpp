#include <Arduino.h>
#include "soc/gpio_struct.h"
#include "esp_heap_caps.h"
#include "lcd_dma.h"
#include "motion_pattern.h"

// DP3364S 160x80 / 1:40: confirmed SHIFTREG_ABC, LCD_CAM/GDMA transport test.
// Unlike standard HUB75, DP3364S needs VSYNC, PRE_ACT and WR_CFG commands.
// HUB75 OE is its ROW pulse input. USE_LCD_DMA=false restores CPU bit-bang.
// Protocol: DP3364S datasheet and MIT-licensed reference by kkwestt.

namespace {
constexpr uint8_t PIN_R1 = 4, PIN_G1 = 5, PIN_B1 = 6;
constexpr uint8_t PIN_R2 = 7, PIN_G2 = 8, PIN_B2 = 9;
constexpr uint8_t PIN_A = 10, PIN_B = 11, PIN_C = 12, PIN_D = 13, PIN_E = 14;
constexpr uint8_t PIN_LAT = 15, PIN_ROW = 16, PIN_CLK = 17;

constexpr uint32_t pinMask(uint8_t pin) { return 1UL << pin; }
constexpr uint32_t M_R1 = pinMask(PIN_R1), M_G1 = pinMask(PIN_G1), M_B1 = pinMask(PIN_B1);
constexpr uint32_t M_R2 = pinMask(PIN_R2), M_G2 = pinMask(PIN_G2), M_B2 = pinMask(PIN_B2);
constexpr uint32_t M_A = pinMask(PIN_A), M_B = pinMask(PIN_B), M_C = pinMask(PIN_C);
// Do not call the E mask M_E: math headers reserve M_E for Euler's number.
constexpr uint32_t M_ADDR_D = pinMask(PIN_D), M_ADDR_E = pinMask(PIN_E);
constexpr uint32_t M_LAT = pinMask(PIN_LAT);
constexpr uint32_t M_ROW = pinMask(PIN_ROW), M_CLK = pinMask(PIN_CLK);
constexpr uint32_t M_BUS = M_R1 | M_G1 | M_B1 | M_R2 | M_G2 | M_B2 |
                           M_A | M_B | M_C | M_ADDR_D | M_ADDR_E | M_LAT | M_ROW | M_CLK;
constexpr uint32_t M_DATA_ALL = M_R1 | M_G1 | M_B1 | M_R2 | M_G2 | M_B2;

constexpr int PANEL_WIDTH = 160;
constexpr int SCAN_ROWS = 40;
constexpr int CHIPS = PANEL_WIDTH / 16;
constexpr int BLOCK_CLOCKS = CHIPS * 16;
// Experiment 2026-11-02: ROW timing is independent of the data-chain length.
// The working 1:32 reference uses 128 clocks even with a 256-clock data block.
// Set back to BLOCK_CLOCKS (160) to reproduce the previous probe.
constexpr int ROW_PERIOD_CLOCKS = 128;
// Restore the clearer motion timing: 8 clocks did not remove the top ghost.
constexpr int MUX_BLANK_CLOCKS = 3;
constexpr int LATCHES_PER_FRAME = SCAN_ROWS * 16;
constexpr int TAIL_ROWS = SCAN_ROWS * 2;

// SHIFTREG_ABC produced full blue on this RUL5158E panel (2026-11-02).
// false restores the previous binary A-E probe without other changes.
constexpr bool USE_SHIFTREG_ABC = true;
// false restores the known full-blue CPU probe. DMA is an on-panel test.
constexpr bool USE_LCD_DMA = true;
constexpr uint32_t DMA_CLOCK_HZ = 8000000;
bool dma_active = false;
bool output_failed = false;
uint32_t mux_state = 0;  // A clock low, B /enable low, C serial data.

// Reference configuration with experimental REG02=0x27; full blue confirmed.
constexpr uint16_t REGISTERS[] = {
    0x1100, 0x0227, 0x033F, 0x043F, 0x0504, 0x0642, 0x0700,
    0x08BF, 0x0960, 0x0ABE, 0x0B8B, 0x0C08, 0x0D12,
};

struct Color { uint16_t r, g, b; const char *name; };
constexpr uint16_t LEVEL = 0x0400;  // Keep the same PWM code for each colour comparison.

uint32_t addressState(int row) {
  if (USE_SHIFTREG_ABC) return mux_state;
  // DP3364S sees all forty rows through ROW pulses. A-E expose five physical
  // address bits; the row decoder behaviour for rows 32..39 is what we probe.
  const uint8_t a = static_cast<uint8_t>(row & 0x1F);
  return ((a & 1) ? M_A : 0) | ((a & 2) ? M_B : 0) |
         ((a & 4) ? M_C : 0) | ((a & 8) ? M_ADDR_D : 0) |
         ((a & 16) ? M_ADDR_E : 0);
}

// Change row-control pins without introducing a DP3364S DCLK or LAT command.
inline void muxPins(uint32_t state) {
  constexpr uint32_t mask = M_A | M_B | M_C | M_ADDR_D | M_ADDR_E;
  GPIO.out_w1tc = mask & ~state;
  GPIO.out_w1ts = mask & state;
}

void shiftMuxBit(bool selected) {
  // B high blanks the row switches while A advances the serial register.
  const uint32_t state = M_B | (selected ? M_C : 0);
  muxPins(state);
  muxPins(state | M_A);
  muxPins(state);
  mux_state = state;
}

void resetMux() {
  if (!USE_SHIFTREG_ABC) return;
  // A new frame restarts the logical scan at row 0. Discard any old token
  // before inserting its replacement, including after a partial scan wrap.
  for (int i = 0; i < SCAN_ROWS; ++i) shiftMuxBit(false);
}

void selectMuxRow(int row) {
  if (!USE_SHIFTREG_ABC) return;
  shiftMuxBit(row == 0);  // one selected bit, then 39 zero bits per sweep.
  mux_state &= ~M_B;
  muxPins(mux_state);
}

inline void clockWord(uint32_t state) {
  GPIO.out_w1ts = state & M_BUS;
  GPIO.out_w1tc = (~state) & M_BUS;
  GPIO.out_w1ts = M_CLK;
  GPIO.out_w1tc = M_CLK;
}

void clocks(uint32_t state, int count) {
  while (count-- > 0) clockWord(state);
}

void sendVsync(uint32_t addr) {
  clocks(addr | M_LAT, 3);
  clocks(addr, 17);
}

void enterSingleEdgeMode() {
  // DP3364S power-up mode is not guaranteed. SDR = LE/LAT high for exactly
  // 15 DCLKs, which makes it sample data only on CLK rising edges.
  // Without this, a double-edge receiver consumes every bit twice.
  clocks(M_LAT, 15);
  clockWord(0);
}

void sendRegister(uint16_t reg) {
  sendVsync(0);
  clocks(M_LAT, 14);  // PRE_ACT
  clockWord(0);
  for (int i = 0; i < BLOCK_CLOCKS; ++i) {
    uint32_t state = ((reg >> (15 - (i & 15))) & 1) ? M_DATA_ALL : 0;
    if (i >= BLOCK_CLOCKS - 5) state |= M_LAT;  // WR_CFG
    clockWord(state);
  }
  clockWord((reg & 1) ? M_DATA_ALL : 0);
}

void configureDp3364s() {
  for (uint16_t reg : REGISTERS) sendRegister(reg);
}

uint32_t pixelBits(const Color &upper, const Color &lower, int bit_index) {
  // This particular DP3364S panel has R/B exchanged behind its HUB75 input:
  // physical B1/B2 lights red LEDs and physical R1/R2 lights blue LEDs.
  // Keep Color in normal RGB order and correct it here, in software.
  return (((upper.r >> bit_index) & 1) ? M_B1 : 0) |
         (((upper.g >> bit_index) & 1) ? M_G1 : 0) |
         (((upper.b >> bit_index) & 1) ? M_R1 : 0) |
         (((lower.r >> bit_index) & 1) ? M_B2 : 0) |
         (((lower.g >> bit_index) & 1) ? M_G2 : 0) |
         (((lower.b >> bit_index) & 1) ? M_R2 : 0);
}

void scanClock(uint32_t pixel, int &row, int &row_clock,
               int &row_pulse_width, bool &first_row_pulse) {
  if (row_clock == 0) {
    row = (row + 1) % SCAN_ROWS;
    selectMuxRow(row);
    // DP3364S: exactly one W12 starts Group 0 / Line 0 after VSYNC.
    // Every following ROW pulse, including later wraps to Line 0, is W4.
    row_pulse_width = first_row_pulse ? 12 : 4;
    first_row_pulse = false;
  }
  uint32_t state = pixel | addressState(row);
  if (row_clock < row_pulse_width) state |= M_ROW;
  clockWord(state);
  row_clock = (row_clock + 1) % ROW_PERIOD_CLOCKS;
}

enum TestPattern : uint8_t {
  FULL_BLUE = 0,
  COLUMN_MAP = 1,
  ROW_MAP = 2,
  FULL_RED,
  FULL_GREEN,
  FULL_WHITE,
  FULL_BLACK,
  FULL_YELLOW,
  FULL_CYAN,
  FULL_MAGENTA,
  GEOMETRY_MAP,
  GRADIENT_MAP,
  MOTION_DEMO,
  COLOUR_MOTION_DEMO,
};

// Run one test continuously. Change this constant and upload to select another.
constexpr TestPattern ACTIVE_TEST = COLOUR_MOTION_DEMO;
constexpr bool MONO_DOUBLE_BUFFER = ACTIVE_TEST == MOTION_DEMO;
constexpr bool COLOUR_DOUBLE_BUFFER = ACTIVE_TEST == COLOUR_MOTION_DEMO;
constexpr bool DYNAMIC_BUFFERS = MONO_DOUBLE_BUFFER || COLOUR_DOUBLE_BUFFER;
// Control run: complete white text and moving line, with a black perimeter.
constexpr bool ENABLE_MOTION_UPDATES = true;
constexpr bool DRAW_MOTION_LINE = true;
constexpr uint8_t MONO_RGB_MASK = 7;  // R=1, G=2, B=4; 7 is white
uint32_t animation_frame = 0;
static_assert(!MONO_DOUBLE_BUFFER || (USE_LCD_DMA && LEVEL == 0x0400),
              "Motion probe requires DMA and LEVEL=0x0400 (one PWM bit)");

const char *patternName(TestPattern pattern) {
  switch (pattern) {
    case FULL_BLUE: return "FULL BLUE";
    case FULL_RED: return "FULL RED";
    case FULL_GREEN: return "FULL GREEN";
    case FULL_WHITE: return "FULL WHITE";
    case FULL_BLACK: return "FULL BLACK";
    case FULL_YELLOW: return "FULL YELLOW";
    case FULL_CYAN: return "FULL CYAN";
    case FULL_MAGENTA: return "FULL MAGENTA";
    case GEOMETRY_MAP: return "GEOMETRY MAP";
    case GRADIENT_MAP: return "GRADIENT MAP";
    case MOTION_DEMO: return "MOTION DEMO";
    case COLOUR_MOTION_DEMO: return "COLOUR MOTION DEMO";
    case COLUMN_MAP: return "COLUMN MAP";
    case ROW_MAP: return "ROW MAP";
  }
  return "UNKNOWN";
}

Color testPixel(int x, int y, TestPattern pattern, int active_row) {
  constexpr Color BLACK = {0, 0, 0, "BLACK"};
  constexpr Color BLUE = {0, 0, LEVEL, "BLUE"};
  constexpr Color CYAN = {0, LEVEL, LEVEL, "CYAN"};
  constexpr Color RED = {LEVEL, 0, 0, "RED"};

  switch (pattern) {
    case COLOUR_MOTION_DEMO: {
      if (x <= 0 || x >= 159 || y <= 0 || y >= 79) return BLACK;
      constexpr Color palette[] = {
          {LEVEL, 0, 0, "RED"}, {0, LEVEL, 0, "GREEN"},
          {0, 0, LEVEL, "BLUE"}, {LEVEL, LEVEL, 0, "YELLOW"},
          {0, LEVEL, LEVEL, "CYAN"}, {LEVEL, 0, LEVEL, "MAGENTA"},
          {LEVEL, LEVEL, LEVEL, "WHITE"},
      };
      const int phase = animation_frame % 314;
      const int line_x = 1 + (phase < 158 ? phase : 314 - phase);
      if (DRAW_MOTION_LINE && x == line_x && y >= 2 && y <= 77)
        return palette[(y / 12) % 7];
      if (motion_pattern::pixel(x, y, animation_frame, false))
        return palette[((x + animation_frame / 2) / 12) % 7];
      return BLACK;
    }
    case MOTION_DEMO:
      return motion_pattern::pixel(x, y, animation_frame, DRAW_MOTION_LINE) ?
          Color{LEVEL, LEVEL, LEVEL, "WHITE"} : BLACK;
    case FULL_BLUE:
      return BLUE;
    case FULL_RED:
      return RED;
    case FULL_GREEN:
      return {0, LEVEL, 0, "GREEN"};
    case FULL_WHITE:
      return {LEVEL, LEVEL, LEVEL, "WHITE"};
    case FULL_BLACK:
      return BLACK;
    case FULL_YELLOW:
      return {LEVEL, LEVEL, 0, "YELLOW"};
    case FULL_CYAN:
      return CYAN;
    case FULL_MAGENTA:
      return {LEVEL, 0, LEVEL, "MAGENTA"};
    case GRADIENT_MAP: {
      // Four static bars, each with 16 increasing raw PWM codes, no gamma.
      // Keep the outer pixel off and cap the maximum at the tested LEVEL.
      if (x == 0 || x == PANEL_WIDTH - 1 || y % 20 < 2 || y % 20 >= 18)
        return BLACK;
      const int step = ((x - 1) * 16) / (PANEL_WIDTH - 2);
      const uint16_t value = static_cast<uint16_t>((uint32_t(LEVEL) * step) / 15);
      switch (y / 20) {
        case 0: return {value, value, value, "GRAY"};
        case 1: return {value, 0, 0, "RED"};
        case 2: return {0, value, 0, "GREEN"};
        case 3: return {0, 0, value, "BLUE"};
      }
      return BLACK;
    }
    case GEOMETRY_MAP: {
      constexpr int HEIGHT = SCAN_ROWS * 2;
      constexpr Color WHITE = {LEVEL, LEVEL, LEVEL, "WHITE"};
      // One-pixel outer frame; corners are inset to keep it continuous.
      if (x == 0 || x == PANEL_WIDTH - 1 || y == 0 || y == HEIGHT - 1)
        return WHITE;
      const bool left = x >= 2 && x <= 6;
      const bool right = x >= PANEL_WIDTH - 7 && x <= PANEL_WIDTH - 3;
      const bool top = y >= 2 && y <= 6;
      const bool bottom = y >= HEIGHT - 7 && y <= HEIGHT - 3;
      if (left && top) return RED;
      if (right && top) return {0, LEVEL, 0, "GREEN"};
      if (left && bottom) return BLUE;
      if (right && bottom) return WHITE;
      // Symmetric 12-pixel cross straddles both logical centre axes.
      if (((x == 79 || x == 80) && y >= 34 && y <= 45) ||
          ((y == 39 || y == 40) && x >= 74 && x <= 85)) return CYAN;
      return BLACK;
    }
    case COLUMN_MAP:
      // Thin red marks identify the ends of each 16-column DP3364S chip.
      // Cyan at x=79/80 exposes the physical centre of the 160-pixel panel.
      if (x == 79 || x == 80) return CYAN;
      if ((x % 16) == 0 || (x % 16) == 15) return RED;
      return BLUE;
    case ROW_MAP:
      // One logical address lights both physical halves: y and y+40.
      return ((y % SCAN_ROWS) == active_row) ? BLUE : BLACK;
  }
  return BLACK;
}

constexpr int pixelX(int chip, int latch) {
  // The OUT15->OUT0 upload order maps left-to-right on this panel.
  // Reversing x inside each chip mirrored every 16-column block.
  return chip * 16 + (latch % 16);
}

void sendFrame(TestPattern pattern, int active_row) {
  resetMux();
  int row = SCAN_ROWS - 1;
  int row_clock = 0;
  int row_pulse_width = 4;
  bool first_row_pulse = true;
  sendVsync(addressState(row));

  for (int latch = 0; latch < LATCHES_PER_FRAME; ++latch) {
    const int line = latch / 16;
    // Preserve DP3364S upload order; pixelX describes physical LED placement.
    for (int i = 0; i < BLOCK_CLOCKS; ++i) {
      const int chip = i / 16;
      const int x = pixelX(chip, latch);
      const Color upper = testPixel(x, line, pattern, active_row);
      const Color lower = testPixel(x, line + SCAN_ROWS, pattern, active_row);
      scanClock(pixelBits(upper, lower, 15 - (i & 15)), row, row_clock,
                row_pulse_width, first_row_pulse);
    }
    clockWord(addressState(row) | M_LAT);  // DATA_LATCH; scan clock frozen.
  }
  for (int i = 0; i < TAIL_ROWS * BLOCK_CLOCKS; ++i)
    scanClock(0, row, row_clock, row_pulse_width, first_row_pulse);
}

constexpr size_t DMA_SCAN_CLOCKS =
    LATCHES_PER_FRAME * BLOCK_CLOCKS + TAIL_ROWS * BLOCK_CLOCKS;
constexpr size_t DMA_FRAME_WORDS =
    SCAN_ROWS * 3 + 20 + DMA_SCAN_CLOCKS + LATCHES_PER_FRAME;
static_assert((DMA_FRAME_WORDS % 2) == 0, "DMA size must be 4-byte aligned");
static_assert(MUX_BLANK_CLOCKS >= 3 && ROW_PERIOD_CLOCKS > MUX_BLANK_CLOCKS + 12,
              "ROW needs mux setup and W12");
static_assert(PIN_R1 == 4 && PIN_G1 == 5 && PIN_B1 == 6 && PIN_R2 == 7 &&
              PIN_G2 == 8 && PIN_B2 == 9 && PIN_A == 10 && PIN_B == 11 &&
              PIN_C == 12 && PIN_D == 13 && PIN_E == 14 && PIN_LAT == 15 &&
              PIN_ROW == 16 && PIN_CLK == 17, "DMA pin mapping must be updated");
static_assert(!USE_LCD_DMA || ACTIVE_TEST != ROW_MAP,
              "DMA snapshot is static: use CPU mode for moving ROW_MAP");

// DMA data bits 0..12 map to GPIO4..16; DCLK is hardware GPIO17.
uint16_t packDma(uint32_t state) {
  if (!MONO_DOUBLE_BUFFER) return static_cast<uint16_t>(state >> PIN_R1);
  return ((state & M_R1) ? 1 : 0) | ((state & M_R2) ? 2 : 0) |
         ((state & M_A) ? 4 : 0) | ((state & M_B) ? 8 : 0) |
         ((state & M_C) ? 16 : 0) | ((state & M_LAT) ? 32 : 0) |
         ((state & M_ROW) ? 64 : 0);
}

bool emitDma(uint32_t gpio_state) {
  return lcd_dma::append(packDma(gpio_state));
}

bool buildDmaFrame() {
  if (!USE_SHIFTREG_ABC) return false;
  // Clear the row chain with outputs blanked, then VSYNC. Unlike bit-bang,
  // these housekeeping samples also have DCLK; they carry no DATA_LATCH.
  for (int i = 0; i < SCAN_ROWS; ++i) {
    if (!emitDma(M_B) || !emitDma(M_B | M_A) || !emitDma(M_B)) return false;
  }
  for (int i = 0; i < 20; ++i)
    if (!emitDma(M_B | (i < 3 ? M_LAT : 0))) return false;

  uint32_t last_mux = M_B;
  Color upper = {}, lower = {};
  for (size_t p = 0; p < DMA_SCAN_CLOCKS; ++p) {
    const int phase = p % ROW_PERIOD_CLOCKS;
    const int row = (p / ROW_PERIOD_CLOCKS) % SCAN_ROWS;
    const uint32_t token = row == 0 ? M_C : 0;
    // Move one row token in three samples, allow settling while B blanks,
    // then issue DP3364S ROW. There is still one rising A edge per row.
    last_mux = token | (phase < MUX_BLANK_CLOCKS ? M_B : 0) | (phase == 1 ? M_A : 0);
    uint32_t state = last_mux;
    const int width = p < ROW_PERIOD_CLOCKS ? 12 : 4;
    if (phase >= MUX_BLANK_CLOCKS && phase < MUX_BLANK_CLOCKS + width) state |= M_ROW;
    if (p < static_cast<size_t>(LATCHES_PER_FRAME * BLOCK_CLOCKS)) {
      const int latch = p / BLOCK_CLOCKS;
      const int i = p % BLOCK_CLOCKS;
      const int line = latch / 16;
      const int x = pixelX(i / 16, latch);
      // One image lookup per pixel, reuse the colour while emitting its bits.
      if ((i & 15) == 0) {
        upper = testPixel(x, line, ACTIVE_TEST, 0);
        lower = testPixel(x, line + SCAN_ROWS, ACTIVE_TEST, 0);
      }
      state |= pixelBits(upper, lower, 15 - (i & 15));
    }
    if (!emitDma(state)) return false;
    if (p < static_cast<size_t>(LATCHES_PER_FRAME * BLOCK_CLOCKS) &&
        ((p + 1) % BLOCK_CLOCKS == 0)) {
      if (!emitDma(last_mux | M_LAT)) return false;
    }
  }
  return true;
}

// Parse the finished waveform before handing it to DMA. Check command widths,
// ROW pulse widths, and one-hot row selection, including the ring boundary.
bool validateDmaFrame() {
  const uint16_t A = packDma(M_A), B = packDma(M_B);
  const uint16_t C = packDma(M_C), LAT = packDma(M_LAT);
  const uint16_t ROW = packDma(M_ROW);
  uint64_t selector = 0;
  constexpr uint64_t mask = (1ULL << SCAN_ROWS) - 1;
  unsigned latch_pulses = 0, vsyncs = 0, row_pulses = 0;
  unsigned lat_width = 0, row_width = 0;
  uint16_t previous = lcd_dma::wordAt(DMA_FRAME_WORDS - 1);
  for (size_t i = 0; i <= DMA_FRAME_WORDS; ++i) {
    const uint16_t value = lcd_dma::wordAt(i % DMA_FRAME_WORDS);
    if ((value & A) && !(previous & A)) {
      if (!(value & B)) return false;  // row shifts must be blanked
      selector = ((selector << 1) | ((value & C) ? 1 : 0)) & mask;
    }
    if ((value & ROW) && !(previous & ROW)) {
      if ((value & B) || !selector || (selector & (selector - 1))) return false;
      ++row_pulses;
    }
    if (value & ROW) ++row_width;
    else if (row_width) {
      if (row_width != (row_pulses == 1 ? 12u : 4u)) return false;
      row_width = 0;
    }
    if (value & LAT) ++lat_width;
    else if (lat_width) {
      if (lat_width == 1) ++latch_pulses;
      else if (lat_width == 3) ++vsyncs;
      else return false;
      lat_width = 0;
    }
    previous = value;
  }
  return latch_pulses == LATCHES_PER_FRAME && vsyncs == 1 &&
         row_pulses == DMA_SCAN_CLOCKS / ROW_PERIOD_CLOCKS;
}

bool validateMotionDarkRows() {
  if (!DYNAMIC_BUFFERS) return true;
  constexpr size_t preamble = SCAN_ROWS * 3 + 20;
  // Decode all 16 transmitted PWM bits, not just the rendered image request.
  // Only the upper group's rows 0/1 and lower group's rows 78/79 must be black.
  for (int latch = 0; latch < LATCHES_PER_FRAME; ++latch) {
    const int y = latch / 16;
    const uint16_t forbidden = y < 2 ? packDma(M_R1 | M_G1 | M_B1) :
        (y >= SCAN_ROWS - 2 ? packDma(M_R2 | M_G2 | M_B2) : 0);
    if (!forbidden) continue;
    for (int i = 0; i < BLOCK_CLOCKS; ++i) {
      const size_t index = preamble + latch * (BLOCK_CLOCKS + 1) + i;
      if (lcd_dma::wordAt(index) & forbidden) return false;
    }
  }
  return true;
}

bool updateMotionFrame() {
  if (COLOUR_DOUBLE_BUFFER) {
    // Rebuild the retired ring into immutable deduplicated packets. Never
    // modify any packet shared with the active ring; publish only when complete.
    return buildDmaFrame() && lcd_dma::present();
  }
  // Only bit 10 is used by this black/white probe. Change the two RGB-group
  // bits at those 6400 sample positions; all timing/control words are immutable.
  constexpr size_t preamble = SCAN_ROWS * 3 + 20;
  for (int latch = 0; latch < LATCHES_PER_FRAME; ++latch) {
    const int y = latch / 16;
    for (int chip = 0; chip < CHIPS; ++chip) {
      const int x = pixelX(chip, latch);
      const size_t index = preamble + latch * (BLOCK_CLOCKS + 1) + chip * 16 + 5;
      const uint16_t rgb = (motion_pattern::pixel(x, y, animation_frame, DRAW_MOTION_LINE) ? 1 : 0) |
          (motion_pattern::pixel(x, y + SCAN_ROWS, animation_frame, DRAW_MOTION_LINE) ? 2 : 0);
      if (!lcd_dma::setWord(index, (lcd_dma::wordAt(index) & ~3u) | rgb)) return false;
    }
  }
  return lcd_dma::present();
}

void configurePins() {
  const uint8_t pins[] = {PIN_R1, PIN_G1, PIN_B1, PIN_R2, PIN_G2, PIN_B2,
                          PIN_A, PIN_B, PIN_C, PIN_D, PIN_E, PIN_LAT,
                          PIN_ROW, PIN_CLK};
  for (uint8_t pin : pins) { pinMode(pin, OUTPUT); digitalWrite(pin, LOW); }
  GPIO.out_w1tc = M_BUS;
}
}  // namespace

void setup() {
  Serial.begin(115200);
  delay(1200);
  configurePins();
  Serial.println();
  Serial.println("DP3364S Stage 4: 160x80 / 1:40 S-PWM protocol probe");
  Serial.println("SDR + VSYNC + PRE_ACT + WR_CFG + W12/W4 ROW stream");
  Serial.printf("ROW period: %d scan clocks; data block: %d clocks\n",
                ROW_PERIOD_CLOCKS, BLOCK_CLOCKS);
  Serial.println(USE_SHIFTREG_ABC ?
      "MUX: SHIFTREG_ABC / A=clock B=/enable C=data; full blue confirmed" :
      "MUX TEST: BINARY A-E (previous probe)");
  enterSingleEdgeMode();
  configureDp3364s();
  Serial.println("DP3364S register upload complete.");
  Serial.println("Register init: startup only; periodic reinit disabled.");
  Serial.printf("TEST: %s\n", patternName(ACTIVE_TEST));
  if (USE_LCD_DMA) {
    resetMux();  // Keep the panel blank while allocating/building the stream.
    Serial.printf("DMA free internal: %u bytes; waveform: %u bytes\n",
        static_cast<unsigned>(heap_caps_get_free_size(MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL)),
        static_cast<unsigned>(DMA_FRAME_WORDS * 2));
    bool built = lcd_dma::allocate(DMA_FRAME_WORDS, MONO_DOUBLE_BUFFER, COLOUR_DOUBLE_BUFFER);
    for (unsigned b = 0; built && b < (DYNAMIC_BUFFERS ? 2u : 1u); ++b)
      built = lcd_dma::selectBuildBuffer(b) && buildDmaFrame() &&
              validateDmaFrame() && validateMotionDarkRows();
    if (!built) {
      Serial.printf("DMA build/validation FAILED: %s. Output stopped.\n", lcd_dma::error());
      output_failed = true;
      GPIO.out_w1tc = M_BUS;
      GPIO.out_w1ts = M_B;
      return;
    }
    Serial.printf("DMA waveform verified: %u words; %u Hz DCLK; %.2f frames/s\n",
        static_cast<unsigned>(DMA_FRAME_WORDS), static_cast<unsigned>(DMA_CLOCK_HZ),
        static_cast<double>(DMA_CLOCK_HZ) / DMA_FRAME_WORDS);
    Serial.printf("MUX blank: %d clocks; dark edge PWM rows verified.\n", MUX_BLANK_CLOCKS);
    dma_active = lcd_dma::start(DMA_CLOCK_HZ, MONO_RGB_MASK);
    if (!dma_active) {
      Serial.printf("DMA start FAILED: %s. Output stopped.\n", lcd_dma::error());
      output_failed = true;
      GPIO.out_w1tc = M_BUS;
      GPIO.out_w1ts = M_B;
    } else {
      Serial.println("LCD_CAM + GDMA running continuously; no periodic register writes.");
      if (COLOUR_DOUBLE_BUFFER)
        Serial.printf("Colour: two 16-bit rings, shared immutable packets; %u payload bytes.\n",
                      static_cast<unsigned>(lcd_dma::bytes()));
      if (MONO_DOUBLE_BUFFER) {
        Serial.printf("MONO logical RGB mask: %u (R=1 G=2 B=4)\n", MONO_RGB_MASK);
        Serial.printf("Motion: two 8-bit buffers, %u bytes total.\n",
                      static_cast<unsigned>(lcd_dma::bytes()));
        Serial.println(ENABLE_MOTION_UPDATES ? "Frame-boundary swaps enabled." :
            "HOLD TEST: frame 240; no buffer writes or swaps after startup.");
        Serial.println(DRAW_MOTION_LINE ? "Vertical line enabled." :
            "TEXT ONLY: vertical line disabled.");
      }
    }
  }
}

void loop() {
  if (dma_active || output_failed) {
    if (dma_active && DYNAMIC_BUFFERS && ENABLE_MOTION_UPDATES && !output_failed) {
      static uint32_t last_step = 0, last_report = 0, last_presented = 0;
      const uint32_t now = millis();
      if (now - last_step >= 33 && lcd_dma::beginUpdate()) {
        ++animation_frame;
        if (!updateMotionFrame()) {
          Serial.printf("ERROR: motion update failed: %s\n", lcd_dma::error());
          output_failed = true;
        }
        last_step = now;
      }
      if (now - last_report >= 5000) {
        const uint32_t presented = lcd_dma::presentedFrames();
        Serial.printf("Motion: %u frames presented, %.1f updates/s\n",
            static_cast<unsigned>(presented),
            (presented - last_presented) * 1000.0 / (now - last_report));
        if (COLOUR_DOUBLE_BUFFER)
          Serial.printf("Colour payload: %u bytes, peak %u; internal DMA free %u\n",
              static_cast<unsigned>(lcd_dma::bytes()), static_cast<unsigned>(lcd_dma::peakBytes()),
              static_cast<unsigned>(heap_caps_get_free_size(MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL)));
        if (presented == last_presented)
          Serial.println("ERROR: no DMA frame-swap acknowledgements.");
        last_presented = presented;
        last_report = now;
      }
      delay(1);
    } else {
      delay(1000);
    }
    if (dma_active && !lcd_dma::running()) {
      Serial.println("ERROR: LCD stream stopped; restart ESP32.");
      dma_active = false;
      output_failed = true;
    }
    return;
  }
  const int active_row = (millis() / 600) % SCAN_ROWS;
  // Experiment: configure registers only in setup(), without interrupting
  // the frame stream with periodic register writes and their extra VSYNCs.
  sendFrame(ACTIVE_TEST, active_row);
}
