/*
  Serial command executor for PCA9685 channels.

  Desktop-side software handles all MIDI parsing and timing. This firmware
  only receives line commands:

    ON <channel>
    OFF <channel>
    ALL_OFF
    PING

  Example:
    ON 4
    OFF 4
    ALL_OFF
*/

#include <Wire.h>
#include <Adafruit_PWMServoDriver.h>

Adafruit_PWMServoDriver pca(0x40);

// 12-bit PWM values for PCA9685
const uint16_t FULL_ON  = 4095;
const uint16_t FULL_OFF = 0;

// Your 12 note channels (C..B)
const uint8_t noteChannels[12] = {
  0,  // C
  1,  // C#
  2,  // D
  3,  // D#
  4,  // E
  5,  // F
  6,  // F#
  7,  // G
  8,  // G#
  9,  // A
  10, // A#
  11  // B
};

// --------- Helpers: PCA9685 control ----------
void allNotesOff() {
  for (int i = 0; i < 12; i++) {
    pca.setPWM(noteChannels[i], 0, FULL_OFF);
  }
}

void noteOn(uint8_t noteIndex) {
  pca.setPWM(noteChannels[noteIndex], 0, FULL_ON);
}

void noteOff(uint8_t noteIndex) {
  pca.setPWM(noteChannels[noteIndex], 0, FULL_OFF);
}

bool parseChannelArg(const String &line, int cmdLen, uint8_t &channel) {
  if (line.length() <= cmdLen) return false;
  String arg = line.substring(cmdLen);
  arg.trim();
  long parsed = arg.toInt();
  if (parsed < 0 || parsed > 11) return false;
  channel = (uint8_t)parsed;
  return true;
}

void executeCommand(String line) {
  line.trim();
  if (line.length() == 0) return;

  line.toUpperCase();

  if (line == "ALL_OFF") {
    allNotesOff();
    return;
  }

  if (line == "PING") {
    Serial.println("PONG");
    return;
  }

  if (line.startsWith("ON ")) {
    uint8_t channel;
    if (parseChannelArg(line, 3, channel)) {
      noteOn(channel);
    }
    return;
  }

  if (line.startsWith("OFF ")) {
    uint8_t channel;
    if (parseChannelArg(line, 4, channel)) {
      noteOff(channel);
    }
    return;
  }
}

void setup() {
  Serial.begin(115200);

  // UNO: SDA=A4, SCL=A5 (fixed). ESP32 would use Wire.begin(SDA,SCL).
  Wire.begin();

  pca.begin();
  pca.setPWMFreq(1000); // OK for LEDs/logic-level signals
  allNotesOff();

  Serial.println("Ready. Commands: ON <0-11>, OFF <0-11>, ALL_OFF, PING");
}

void loop() {
  if (Serial.available() > 0) {
    String line = Serial.readStringUntil('\n');
    line.trim();

    if (line.length() == 0) return;

    executeCommand(line);
  }
}
