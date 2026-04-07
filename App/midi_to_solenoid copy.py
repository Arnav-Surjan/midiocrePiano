"""Convert MIDI files into a solenoid event stream for an ESP32 + PCA9685 setup.

This module focuses purely on *formatting* the MIDI into simple, time-ordered
note-on/note-off events plus an explicit mapping from MIDI notes to solenoid
channels. Your ESP32 firmware can then consume this data (e.g. as JSON) and
turn it into GPIO/PCA9685 actions.

High-level design
-----------------
- Input: a .mid file path.
- Output: a dict that you can JSON-serialize, with:

  {
      "ticks_per_beat": int,
      "tempo_us_per_beat": int,  # default 500000 if no tempo
      "events": [
          {"time_ms": int, "note": int, "velocity": int, "type": "on"|"off"},
          ...
      ],
      "note_to_channel": {"60": 0, "61": 1, ...}
  }

- Timing: we convert MIDI delta ticks -> absolute milliseconds using the first
  tempo we encounter (ignoring later tempo changes for now to keep the
  playback logic on the ESP32 simple).
- Notes: we collect note_on (velocity>0) and note_off (or note_on with
  velocity=0) pairs into on/off events.

You can change NOTE_TO_CHANNEL below to match your wiring.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Literal

import mido


# Example mapping from MIDI note numbers to solenoid channels.
# Adjust this to match your physical wiring.
# For example, map a small range of keys:
#   60 = middle C, 61 = C#, 62 = D, ...
NOTE_TO_CHANNEL: Dict[int, int] = {
    # 60: 0,
    # 61: 1,
    # 62: 2,
}


EventType = Literal["on", "off"]


@dataclass
class SolenoidEvent:
    time_ms: int  # absolute time from song start
    note: int     # MIDI note number
    velocity: int
    type: EventType  # "on" or "off"

    def to_dict(self) -> Dict[str, int | str]:
        return {
            "time_ms": self.time_ms,
            "note": self.note,
            "velocity": self.velocity,
            "type": self.type,
        }


def _extract_tempo_us_per_beat(mid: mido.MidiFile) -> int:
    """Return the first tempo (microseconds per beat) or 500,000 by default.

    Many files contain only a single tempo at the beginning, which keeps
    playback logic on the microcontroller much simpler.
    """

    for track in mid.tracks:
        for msg in track:
            if msg.type == "set_tempo":
                return msg.tempo
    return 500_000  # default 120 BPM


def _ticks_to_ms(ticks: int, ticks_per_beat: int, tempo_us_per_beat: int) -> int:
    """Convert MIDI ticks to integer milliseconds."""

    # 1 beat = tempo_us_per_beat microseconds
    # ticks_per_beat ticks = 1 beat
    # So 1 tick = tempo_us_per_beat / ticks_per_beat microseconds
    us = (ticks * tempo_us_per_beat) / ticks_per_beat
    return int(us / 1000)


def midi_to_solenoid_events(path: str | Path) -> Dict[str, object]:
    """Parse a MIDI file and return a solenoid-friendly event stream.

    The resulting dict is designed to be JSON-serializable directly.
    """

    midi_path = Path(path)
    if not midi_path.is_file():
        raise FileNotFoundError(midi_path)

    mid = mido.MidiFile(midi_path)

    ticks_per_beat = mid.ticks_per_beat
    tempo_us_per_beat = _extract_tempo_us_per_beat(mid)

    # For simplicity, merge all tracks into a single time-ordered stream.
    # mido.MidiFile.play() yields messages in real-time order with .time as
    # seconds; instead we manually track ticks so we can use our own tempo.
    current_ticks = 0
    events: List[SolenoidEvent] = []

    # Track note-on messages that haven't been turned off yet so we can
    # always emit a matching off event.
    active_notes: Dict[int, int] = {}

    for msg in mido.merge_tracks(mid.tracks):
        current_ticks += msg.time

        if msg.type == "set_tempo":
            # For now we ignore tempo changes after the first one; you could
            # extend this in the future to support variable tempo.
            continue

        if msg.type == "note_on" and msg.velocity > 0:
            active_notes[msg.note] = current_ticks
            time_ms = _ticks_to_ms(current_ticks, ticks_per_beat, tempo_us_per_beat)
            events.append(SolenoidEvent(time_ms=time_ms, note=msg.note, velocity=msg.velocity, type="on"))
        elif msg.type in {"note_off", "note_on"}:
            # note_off or note_on with velocity 0
            if msg.type == "note_on" and msg.velocity != 0:
                # handled above
                continue
            start_ticks = active_notes.pop(msg.note, None)
            if start_ticks is None:
                # We never saw a note_on for this note; still emit an off
                # just in case.
                time_ms = _ticks_to_ms(current_ticks, ticks_per_beat, tempo_us_per_beat)
            else:
                time_ms = _ticks_to_ms(current_ticks, ticks_per_beat, tempo_us_per_beat)

            events.append(SolenoidEvent(time_ms=time_ms, note=msg.note, velocity=0, type="off"))

    # Sort events just in case; they should already be ordered.
    events.sort(key=lambda e: e.time_ms)

    return {
        "ticks_per_beat": ticks_per_beat,
        "tempo_us_per_beat": tempo_us_per_beat,
        "events": [e.to_dict() for e in events],
        "note_to_channel": {str(note): channel for note, channel in NOTE_TO_CHANNEL.items()},
    }


if __name__ == "__main__":  # simple manual test helper
    import json
    import sys

    if len(sys.argv) != 2:
        print("Usage: python midi_to_solenoid.py <file.mid>")
        raise SystemExit(1)

    data = midi_to_solenoid_events(sys.argv[1])
    print(json.dumps(data, indent=2))
