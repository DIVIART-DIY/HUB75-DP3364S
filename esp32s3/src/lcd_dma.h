#pragma once
#include <stddef.h>
#include <stdint.h>

// Static 16-bit colour, double 8-bit mono, or two 16-bit colour rings sharing
// immutable deduplicated payload packets. Colour packet RAM is image-dependent.
// GPIO17 is hardware DCLK. EOF events acknowledge whole-frame transitions.
namespace lcd_dma {
bool allocate(size_t words, bool mono_double_buffer = false, bool colour_packets = false);
bool append(uint16_t word);
uint16_t wordAt(size_t index);
bool selectBuildBuffer(unsigned index);  // before start, for initial generation
bool beginUpdate();                     // acquires the retired buffer, never waits
bool setWord(size_t index, uint16_t word);
bool present();                         // links the finished frame for DMA
uint32_t presentedFrames();
bool start(uint32_t clock_hz, uint8_t mono_rgb_mask = 7); // logical R=1,G=2,B=4
bool running();
const char *error();
size_t bytes();
size_t peakBytes();
}
