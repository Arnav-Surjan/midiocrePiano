/*
 * Solenoid Piano Controller — Arduino UNO R4 Minima
 * ===================================================
 *
 * Receives a timestamped event list from a PC over USB Serial,
 * then plays it back with timer precision through 6x PCA9685
 * PWM drivers controlling 88 solenoids via MOSFETs.
 *
 * Uses the Adafruit PWM Servo Driver library for PCA9685 init only;
 * runtime writes bypass the library and use a deferred burst-write
 * queue to coalesce chord transitions into single I²C transactions.
 *
 * STREAMING MODE: The event buffer is now a ring buffer. The PC feeds
 * events during playback, so song length is unbounded by MCU RAM.
 * The ACK packet carries the current free-slot count so the PC can
 * throttle to avoid overruns. Playback ends only when the PC sends
 * CMD_EOS *and* the ring drains to zero.
 *
 * Install Adafruit lib via: Sketch -> Include Library -> Manage Libraries
 *              -> search "Adafruit PWM Servo" -> Install
 *
 * Hardware connections:
 *   USB-C           — Serial to PC (115200 baud)
 *   I2C (Wire)      — PCA9685 bus (6 boards at 0x40-0x45)
 *       D18/SDA     = SDA
 *       D19/SCL     = SCL
 *   External 4.7k pull-ups on SDA and SCL to 5V
 *   PCA9685 VCC     — 5V from Arduino 5V pin
 *   PCA9685 GND     — Arduino GND
 *   PCA9685 OE      — Tie to GND
 */

#include <Wire.h>
#include <Adafruit_PWMServoDriver.h>

// =========================================================================
// Configuration — tune these on the bench
// =========================================================================

// Solenoid timing
#define STRIKE_DURATION_US   200000   // full power strike duration
#define RESTRIKE_GAP_US      110000   // silence between repeated notes
#define HOLD_DUTY_MIN        800      // ~20% of 4095
#define HOLD_DUTY_MAX        1600     // ~40% of 4095

// PCA9685
#define PCA9685_NUM_BOARDS   5 // 6
#define PCA9685_CHANNELS_PER 16
#define SOLENOID_COUNT       74 // 88
#define PCA9685_PWM_FREQ_HZ  1000
#define PCA9685_BASE_ADDR    0x40

// PCA9685 register map (only what we need at runtime)
#define PCA9685_LED0_ON_L    0x06

// 12-bit PWM values
#define PWM_ALWAYS_ON_BIT    0x1000   // Bit 4 of LEDn_ON_H = "always on"
#define PWM_FULL_OFF         0

// Streaming ring buffer. 1024 slots * 8 bytes = 8 KB SRAM.
// At 115200 baud the PC can feed ~1400 events/sec, and even dense piano
// passages peak around 100 events/sec, so this gives ~10 s of lead time.
#define RING_CAPACITY        1024

// Serial protocol
#define PACKET_HEADER        0xAA
#define PACKET_FOOTER        0x55
#define CMD_EVENT_BATCH      0x01
#define CMD_START            0x02
#define CMD_STOP             0x03
#define CMD_PING             0x04
#define CMD_EOS              0x05   // PC: "no more events coming"
#define CMD_ACK              0x10
#define BATCH_SIZE           64

// LED
#define LED_PIN              LED_BUILTIN  // P111 on UNO R4 Minima


// =========================================================================
// PCA9685 — Adafruit library for init only, raw Wire for runtime bursts
// =========================================================================

Adafruit_PWMServoDriver pcaBoards[PCA9685_NUM_BOARDS] = {
    Adafruit_PWMServoDriver(0x40),
    Adafruit_PWMServoDriver(0x41),
    Adafruit_PWMServoDriver(0x42),
    Adafruit_PWMServoDriver(0x43),
    Adafruit_PWMServoDriver(0x44)
    // Adafruit_PWMServoDriver(0x45)
};

// --- Deferred-write queue -------------------------------------------------
//
// All solenoid state changes during a loop() iteration are queued here
// instead of being pushed immediately to I²C. At the end of loop(),
// pca9685_flush() walks each board, coalesces contiguous dirty channels,
// and emits one burst I²C transaction per run.
//
// Why: on the R4 Minima, Wire.endTransmission() carries ~1 ms of peripheral
// teardown overhead. A 4-event chord transition = 4 transactions = ~4 ms
// stall that manifests as audible lag in fast passages. Coalesced bursts
// reduce that to 1–2 transactions by exploiting the PCA9685's auto-
// increment register mode (MODE1 AI bit, which Adafruit's setPWMFreq sets).

uint16_t pending_on[SOLENOID_COUNT];
uint16_t pending_off[SOLENOID_COUNT];
bool     pending_dirty[SOLENOID_COUNT];
bool     board_dirty[PCA9685_NUM_BOARDS];

static inline void pca9685_queue(uint8_t solenoid,
                                 uint16_t on_val, uint16_t off_val) {
    if (solenoid >= SOLENOID_COUNT) return;
    pending_on[solenoid]    = on_val;
    pending_off[solenoid]   = off_val;
    pending_dirty[solenoid] = true;
    board_dirty[solenoid / PCA9685_CHANNELS_PER] = true;
}

// Emit one I²C transaction that updates `n` consecutive channels starting
// at `first_ch` on `board`. Requires PCA9685 auto-increment mode (AI=1 in
// MODE1), which Adafruit_PWMServoDriver::setPWMFreq sets during init.
static void pca9685_burst_write(uint8_t board, uint8_t first_ch, uint8_t n,
                                const uint16_t* on_vals,
                                const uint16_t* off_vals) {
    Wire.beginTransmission((uint8_t)(PCA9685_BASE_ADDR + board));
    Wire.write(PCA9685_LED0_ON_L + 4 * first_ch);
    for (uint8_t i = 0; i < n; i++) {
        Wire.write((uint8_t)(on_vals[i]  & 0xFF));
        Wire.write((uint8_t)(on_vals[i]  >> 8));
        Wire.write((uint8_t)(off_vals[i] & 0xFF));
        Wire.write((uint8_t)(off_vals[i] >> 8));
    }
    Wire.endTransmission();
}

// Flush all pending PCA9685 writes, coalescing contiguous dirty channels
// on each board into single I²C transactions. Called once per loop().
void pca9685_flush() {
    for (uint8_t b = 0; b < PCA9685_NUM_BOARDS; b++) {
        if (!board_dirty[b]) continue;

        uint8_t base = b * PCA9685_CHANNELS_PER;
        uint8_t ch = 0;

        while (ch < PCA9685_CHANNELS_PER) {
            if (!pending_dirty[base + ch]) {
                ch++;
                continue;
            }
            // Contiguous dirty run starts here.
            uint8_t run_start = ch;
            while (ch < PCA9685_CHANNELS_PER && pending_dirty[base + ch]) {
                pending_dirty[base + ch] = false;
                ch++;
            }
            pca9685_burst_write(b, run_start, ch - run_start,
                                &pending_on [base + run_start],
                                &pending_off[base + run_start]);
        }
        board_dirty[b] = false;
    }
}

void pca9685_init_all() {
    for (uint8_t b = 0; b < PCA9685_NUM_BOARDS; b++) {
        pcaBoards[b].begin();
        pcaBoards[b].setPWMFreq(PCA9685_PWM_FREQ_HZ);
        // All channels off. Direct writes here are fine — runs once.
        for (uint8_t ch = 0; ch < PCA9685_CHANNELS_PER; ch++) {
            pcaBoards[b].setPWM(ch, 0, PWM_FULL_OFF);
        }
    }
}

// Public API — these now queue writes instead of issuing I²C immediately.
// Flush happens once per loop() after all of this tick's events have run.

void pca9685_set_duty(uint8_t solenoid, uint16_t duty_12bit) {
    if (solenoid >= SOLENOID_COUNT) return;
    if (duty_12bit >= 4095) {
        pca9685_queue(solenoid, PWM_ALWAYS_ON_BIT, 0);
    } else {
        pca9685_queue(solenoid, 0, duty_12bit);
    }
}

void pca9685_set_full_on(uint8_t solenoid) {
    pca9685_queue(solenoid, PWM_ALWAYS_ON_BIT, 0);
}

void pca9685_set_off(uint8_t solenoid) {
    pca9685_queue(solenoid, 0, PWM_FULL_OFF);
}


// =========================================================================
// Solenoid Controller — peak-and-hold + re-strike gap
// =========================================================================

enum SolenoidState : uint8_t {
    SOL_OFF = 0,
    SOL_STRIKING,
    SOL_HOLDING,
    SOL_GAP_WAIT
};

struct SolenoidChannel {
    SolenoidState state;
    uint8_t       velocity;
    uint32_t      strike_start_us;
    uint32_t      gap_end_us;
    uint8_t       pending_velocity;
};

SolenoidChannel channels[SOLENOID_COUNT];

uint16_t velocity_to_hold_duty(uint8_t velocity) {
    if (velocity == 0) return 0;
    uint32_t range = HOLD_DUTY_MAX - HOLD_DUTY_MIN;
    return HOLD_DUTY_MIN + (range * velocity) / 127;
}

void solenoid_init() {
    for (uint8_t i = 0; i < SOLENOID_COUNT; i++) {
        channels[i].state            = SOL_OFF;
        channels[i].velocity         = 0;
        channels[i].strike_start_us  = 0;
        channels[i].gap_end_us       = 0;
        channels[i].pending_velocity = 0;
    }
}

void begin_strike(uint8_t ch, uint8_t velocity, uint32_t now_us) {
    channels[ch].state           = SOL_STRIKING;
    channels[ch].velocity        = velocity;
    channels[ch].strike_start_us = now_us;
    pca9685_set_full_on(ch);
}

void solenoid_note_on(uint8_t ch, uint8_t velocity, uint32_t now_us) {
    if (ch >= SOLENOID_COUNT) return;

    digitalWrite(LED_PIN, HIGH);

    SolenoidChannel *s = &channels[ch];

    if (s->state == SOL_OFF) {
        begin_strike(ch, velocity, now_us);
    } else {
        // Re-strike gap
        pca9685_set_off(ch);
        s->state            = SOL_GAP_WAIT;
        s->gap_end_us       = now_us + RESTRIKE_GAP_US;
        s->pending_velocity = velocity;
    }
}

void solenoid_note_off(uint8_t ch) {
    if (ch >= SOLENOID_COUNT) return;

    digitalWrite(LED_PIN, LOW);

    channels[ch].state    = SOL_OFF;
    channels[ch].velocity = 0;
    pca9685_set_off(ch);
}

void solenoid_all_off() {
    for (uint8_t i = 0; i < SOLENOID_COUNT; i++) {
        channels[i].state    = SOL_OFF;
        channels[i].velocity = 0;
        pca9685_set_off(i);
    }
}

void solenoid_update(uint32_t now_us) {
    for (uint8_t i = 0; i < SOLENOID_COUNT; i++) {
        SolenoidChannel *s = &channels[i];

        switch (s->state) {
        case SOL_STRIKING:
            if ((now_us - s->strike_start_us) >= STRIKE_DURATION_US) {
                uint16_t hold_duty = velocity_to_hold_duty(s->velocity);
                pca9685_set_duty(i, hold_duty);
                s->state = SOL_HOLDING;
            }
            break;

        case SOL_GAP_WAIT:
            if (now_us >= s->gap_end_us) {
                begin_strike(i, s->pending_velocity, now_us);
            }
            break;

        case SOL_HOLDING:
        case SOL_OFF:
        default:
            break;
        }
    }
}


// =========================================================================
// MIDI Player — streaming ring buffer and timer-driven playback
// =========================================================================

struct StoredEvent {
    uint32_t timestamp_us;
    uint8_t  channel;
    uint8_t  event_type;  // 0 = off, 1 = on
    uint8_t  velocity;
    uint8_t  reserved;
};

// Ring buffer state. `ring_head` is the next event to play; `ring_tail`
// is where the next appended event will land. `ring_count` tracks fill
// level for trivial empty/full checks.
StoredEvent event_buffer[RING_CAPACITY];
uint16_t    ring_head     = 0;
uint16_t    ring_tail     = 0;
uint16_t    ring_count    = 0;

bool        playing       = false;
bool        stream_ended  = false;   // set when PC sends CMD_EOS
uint32_t    play_start_us = 0;

static inline uint16_t ring_free() {
    return (uint16_t)(RING_CAPACITY - ring_count);
}

bool midi_player_add_event(uint32_t timestamp_us, uint8_t channel,
                           uint8_t event_type, uint8_t velocity) {
    if (ring_count >= RING_CAPACITY) return false;   // buffer full

    StoredEvent *ev = &event_buffer[ring_tail];
    ev->timestamp_us = timestamp_us;
    ev->channel      = channel;
    ev->event_type   = event_type;
    ev->velocity     = velocity;
    ev->reserved     = 0;

    ring_tail = (uint16_t)((ring_tail + 1) % RING_CAPACITY);
    ring_count++;
    return true;
}

void midi_player_start() {
    // Streaming mode: starting with an empty ring is fine — playback will
    // kick in as soon as events arrive. We do not gate on buffer contents.
    play_start_us = micros();
    playing       = true;
    stream_ended  = false;
}

void midi_player_stop() {
    playing       = false;
    stream_ended  = false;
    ring_head     = 0;
    ring_tail     = 0;
    ring_count    = 0;
    play_start_us = 0;
    solenoid_all_off();
}

void midi_player_update(uint32_t now_us) {
    if (!playing) return;

    uint32_t elapsed = now_us - play_start_us;

    while (ring_count > 0) {
        StoredEvent *ev = &event_buffer[ring_head];
        if (ev->timestamp_us > elapsed) break;

        if (ev->event_type == 1) {
            solenoid_note_on(ev->channel, ev->velocity, now_us);
        } else {
            solenoid_note_off(ev->channel);
        }

        ring_head = (uint16_t)((ring_head + 1) % RING_CAPACITY);
        ring_count--;
    }

    // Only end playback when the PC has signaled end-of-stream AND we've
    // drained every event. A transiently empty ring during streaming just
    // means the PC is temporarily behind — do NOT stop.
    if (stream_ended && ring_count == 0) {
        playing = false;
    }
}


// =========================================================================
// Serial Protocol — packet parser
// =========================================================================

enum ParseState : uint8_t {
    PS_WAIT_HEADER,
    PS_READ_PAYLOAD,
    PS_WAIT_CHECKSUM,
    PS_WAIT_FOOTER
};

#define MAX_PAYLOAD 520

ParseState  parse_state = PS_WAIT_HEADER;
uint8_t     payload_buf[MAX_PAYLOAD];
uint16_t    payload_idx = 0;
uint16_t    expected_len = 0;
uint8_t     current_cmd = 0;
uint8_t     running_checksum = 0;

// ACK now carries the current free-slot count so the PC can pace its
// uploads. Frame layout:
//   [HEADER] [CMD_ACK] [free_lo] [free_hi] [checksum] [FOOTER]
// Checksum is XOR of the three payload bytes (CMD_ACK, free_lo, free_hi).
void send_ack() {
    uint16_t free_slots = ring_free();
    uint8_t  lo  = (uint8_t)(free_slots & 0xFF);
    uint8_t  hi  = (uint8_t)(free_slots >> 8);
    uint8_t  chk = (uint8_t)(CMD_ACK ^ lo ^ hi);
    uint8_t  pkt[6] = { PACKET_HEADER, CMD_ACK, lo, hi, chk, PACKET_FOOTER };
    Serial.write(pkt, 6);
}

void process_event_batch(const uint8_t *data, uint16_t len) {
    if (len < 3) return;

    uint16_t count = (uint16_t)data[1] | ((uint16_t)data[2] << 8);
    uint16_t offset = 3;

    for (uint16_t i = 0; i < count; i++) {
        if ((offset + 8) > len) break;

        uint32_t timestamp_us = (uint32_t)data[offset]
                              | ((uint32_t)data[offset+1] << 8)
                              | ((uint32_t)data[offset+2] << 16)
                              | ((uint32_t)data[offset+3] << 24);

        uint8_t channel    = data[offset+4];
        uint8_t event_type = data[offset+5];
        uint8_t velocity   = data[offset+6];

        midi_player_add_event(timestamp_us, channel, event_type, velocity);

        offset += 8;
    }

    send_ack();
}

void process_packet(const uint8_t *data, uint16_t len) {
    if (len == 0) return;

    uint8_t cmd = data[0];

    switch (cmd) {
    case CMD_EVENT_BATCH:
        process_event_batch(data, len);
        break;
    case CMD_START:
        midi_player_start();
        send_ack();
        break;
    case CMD_STOP:
        midi_player_stop();
        send_ack();
        break;
    case CMD_PING:
        send_ack();
        break;
    case CMD_EOS:
        stream_ended = true;
        send_ack();
        break;
    }
}

void serial_protocol_feed(uint8_t byte) {
    switch (parse_state) {
    case PS_WAIT_HEADER:
        if (byte == PACKET_HEADER) {
            payload_idx = 0;
            running_checksum = 0;
            parse_state = PS_READ_PAYLOAD;
        }
        break;

    case PS_READ_PAYLOAD:
        if (payload_idx == 0) {
            current_cmd = byte;
            if (byte == CMD_START || byte == CMD_STOP ||
                byte == CMD_PING  || byte == CMD_EOS) {
                expected_len = 1;
            } else {
                expected_len = 0;
            }
        } else if (payload_idx == 2 && current_cmd == CMD_EVENT_BATCH) {
            uint16_t count = (uint16_t)payload_buf[1]
                           | ((uint16_t)byte << 8);
            expected_len = 3 + (count * 8);
        }

        if (payload_idx < MAX_PAYLOAD) {
            payload_buf[payload_idx] = byte;
            running_checksum ^= byte;
        }
        payload_idx++;

        if (expected_len > 0 && payload_idx >= expected_len) {
            parse_state = PS_WAIT_CHECKSUM;
        }
        break;

    case PS_WAIT_CHECKSUM:
        if (byte == running_checksum) {
            parse_state = PS_WAIT_FOOTER;
        } else {
            parse_state = PS_WAIT_HEADER;
        }
        break;

    case PS_WAIT_FOOTER:
        if (byte == PACKET_FOOTER) {
            process_packet(payload_buf, payload_idx);
        }
        parse_state = PS_WAIT_HEADER;
        break;

    default:
        parse_state = PS_WAIT_HEADER;
        break;
    }
}


// =========================================================================
// Arduino setup() and loop()
// =========================================================================

void setup() {
    // USB Serial
    Serial.begin(115200);
    delay(1000);

    // I2C
    Wire.begin();
    Wire.setClock(400000);

    // LED
    pinMode(LED_PIN, OUTPUT);
    digitalWrite(LED_PIN, LOW);

    // Wait for PCA9685 boards to power up
    delay(500);

    // Initialize all PCA9685 boards (also sets MODE1 auto-increment bit,
    // which the burst-write path depends on).
    pca9685_init_all();

    // Initialize solenoid state machine
    solenoid_init();

    // Startup blink — confirms init complete
    for (int i = 0; i < 3; i++) {
        digitalWrite(LED_PIN, HIGH);
        delay(150);
        digitalWrite(LED_PIN, LOW);
        delay(150);
    }
}

void loop() {
    // 1. Playback FIRST so it never gets gated on parsing
    uint32_t now = micros();
    midi_player_update(now);
    solenoid_update(now);
    pca9685_flush();

    // 2. Bounded serial work — don't drain the whole buffer in one shot
    int budget = 48;
    while (Serial.available() && budget-- > 0) {
        serial_protocol_feed(Serial.read());
    }
}