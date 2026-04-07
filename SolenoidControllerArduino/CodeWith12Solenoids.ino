/*
  Simple "song string" parser + PCA9685 control for 12 notes (C..B)

  Protocol (one line over Serial, end with '.' to stop):
    - ','  = next event (sequential)
    - ';'  = notes in the same chord (simultaneous)
    - ':'  = duration in ms for a note OR chord
            If you put duration on any note in a chord, we use the MAX duration
            found in that chord as the chord duration.
    - '.'  = end of song / stop (can appear at end of line)


    Test 1, going through scale: C:1000,D:1000,E:1000,F:1000,G:1000,A:1000,B:1000,A#:1000.
    Test 2, mary had a little lamb: E:500,D:500,C:500,D:500,E:500,E:500,E:1000,D:500,D:500,D:1000,E:500,G:500,G:1000,E:500,D:500,C:500,D:500,E:500,E:500,E:500,E:500,D:500,D:500,E:500,D:500,C:1000.
    Test 3: mary real thing: E:250,D:250,C:250,D:250,E:250,E:250,E:500,D:250,D:250,D:500,E:250,G:250,G:500,E:250,D:250,C:250,D:250,E:250,E:250,E:250,E:250,D:250,D:250,E:250,D:250,C:500.


  Examples:
    C,D,E.
    A;A#,B.
    A:1000;A#:1000,B:500.
    C:250,D:250,E:500,F:1000.

  Notes supported: C, C#, D, D#, E, F, F#, G, G#, A, A#, B
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

// Default duration if none provided
const uint16_t DEFAULT_DUR_MS = 500;

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

// --------- Helpers: string parsing ----------
static inline bool isSpace(char c) { return c == ' ' || c == '\t' || c == '\r'; }

void trimInPlace(String &s) {
  while (s.length() > 0 && isSpace(s[0])) s.remove(0, 1);
  while (s.length() > 0 && isSpace(s[s.length() - 1])) s.remove(s.length() - 1, 1);
}

bool splitOnce(const String &s, char delim, String &left, String &right) {
  int idx = s.indexOf(delim);
  if (idx < 0) return false;
  left = s.substring(0, idx);
  right = s.substring(idx + 1);
  return true;
}

// Map note name -> index 0..11 in noteChannels
// Accepts: "C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"
int noteNameToIndex(String name) {
  trimInPlace(name);
  name.toUpperCase();

  if (name == "C")  return 0;
  if (name == "C#") return 1;
  if (name == "D")  return 2;
  if (name == "D#") return 3;
  if (name == "E")  return 4;
  if (name == "F")  return 5;
  if (name == "F#") return 6;
  if (name == "G")  return 7;
  if (name == "G#") return 8;
  if (name == "A")  return 9;
  if (name == "A#") return 10;
  if (name == "B")  return 11;

  return -1;
}

// Parse "NOTE[:duration]" into noteIndex and duration (optional).
// Returns true if note is valid.
bool parseNoteToken(String tok, int &noteIndex, uint16_t &durMs, bool &hasDur) {
  trimInPlace(tok);

  // Remove trailing '.' if present (end marker)
  if (tok.endsWith(".")) tok.remove(tok.length() - 1);

  String left, right;
  if (splitOnce(tok, ':', left, right)) {
    trimInPlace(left);
    trimInPlace(right);

    int idx = noteNameToIndex(left);
    if (idx < 0) return false;

    long d = right.toInt(); // returns 0 if not a number
    if (d <= 0) return false;

    noteIndex = idx;
    durMs = (uint16_t)d;
    hasDur = true;
    return true;
  } else {
    int idx = noteNameToIndex(tok);
    if (idx < 0) return false;
    noteIndex = idx;
    durMs = DEFAULT_DUR_MS;
    hasDur = false;
    return true;
  }
}

// Play one "event" (a chord): "A;A#;B" possibly with per-note durations.
// Strategy: turn all notes on, wait chordDur, turn them off.
// chordDur = max(duration among notes that have durations), else DEFAULT_DUR_MS.
void playChordEvent(String eventStr) {
  trimInPlace(eventStr);
  if (eventStr.length() == 0) return;

  // If someone sends just "." -> stop
  if (eventStr == ".") return;

  // Parse chord notes separated by ';'
  // We'll collect up to 12 note indices (avoid duplicates)
  bool active[12] = {false};

  uint16_t chordDur = DEFAULT_DUR_MS;
  bool anyDur = false;

  int start = 0;
  while (start < eventStr.length()) {
    int sep = eventStr.indexOf(';', start);
    String tok = (sep < 0) ? eventStr.substring(start) : eventStr.substring(start, sep);
    start = (sep < 0) ? eventStr.length() : (sep + 1);

    int idx;
    uint16_t dur;
    bool hasDur;
    if (!parseNoteToken(tok, idx, dur, hasDur)) {
      // Ignore invalid tokens but you could print an error
      continue;
    }

    active[idx] = true;
    if (hasDur) {
      anyDur = true;
      if (dur > chordDur) chordDur = dur;
    }
  }

  // If no valid notes, do nothing
  bool anyNote = false;
  for (int i = 0; i < 12; i++) if (active[i]) { anyNote = true; break; }
  if (!anyNote) return;

  // Turn on notes
  for (int i = 0; i < 12; i++) if (active[i]) noteOn(i);

  delay(chordDur);

  // Turn off notes
  for (int i = 0; i < 12; i++) if (active[i]) noteOff(i);
  delay(60);
}

// Play a whole song line: events separated by ',' ending with optional '.'
void playSongLine(String line) {
  trimInPlace(line);
  if (line.length() == 0) return;

  // If the user didn't include '.', we'll still play what they sent
  // but '.' is used as "stop" marker if present.
  int start = 0;
  while (start < line.length()) {
    int sep = line.indexOf(',', start);
    String eventStr = (sep < 0) ? line.substring(start) : line.substring(start, sep);
    start = (sep < 0) ? line.length() : (sep + 1);

    trimInPlace(eventStr);

    // If this chunk contains '.', stop after playing what's before it.
    int dot = eventStr.indexOf('.');
    if (dot >= 0) {
      String beforeDot = eventStr.substring(0, dot);
      trimInPlace(beforeDot);
      if (beforeDot.length() > 0) {
        playChordEvent(beforeDot);
      }
      break; // end of song
    } else {
      playChordEvent(eventStr);
    }
  }

  allNotesOff();
}

void setup() {
  Serial.begin(9600);

  // UNO: SDA=A4, SCL=A5 (fixed). ESP32 would use Wire.begin(SDA,SCL).
  Wire.begin();

  pca.begin();
  pca.setPWMFreq(1000); // OK for LEDs/logic-level signals
  allNotesOff();

  Serial.println("Ready. Send a song line ending with '.'");
  Serial.println("Examples: C,D,E.   A;A#,B.   A:1000;A#:1000,B:500.");
}

void loop() {
  if (Serial.available() > 0) {
    String line = Serial.readStringUntil('\n');
    line.trim();

    if (line.length() == 0) return;

    // Optional: allow sending just "." to stop/clear
    if (line == ".") {
      allNotesOff();
      Serial.println("Stopped.");
      return;
    }

    Serial.print("Playing: ");
    Serial.println(line);

    playSongLine(line);

    Serial.println("Done.");
  }
}