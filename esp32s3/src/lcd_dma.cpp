// LCD_CAM setup adapted from kkwestt/RaspberryPi-HUB75-DP3364S-Matrix
// Copyright (c) 2026 Konstantin, MIT. See THIRD_PARTY_NOTICES.md.
#include "lcd_dma.h"
#include "driver/gpio.h"
#include "driver/periph_ctrl.h"
#include "esp_heap_caps.h"
#include "esp_private/gdma.h"
#include "esp_rom_gpio.h"
#include "hal/dma_types.h"
#include "hal/gpio_hal.h"
#include "soc/gpio_sig_map.h"
#include "soc/io_mux_reg.h"
#include "soc/lcd_cam_struct.h"
#include "rom/ets_sys.h"
#include "freertos/FreeRTOS.h"
#include "freertos/portmacro.h"
#include <string.h>

namespace lcd_dma {
namespace {
constexpr size_t CHUNK = 4092;
// 322 words = two 160-bit upload blocks plus their DATA_LATCH clocks.
// All buffers and descriptor lengths remain multiples of four bytes.
constexpr size_t PACKET_BYTES = 644, MAX_PACKETS = 768;
struct Packet {
  uint8_t *data;
  uint32_t hash;
  uint16_t length, references;
};
Packet packets[MAX_PACKETS] = {};
uint16_t packet_staging[PACKET_BYTES / 2];
bool packet_mode = false;
size_t chunk_bytes = CHUNK, packet_bytes = 0, peak_packet_bytes = 0;
dma_descriptor_t *descriptors[2] = {};
size_t count = 0, capacity = 0, used[2] = {};
unsigned buffers = 1, sample_bytes = 2, build_index = 0;
bool acquired = false;
volatile unsigned active_index = 0, pending_index = 0;
volatile bool pending = false;
volatile uint32_t presentations = 0;
portMUX_TYPE swap_lock = portMUX_INITIALIZER_UNLOCKED;
gdma_channel_handle_t channel = nullptr;
const char *failure = "none";

// Packet buffers are immutable while referenced by either DMA ring. Only the
// retired ring may drop references. This function must never run in the ISR.
void retirePackets(unsigned bank) {
  if (!descriptors[bank]) return;
  for (size_t d = 0; d < count; ++d) {
    void *data = descriptors[bank][d].buffer;
    if (!data) continue;
    for (auto &p : packets) {
      if (p.data != data) continue;
      if (--p.references == 0) {
        packet_bytes -= p.length;
        heap_caps_free(p.data);
        p = {};
      }
      break;
    }
    descriptors[bank][d].buffer = nullptr;
  }
}

bool internPacket(size_t descriptor, size_t length) {
  const auto *data = reinterpret_cast<const uint8_t *>(packet_staging);
  uint32_t hash = 2166136261u;
  for (size_t i = 0; i < length; ++i) hash = (hash ^ data[i]) * 16777619u;
  Packet *vacant = nullptr;
  for (auto &p : packets) {
    if (!p.data) { if (!vacant) vacant = &p; continue; }
    // Always compare bytes as well: a hash collision cannot alter the image.
    if (p.hash == hash && p.length == length && memcmp(p.data, data, length) == 0) {
      ++p.references;
      descriptors[build_index][descriptor].buffer = p.data;
      return true;
    }
  }
  if (!vacant) { failure = "colour packet table full"; return false; }
  auto *copy = static_cast<uint8_t *>(heap_caps_malloc(length, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL));
  if (!copy) { failure = "no SRAM for next colour frame; last frame retained"; return false; }
  memcpy(copy, data, length);
  *vacant = {copy, hash, static_cast<uint16_t>(length), 1};
  packet_bytes += length;
  if (packet_bytes > peak_packet_bytes) peak_packet_bytes = packet_bytes;
  descriptors[build_index][descriptor].buffer = copy;
  return true;
}

bool IRAM_ATTR frameEof(gdma_channel_handle_t, gdma_event_data_t *event, void *) {
  portENTER_CRITICAL_ISR(&swap_lock);
  // Do not recycle the old waveform at its EOF: its successor may already
  // have been prefetched. Wait until the NEW ring's EOF has actually arrived.
  if (pending && event->tx_eof_desc_addr ==
          reinterpret_cast<intptr_t>(&descriptors[pending_index][count - 1])) {
    active_index = pending_index;
    pending = false;
    ++presentations;
  }
  portEXIT_CRITICAL_ISR(&swap_lock);
  return false;
}

void release() {
  if (channel) {
    gdma_disconnect(channel);
    gdma_del_channel(channel);
    channel = nullptr;
  }
  for (unsigned b = 0; b < buffers; ++b) {
    if (!descriptors[b]) continue;
    if (packet_mode) retirePackets(b);
    else for (size_t i = 0; i < count; ++i) heap_caps_free(descriptors[b][i].buffer);
    heap_caps_free(descriptors[b]);
    descriptors[b] = nullptr;
  }
  count = capacity = used[0] = used[1] = 0;
}

bool fail(const char *message) {
  failure = message;
  release();
  return false;
}

void route(int pin, int signal) {
  gpio_hal_iomux_func_sel(GPIO_PIN_MUX_REG[pin], PIN_FUNC_GPIO);
  gpio_set_direction(static_cast<gpio_num_t>(pin), GPIO_MODE_OUTPUT);
  gpio_set_drive_capability(static_cast<gpio_num_t>(pin), GPIO_DRIVE_CAP_3);
  esp_rom_gpio_connect_out_signal(pin, signal, false, false);
}
}  // namespace

bool allocate(size_t words, bool mono_double_buffer, bool colour_packets) {
  if (descriptors[0] || channel) return false;
  packet_mode = colour_packets;
  sample_bytes = mono_double_buffer && !colour_packets ? 1 : 2;
  buffers = mono_double_buffer || colour_packets ? 2 : 1;
  chunk_bytes = packet_mode ? PACKET_BYTES : CHUNK;
  if (!words || ((words * sample_bytes) % 4)) return false;
  acquired = false;
  pending = false;
  active_index = pending_index = 0;
  presentations = 0;
  packet_bytes = peak_packet_bytes = 0;
  used[0] = used[1] = 0;
  capacity = words;
  count = (words * sample_bytes + chunk_bytes - 1) / chunk_bytes;
  for (unsigned b = 0; b < buffers; ++b) {
    descriptors[b] = static_cast<dma_descriptor_t *>(heap_caps_calloc(
        count, sizeof(dma_descriptor_t), MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL));
    if (!descriptors[b]) return fail("no SRAM for DMA descriptors");
    size_t remaining = words * sample_bytes;
    for (size_t i = 0; i < count; ++i) {
      const size_t n = remaining > chunk_bytes ? chunk_bytes : remaining;
      auto &d = descriptors[b][i];
      if (!packet_mode) {
        d.buffer = static_cast<uint8_t *>(heap_caps_malloc(n, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL));
        if (!d.buffer) return fail("no internal DMA SRAM for waveform");
      }
      d.dw0.size = n;
      d.dw0.length = n;
      d.dw0.owner = DMA_DESCRIPTOR_BUFFER_OWNER_DMA;
      d.dw0.suc_eof = 0;
      d.next = &descriptors[b][(i + 1) % count];
      remaining -= n;
    }
    descriptors[b][count - 1].dw0.suc_eof = 1;
  }
  build_index = 0;
  return true;
}

bool setWord(size_t index, uint16_t word) {
  if (packet_mode) return false;  // shared payloads must never be edited in place
  if (!descriptors[build_index] || index >= capacity ||
      (channel && !acquired) || (sample_bytes == 1 && word > 255)) return false;
  const size_t offset = index * sample_bytes;
  auto *p = static_cast<uint8_t *>(descriptors[build_index][offset / chunk_bytes].buffer);
  if (sample_bytes == 1) p[offset % chunk_bytes] = word;
  else reinterpret_cast<uint16_t *>(p)[(offset % chunk_bytes) / 2] = word;
  return true;
}

bool append(uint16_t word) {
  if (packet_mode) {
    if (used[build_index] >= capacity || (channel && !acquired)) return false;
    const size_t offset = used[build_index] * 2;
    packet_staging[(offset % chunk_bytes) / 2] = word;
    ++used[build_index];
    if ((used[build_index] * 2) % chunk_bytes == 0 || used[build_index] == capacity) {
      const size_t length = used[build_index] == capacity ?
          descriptors[build_index][offset / chunk_bytes].dw0.length : chunk_bytes;
      return internPacket(offset / chunk_bytes, length);
    }
    return true;
  }
  if (used[build_index] >= capacity || !setWord(used[build_index], word)) {
    failure = "waveform overflow or unavailable buffer";
    return false;
  }
  ++used[build_index];
  return true;
}

uint16_t wordAt(size_t index) {
  if (!descriptors[build_index] || index >= used[build_index]) return 0;
  const size_t offset = index * sample_bytes;
  const auto *p = static_cast<const uint8_t *>(descriptors[build_index][offset / chunk_bytes].buffer);
  if (!p) return 0;
  return sample_bytes == 1 ? p[offset % chunk_bytes] :
      reinterpret_cast<const uint16_t *>(p)[(offset % chunk_bytes) / 2];
}

bool selectBuildBuffer(unsigned index) {
  if (channel || index >= buffers) return false;
  build_index = index;
  return true;
}

bool beginUpdate() {
  bool ready = false;
  portENTER_CRITICAL(&swap_lock);
  if (channel && buffers == 2 && !pending && !acquired) {
    build_index = 1 - active_index;
    descriptors[build_index][count - 1].next = &descriptors[build_index][0];
    acquired = ready = true;
  }
  portEXIT_CRITICAL(&swap_lock);
  if (ready && packet_mode) {
    retirePackets(build_index);
    used[build_index] = 0;
  }
  return ready;
}

bool present() {
  if (!acquired || used[build_index] != capacity) return false;
  for (size_t i = 0; i < count; ++i)
    if (!descriptors[build_index][i].buffer) return false;
  bool submitted = false;
  portENTER_CRITICAL(&swap_lock);
  if (acquired && !pending && used[build_index] == capacity) {
    pending_index = build_index;
    pending = true;
    // SRAM is not cached. Publish all sample writes before the aligned
    // descriptor-link store. LCD/DCLK is never stopped to change frames.
    __asm__ __volatile__("memw" ::: "memory");
    descriptors[active_index][count - 1].next = &descriptors[build_index][0];
    __asm__ __volatile__("memw" ::: "memory");
    acquired = false;
    submitted = true;
  }
  portEXIT_CRITICAL(&swap_lock);
  return submitted;
}

bool start(uint32_t clock_hz, uint8_t mono_rgb_mask) {
  for (unsigned b = 0; b < buffers; ++b)
    if (!descriptors[b] || used[b] != capacity) return fail("incomplete waveform");
  // Keep supported clock divisors exact for this probe.
  if (clock_hz != 8000000 && clock_hz != 4000000) return fail("unsupported DCLK");
  gdma_channel_alloc_config_t config = {};
  config.direction = GDMA_CHANNEL_DIRECTION_TX;
  if (gdma_new_channel(&config, &channel) != ESP_OK) return fail("no GDMA TX channel");
  if (gdma_connect(channel, GDMA_MAKE_TRIGGER(GDMA_TRIG_PERIPH_LCD, 0)) != ESP_OK)
    return fail("GDMA LCD connect failed");
  gdma_strategy_config_t strategy = {};
  strategy.owner_check = false;
  strategy.auto_update_desc = false;
  if (gdma_apply_strategy(channel, &strategy) != ESP_OK) return fail("GDMA strategy failed");
  gdma_transfer_ability_t ability = {};
  ability.sram_trans_align = 4;
  ability.psram_trans_align = 64;
  if (gdma_set_transfer_ability(channel, &ability) != ESP_OK) return fail("GDMA alignment failed");
  if (buffers == 2) {
    gdma_tx_event_callbacks_t callbacks = {};
    callbacks.on_trans_eof = frameEof;
    if (gdma_register_tx_event_callbacks(channel, &callbacks, nullptr) != ESP_OK)
      return fail("GDMA EOF callback failed");
  }

  periph_module_enable(PERIPH_LCD_CAM_MODULE);
  periph_module_reset(PERIPH_LCD_CAM_MODULE);
  LCD_CAM.lcd_user.lcd_reset = 1;
  ets_delay_us(100);
  LCD_CAM.lcd_clock.clk_en = 1;
  LCD_CAM.lcd_clock.lcd_clk_sel = 3;
  LCD_CAM.lcd_clock.lcd_clkm_div_num = 160000000u / clock_hz;
  LCD_CAM.lcd_clock.lcd_clkm_div_a = 1;
  LCD_CAM.lcd_clock.lcd_clkm_div_b = 0;
  LCD_CAM.lcd_clock.lcd_clkcnt_n = 1;
  LCD_CAM.lcd_clock.lcd_clk_equ_sysclk = 1;
  LCD_CAM.lcd_clock.lcd_ck_idle_edge = 0;
  LCD_CAM.lcd_clock.lcd_ck_out_edge = 0;
  LCD_CAM.lcd_ctrl.lcd_rgb_mode_en = 0;
  LCD_CAM.lcd_rgb_yuv.lcd_conv_bypass = 0;
  LCD_CAM.lcd_misc.lcd_next_frame_en = 0;
  LCD_CAM.lcd_misc.lcd_bk_en = 1;
  LCD_CAM.lcd_misc.lcd_vfk_cyclelen = 0;
  LCD_CAM.lcd_misc.lcd_vbk_cyclelen = 0;
  LCD_CAM.lcd_data_dout_mode.val = 0;
  LCD_CAM.lcd_user.lcd_always_out_en = 1;
  LCD_CAM.lcd_user.lcd_8bits_order = 0;
  LCD_CAM.lcd_user.lcd_bit_order = 0;
  LCD_CAM.lcd_user.lcd_byte_order = 0;
  LCD_CAM.lcd_user.lcd_2byte_en = sample_bytes == 2;
  LCD_CAM.lcd_user.lcd_cmd = 0;
  LCD_CAM.lcd_user.lcd_dummy = 0;
  LCD_CAM.lcd_user.lcd_dummy_cyclelen = 0;
  LCD_CAM.lcd_user.lcd_dout = 1;
  LCD_CAM.lcd_user.lcd_update = 1;

  LCD_CAM.lcd_user.lcd_start = 0;
  gdma_reset(channel);
  LCD_CAM.lcd_misc.lcd_afifo_reset = 1;
  if (gdma_start(channel, reinterpret_cast<intptr_t>(descriptors[0])) != ESP_OK)
    return fail("GDMA start failed");
  // Route only after all fallible setup has succeeded.
  if (sample_bytes == 2) {
    for (int i = 0; i < 13; ++i) route(4 + i, LCD_DATA_OUT0_IDX + i);
  } else {
    // A single PWM waveform per RGB group; select its logical tint at routing.
    // This panel exchanges R/B: GPIO4/7 = blue, 5/8 = green, 6/9 = red.
    const uint8_t logical_colour[] = {4, 2, 1};
    for (int group = 0; group < 2; ++group) {
      for (int colour = 0; colour < 3; ++colour) {
        const int pin = 4 + group * 3 + colour;
        if (mono_rgb_mask & logical_colour[colour]) {
          route(pin, LCD_DATA_OUT0_IDX + group);
        } else {
          esp_rom_gpio_connect_out_signal(pin, SIG_GPIO_OUT_IDX, false, false);
          gpio_set_level(static_cast<gpio_num_t>(pin), 0);
        }
      }
    }
    route(10, LCD_DATA_OUT0_IDX + 2);  // A
    route(11, LCD_DATA_OUT0_IDX + 3);  // B
    route(12, LCD_DATA_OUT0_IDX + 4);  // C
    route(15, LCD_DATA_OUT0_IDX + 5);  // LAT
    route(16, LCD_DATA_OUT0_IDX + 6);  // ROW; D/E remain GPIO LOW
  }
  route(17, LCD_PCLK_IDX);
  ets_delay_us(100);
  LCD_CAM.lcd_user.lcd_start = 1;
  return true;
}

bool running() { return channel && LCD_CAM.lcd_user.lcd_start; }
const char *error() { return failure; }
uint32_t presentedFrames() { return presentations; }
size_t bytes() { return packet_mode ? packet_bytes : capacity * sample_bytes * buffers; }
size_t peakBytes() { return packet_mode ? peak_packet_bytes : bytes(); }
}  // namespace lcd_dma
