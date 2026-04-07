"""Build a desktop-timed playback schedule for the microcontroller.

The desktop converts MIDI into a timestamped list of serial commands. The
microcontroller only receives `ON <channel>` and `OFF <channel>` commands and
does not perform music parsing or timing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Literal, Optional

import mido


NOTE_TO_CHANNEL: Dict[int, int] = {
    60: 0,   # C4
    61: 1,   # C#4
    62: 2,   # D4
    63: 3,   # D#4
    64: 4,   # E4
    65: 5,   # F4
    66: 6,   # F#4
    67: 7,   # G4
    68: 8,   # G#4
    69: 9,   # A4
    70: 10,  # A#4
    71: 11,  # B4
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


@dataclass
class SerialCommandEvent:
    time_ms: int
    command: str

    def to_dict(self) -> Dict[str, int | str]:
        return {
            "time_ms": self.time_ms,
            "command": self.command,
        }


def _extract_tempo_us_per_beat(mid: mido.MidiFile) -> int:
    """Return the first tempo (microseconds per beat) or 500,000 by default."""

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


def extract_note_events(path: str | Path) -> List[SolenoidEvent]:
    """Extract note on/off events with correct tempo-change timing.

    This function honors tempo changes across the song by integrating each
    message delta with the current tempo.
    """

    midi_path = Path(path)
    if not midi_path.is_file():
        raise FileNotFoundError(midi_path)

    mid = mido.MidiFile(midi_path)
    ticks_per_beat = mid.ticks_per_beat

    current_time_sec = 0.0
    tempo_us_per_beat = _extract_tempo_us_per_beat(mid)
    events: List[tuple[int, SolenoidEvent]] = []
    seq = 0

    for msg in mido.merge_tracks(mid.tracks):
        # Delta time uses the tempo active before this message.
        current_time_sec += mido.tick2second(msg.time, ticks_per_beat, tempo_us_per_beat)
        time_ms = int(round(current_time_sec * 1000.0))

        if msg.type == "set_tempo":
            tempo_us_per_beat = msg.tempo
            continue

        if msg.type == "note_on" and msg.velocity > 0:
            events.append(
                (
                    seq,
                    SolenoidEvent(
                        time_ms=time_ms,
                        note=msg.note,
                        velocity=msg.velocity,
                        type="on",
                    ),
                )
            )
            seq += 1
        elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
            events.append(
                (
                    seq,
                    SolenoidEvent(
                        time_ms=time_ms,
                        note=msg.note,
                        velocity=0,
                        type="off",
                    ),
                )
            )
            seq += 1

    events.sort(key=lambda pair: (pair[1].time_ms, pair[0]))
    return [event for _, event in events]


def build_serial_schedule(
    note_events: List[SolenoidEvent],
    note_to_channel: Optional[Dict[int, int]] = None,
) -> List[SerialCommandEvent]:
    """Convert note events into serial command events.

    Supported command format for the Arduino firmware:
      ON <channel>
      OFF <channel>
    """

    mapping = note_to_channel if note_to_channel is not None else NOTE_TO_CHANNEL

    schedule: List[SerialCommandEvent] = []
    for event in note_events:
        channel = mapping.get(event.note)
        if channel is None:
            continue

        if event.type == "on":
            cmd = f"ON {channel}"
        else:
            cmd = f"OFF {channel}"

        schedule.append(SerialCommandEvent(time_ms=event.time_ms, command=cmd))

    return schedule


def midi_to_playback_package(
    path: str | Path,
    note_to_channel: Optional[Dict[int, int]] = None,
) -> Dict[str, object]:
    """Build a desktop playback package with timestamped serial commands."""

    midi_path = Path(path)
    if not midi_path.is_file():
        raise FileNotFoundError(midi_path)

    mid = mido.MidiFile(midi_path)
    mapping = note_to_channel if note_to_channel is not None else NOTE_TO_CHANNEL
    note_events = extract_note_events(midi_path)
    serial_schedule = build_serial_schedule(note_events, mapping)

    return {
        "ticks_per_beat": mid.ticks_per_beat,
        "events": [event.to_dict() for event in note_events],
        "note_to_channel": {str(note): channel for note, channel in mapping.items()},
        "serial_schedule": [entry.to_dict() for entry in serial_schedule],
        "protocol": {
            "type": "line_serial_v1",
            "commands": ["ON <channel>", "OFF <channel>", "ALL_OFF"],
        },
    }


def midi_to_solenoid_events(path: str | Path) -> Dict[str, object]:
    """Backward-compatible wrapper for legacy callers.

    Kept for existing imports in the UI code.
    """

    return midi_to_playback_package(path)


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) != 2:
        print("Usage: python midi_to_solenoid.py <file.mid>")
        raise SystemExit(1)

    data = midi_to_playback_package(sys.argv[1])
    print(json.dumps(data, indent=2))
