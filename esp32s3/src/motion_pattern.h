#pragma once
#include <stdint.h>

namespace motion_pattern {
// Ghosting isolation: blank the final scan address of the upper RGB group.
// Set to -1 to restore the complete glyphs.
constexpr int DIAGNOSTIC_BLANK_ROW = -1;
// Small original 5x7 font, sufficient for the diagnostic text "HUB75 DMA".
inline uint8_t glyphRow(char ch, int row) {
  static const uint8_t H[] = {17, 17, 17, 31, 17, 17, 17};
  static const uint8_t U[] = {17, 17, 17, 17, 17, 17, 14};
  static const uint8_t B[] = {30, 17, 17, 30, 17, 17, 30};
  static const uint8_t N7[] = {31, 1, 2, 4, 8, 8, 8};
  static const uint8_t N5[] = {31, 16, 16, 30, 1, 1, 30};
  static const uint8_t D[] = {30, 17, 17, 17, 17, 17, 30};
  static const uint8_t M[] = {17, 27, 21, 21, 17, 17, 17};
  static const uint8_t A[] = {14, 17, 17, 31, 17, 17, 17};
  if (row < 0 || row >= 7) return 0;
  switch (ch) {
    case 'H': return H[row];
    case 'U': return U[row];
    case 'B': return B[row];
    case '7': return N7[row];
    case '5': return N5[row];
    case 'D': return D[row];
    case 'M': return M[row];
    case 'A': return A[row];
    default: return 0;
  }
}

inline bool pixel(int x, int y, uint32_t frame, bool draw_line = true) {
  if (x <= 0 || x >= 159 || y <= 0 || y >= 79) return false;
  if (y == DIAGNOSTIC_BLANK_ROW) return false;
  const int phase = frame % 314;
  const int line_x = 1 + (phase < 158 ? phase : 314 - phase);
  if (draw_line && x == line_x && y >= 2 && y <= 77) return true;
  static const char text[] = "HUB75 DMA";
  constexpr int scale = 2, advance = 6 * scale;
  constexpr int width = (sizeof(text) - 1) * advance - scale;
  const int text_x = 158 - ((frame / 2) % (158 + width));
  const int dx = x - text_x;
  const int dy = y - 33;  // repeat the original position across the y=39/40 boundary
  if (dx < 0 || dx >= width || dy < 0 || dy >= 14) return false;
  const int column = (dx % advance) / scale;
  if (column >= 5) return false;
  return (glyphRow(text[dx / advance], dy / scale) & (1u << (4 - column))) != 0;
}
}  // namespace motion_pattern
