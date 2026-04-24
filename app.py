"""
Solenoid Piano Controller
==========================
Desktop GUI for parsing MIDI files and transmitting to
the Arduino UNO R4 Minima solenoid piano controller.

STREAMING MODE: Events are fed to the Arduino's ring buffer continuously
during playback, so song length is no longer capped by MCU RAM. The
Arduino's ACK carries its current free-slot count, which this uploader
uses to throttle and avoid overruns. When all events have been sent,
a CMD_EOS tells the firmware it can end playback once the ring drains.

Dependencies:
    pip install mido python-rtmidi pyserial customtkinter

Launch:
    python solenoid_piano_app.py
"""

import customtkinter as ctk
import threading
import struct
import time
import sys
import os
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from tkinter import filedialog, messagebox

import mido
import serial
import serial.tools.list_ports


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIDI_NOTE_LOW  = 21
MIDI_NOTE_HIGH = 108
NUM_KEYS = 74  

PACKET_HEADER   = 0xAA
PACKET_FOOTER   = 0x55
CMD_EVENT_BATCH = 0x01
CMD_START       = 0x02
CMD_STOP        = 0x03
CMD_PING        = 0x04
CMD_EOS         = 0x05     # "no more events coming"
CMD_ACK         = 0x10

BATCH_SIZE = 64

# Must match RING_CAPACITY in the firmware. Used only as an initial
# optimistic estimate before the first ACK arrives with the real value.
RING_CAPACITY = 1024

# How many batches to prime the ring with before issuing CMD_START.
# Two full batches (~128 events) gives the playback engine a comfortable
# head start before serial throughput has to keep up with real time.
PRIME_BATCHES = 2

# Timing-cleanup constants
RESTRIKE_WINDOW_US  = 100_000   # 100 ms  – merge fast OFF→ON restrikes
DUPLICATE_ON_US     = 210_000   # 210 ms  – remove duplicate ONs per channel
MIN_GAP_US          = 15_000   # 100 ms  – minimum time between event groups
CHORD_WINDOW_US     = 1_000     # 2 ms    – events within this window = chord

# Serial-transport timing
INTER_BATCH_DELAY_S = 0.005     # small breather between batches
POST_OPEN_SETTLE_S  = 2.0       # UNO R4 resets on port open
ACK_TIMEOUT_S       = 2.0
BACKPRESSURE_POLL_S = 0.010     # wait between PINGs when ring is full

NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F',
              'F#', 'G', 'G#', 'A', 'A#', 'B']

ACCENT       = "#BF5700"
ACCENT_HOVER = "#C48654"
SECTION_BG = "#FFE6C8"   
WINDOW_BG = "#FFF7EE"

class EventType(IntEnum):
    NOTE_ON  = 1
    NOTE_OFF = 0


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class SolenoidEvent:
    timestamp_us: int
    channel: int
    event_type: EventType
    velocity: int

    def pack(self) -> bytes:
        return struct.pack('<IBBBx',
                           self.timestamp_us,
                           self.channel,
                           int(self.event_type),
                           self.velocity)

    @staticmethod
    def packed_size() -> int:
        return 8

    def format_row(self) -> tuple[str, str, str, str, str]:
        t_sec  = f"{self.timestamp_us / 1_000_000:.4f}s"
        ch     = str(self.channel)
        note   = midi_note_to_name(self.channel + MIDI_NOTE_LOW)
        etype  = "ON" if self.event_type == EventType.NOTE_ON else "OFF"
        vel    = str(self.velocity)
        return (t_sec, ch, note, etype, vel)


@dataclass
class SongData:
    filename: str
    events: list[SolenoidEvent] = field(default_factory=list)
    duration_us: int = 0
    tempo_bpm: float = 120.0

    @property
    def num_events(self) -> int:
        return len(self.events)

    @property
    def duration_sec(self) -> float:
        return self.duration_us / 1_000_000


# ---------------------------------------------------------------------------
# MIDI helpers
# ---------------------------------------------------------------------------

def midi_note_to_name(note: int) -> str:
    octave = (note // 12) - 1
    name   = NOTE_NAMES[note % 12]
    return f"{name}{octave}"


def midi_note_to_channel(note: int) -> int | None:
    if MIDI_NOTE_LOW <= note <= MIDI_NOTE_HIGH:
        return note - MIDI_NOTE_LOW
    return None


def parse_composer_from_filename(filepath: Path) -> str:
    stem = filepath.stem
    if "-" not in stem:
        return ""
    composer = stem.rsplit("-", 1)[1].strip()
    return composer


# ---------------------------------------------------------------------------
# Timing / event-cleanup pipeline
# ---------------------------------------------------------------------------

def merge_fast_restrikes(
    events: list[SolenoidEvent],
    restrike_window_us: int = RESTRIKE_WINDOW_US,
    firmware_restrike_delay_us: int = 110_000,
    min_on_gap_us: int = 100_000,
) -> list[SolenoidEvent]:
    """
    If a NOTE_OFF is followed by a NOTE_ON on the same channel within
    restrike_window_us, remove the OFF and shift that next ON earlier by
    firmware_restrike_delay_us.

    Then re-check ON-to-ON spacing on the same channel:
      - if shifted ON is at least min_on_gap_us after previous ON, keep it
      - otherwise delete the shifted ON
    """

    merged: list[SolenoidEvent] = []
    n = len(events)
    skip_indices: set[int] = set()

    for i, ev in enumerate(events):
        if i in skip_indices:
            continue

        if ev.event_type != EventType.NOTE_OFF:
            merged.append(ev)
            continue

        remove_off = False

        for j in range(i + 1, n):
            nxt = events[j]

            if nxt.channel != ev.channel:
                continue

            gap = nxt.timestamp_us - ev.timestamp_us

            if gap > restrike_window_us:
                break

            if nxt.event_type == EventType.NOTE_ON:
                new_time = max(0, nxt.timestamp_us - firmware_restrike_delay_us)

                shifted_on = SolenoidEvent(
                    timestamp_us=new_time,
                    channel=nxt.channel,
                    event_type=nxt.event_type,
                    velocity=nxt.velocity,
                )

                merged.append(shifted_on)
                skip_indices.add(j)
                remove_off = True

            break

        if not remove_off:
            merged.append(ev)

    # Re-sort because shifting ONs backward can put events out of order
    merged.sort(key=lambda e: e.timestamp_us)

    # Remove ONs that are too close to the previous ON on the same channel
    final: list[SolenoidEvent] = []
    last_on_time_by_channel: dict[int, int] = {}

    for ev in merged:
        if ev.event_type == EventType.NOTE_ON:
            last_on = last_on_time_by_channel.get(ev.channel)

            if last_on is not None:
                gap = ev.timestamp_us - last_on
                if gap < min_on_gap_us:
                    continue

            last_on_time_by_channel[ev.channel] = ev.timestamp_us

        final.append(ev)

    return final


def remove_fast_duplicate_ons(events: list[SolenoidEvent],
                               window_us: int = DUPLICATE_ON_US
                               ) -> list[SolenoidEvent]:
    """
    Drop a NOTE_ON if the previous NOTE_ON for the same channel occurred
    within window_us (avoids hammering the solenoid faster than it can reset).
    """
    result: list[SolenoidEvent] = []
    last_on_time: dict[int, int] = {}

    for ev in events:
        if ev.event_type == EventType.NOTE_ON:
            last_time = last_on_time.get(ev.channel)
            if last_time is not None and (ev.timestamp_us - last_time) < window_us:
                continue                     # skip – too soon
            last_on_time[ev.channel] = ev.timestamp_us
            result.append(ev)
        else:
            result.append(ev)               # always keep NOTE_OFF

    return result


def enforce_min_gap_chord_aware(events: list[SolenoidEvent],
                                min_gap_us: int = MIN_GAP_US,
                                chord_window_us: int = CHORD_WINDOW_US
                                ) -> list[SolenoidEvent]:
    """
    Groups events within chord_window_us of each other into a single
    chord group, then enforces min_gap_us BETWEEN groups only.

    Fixes the bug in enforce_min_gap_global:
      Example: C1@0.00s, C2@0.05s, C3@0.06s with 100 ms gap.
      - Old (per-channel global shift): C2 gets shifted to 0.11s, but C3
        is still at 0.06s so it plays BEFORE C2 — the shift reordered
        the song.
      - This version: operates on GROUPS, so the relative ordering of
        notes in a "cluster" is preserved and chords stay simultaneous.

    All events inside a group receive the same (possibly-adjusted)
    timestamp, and a group's timestamp is never moved earlier than its
    original time.
    """
    if not events:
        return []

    # --- 1. Build chord groups -------------------------------------------
    # A new group starts whenever the gap from the first event of the current
    # group exceeds chord_window_us.
    groups: list[list[SolenoidEvent]] = [[events[0]]]
    for ev in events[1:]:
        if ev.timestamp_us - groups[-1][0].timestamp_us <= chord_window_us:
            groups[-1].append(ev)
        else:
            groups.append([ev])

    # --- 2. Assign timestamps, enforcing min gap between groups -----------
    result: list[SolenoidEvent] = []
    scheduled_time: int = 0

    for idx, g in enumerate(groups):
        original_time = g[0].timestamp_us

        if idx == 0:
            # First group: never shift earlier, no gap requirement yet.
            t = original_time
        else:
            # Must be at least min_gap_us after the previous group AND
            # no earlier than where the MIDI file placed it.
            t = max(original_time, scheduled_time + min_gap_us)

        for ev in g:
            result.append(SolenoidEvent(
                timestamp_us=t,
                channel=ev.channel,
                event_type=ev.event_type,
                velocity=ev.velocity,
            ))

        scheduled_time = t

    return result

def enforce_min_gap_per_channel(events: list[SolenoidEvent],
                                min_gap_us: int = MIN_GAP_US,
                                chord_window_us: int = CHORD_WINDOW_US
                                ) -> list[SolenoidEvent]:
    """
    Per-channel minimum gap. A group's timestamp is only pushed forward if
    one of ITS channels had a recent NOTE_ON that's too close. Unrelated
    channels never slow each other down, so fast melodies across different
    solenoids play at full MIDI speed.

    Chord grouping is preserved: events within chord_window_us get a shared
    timestamp and move as a unit.
    """
    if not events:
        return []

    # --- 1. Build chord groups -------------------------------------------
    groups: list[list[SolenoidEvent]] = [[events[0]]]
    for ev in events[1:]:
        if ev.timestamp_us - groups[-1][0].timestamp_us <= chord_window_us:
            groups[-1].append(ev)
        else:
            groups.append([ev])

    # --- 2. Shift only when a channel in the group has a recent ON -------
    adjusted: list[tuple[int, list[SolenoidEvent]]] = []
    last_on_per_channel: dict[int, int] = {}

    for g in groups:
        earliest = g[0].timestamp_us
        for ev in g:
            if ev.event_type == EventType.NOTE_ON:
                last = last_on_per_channel.get(ev.channel)
                if last is not None:
                    required = last + min_gap_us
                    if required > earliest:
                        earliest = required

        adjusted.append((earliest, g))
        for ev in g:
            if ev.event_type == EventType.NOTE_ON:
                last_on_per_channel[ev.channel] = earliest

    # Re-sort: a heavily-shifted group could end up after a later one
    adjusted.sort(key=lambda x: x[0])

    # --- 3. Emit ---------------------------------------------------------
    result: list[SolenoidEvent] = []
    for t, g in adjusted:
        for ev in g:
            result.append(SolenoidEvent(
                timestamp_us=t,
                channel=ev.channel,
                event_type=ev.event_type,
                velocity=ev.velocity,
            ))
    return result

# Tune these
NOTE_EXTEND_US       = 50_000   # push every OFF back by this much -20 , 35
MIN_NOTE_DURATION_US = 120_000   # floor: every note must last at least this long -30, 50

def extend_note_durations(events: list[SolenoidEvent],
                          extend_us: int = NOTE_EXTEND_US,
                          min_duration_us: int = MIN_NOTE_DURATION_US,
                          ) -> list[SolenoidEvent]:
    """
    Push NOTE_OFF events LATER to give each note time to actually ring on
    the solenoid piano. For fast runs, the original MIDI durations are too
    short for the hammer/string interaction to sound, so we intentionally
    slur notes together by delaying their releases.

    For each NOTE_OFF, the new off time is:

        new_off = max(on_time + min_duration_us, original_off + extend_us)

    ...clamped so it never crosses the NEXT NOTE_ON on the same channel
    (same-channel overlap makes no physical sense — a key is down or up).

    NOTE_ONs are never moved; their timestamps are the perceptually
    critical ones.
    """
    import bisect

    # Per-channel sorted list of NOTE_ON timestamps, for fast "next ON" lookup.
    on_times_by_channel: dict[int, list[int]] = {}
    for ev in events:
        if ev.event_type == EventType.NOTE_ON:
            on_times_by_channel.setdefault(ev.channel, []).append(ev.timestamp_us)

    last_on_per_channel: dict[int, int] = {}
    result: list[SolenoidEvent] = []

    for ev in events:
        if ev.event_type == EventType.NOTE_ON:
            last_on_per_channel[ev.channel] = ev.timestamp_us
            result.append(ev)
            continue

        # NOTE_OFF: figure out where it's allowed to end up
        on_time = last_on_per_channel.get(ev.channel)
        if on_time is None:
            # Orphan OFF (no matching ON) — leave it alone
            result.append(ev)
            continue

        desired_off = max(on_time + min_duration_us,
                          ev.timestamp_us + extend_us)

        # Cap at the next NOTE_ON on this channel (minus 1 µs margin),
        # otherwise the firmware sees OFF and ON at the same instant
        # and the ordering is ambiguous.
        ons = on_times_by_channel.get(ev.channel, [])
        idx = bisect.bisect_right(ons, ev.timestamp_us)
        if idx < len(ons):
            desired_off = min(desired_off, ons[idx] - 1)

        # Never shorten a note below its original length
        new_off = max(ev.timestamp_us, desired_off)

        result.append(SolenoidEvent(
            timestamp_us=new_off,
            channel=ev.channel,
            event_type=ev.event_type,
            velocity=ev.velocity,
        ))

    result.sort(key=lambda e: e.timestamp_us)
    return result

FAST_RUN_THRESHOLD_US = 80_000   # only extend if next ON arrives within this
MIN_NOTE_DURATION_US  = 50_000   # target minimum on-to-off duration in fast runs

def extend_fast_run_offs(events: list[SolenoidEvent],
                         fast_threshold_us: int = FAST_RUN_THRESHOLD_US,
                         min_duration_us: int = MIN_NOTE_DURATION_US,
                         ) -> list[SolenoidEvent]:
    """
    Delay NOTE_OFF events only when they occur inside a fast run — i.e. when
    the next NOTE_ON (on any channel) arrives within fast_threshold_us of
    this OFF. In that case, push the OFF late enough that the note lasts
    at least min_duration_us from its own NOTE_ON.

    Isolated notes with a long gap to the next event are left untouched,
    so sustained passages don't get smeared.

    Caps:
      • Never moves an OFF earlier than its original time.
      • Never pushes an OFF past the next NOTE_ON on the SAME channel
        (would make hammer state ambiguous).
    """
    import bisect

    # Sorted NOTE_ON timestamps: global (any channel) + per-channel.
    all_on_times: list[int] = []
    on_times_by_channel: dict[int, list[int]] = {}
    for ev in events:
        if ev.event_type == EventType.NOTE_ON:
            all_on_times.append(ev.timestamp_us)
            on_times_by_channel.setdefault(ev.channel, []).append(ev.timestamp_us)
    all_on_times.sort()

    last_on_per_channel: dict[int, int] = {}
    result: list[SolenoidEvent] = []

    for ev in events:
        if ev.event_type == EventType.NOTE_ON:
            last_on_per_channel[ev.channel] = ev.timestamp_us
            result.append(ev)
            continue

        on_time = last_on_per_channel.get(ev.channel)
        if on_time is None:
            result.append(ev)   # orphan OFF, skip
            continue

        # Is this a fast run? Look for the next NOTE_ON (any channel) AFTER
        # this OFF and see how close it is.
        idx = bisect.bisect_right(all_on_times, ev.timestamp_us)
        in_fast_run = (
            idx < len(all_on_times)
            and (all_on_times[idx] - ev.timestamp_us) <= fast_threshold_us
        )

        if not in_fast_run:
            result.append(ev)   # slow passage — leave alone
            continue

        desired_off = on_time + min_duration_us

        # Cap at the next ON on this channel so we don't cross it.
        ch_ons = on_times_by_channel.get(ev.channel, [])
        ch_idx = bisect.bisect_right(ch_ons, ev.timestamp_us)
        if ch_idx < len(ch_ons):
            desired_off = min(desired_off, ch_ons[ch_idx] - 1)

        new_off = max(ev.timestamp_us, desired_off)

        result.append(SolenoidEvent(
            timestamp_us=new_off,
            channel=ev.channel,
            event_type=ev.event_type,
            velocity=ev.velocity,
        ))

    result.sort(key=lambda e: e.timestamp_us)
    return result

# ---------------------------------------------------------------------------
# MIDI parser
# ---------------------------------------------------------------------------

def parse_midi_file(filepath: str | Path) -> SongData:
    filepath = Path(filepath)
    mid = mido.MidiFile(filepath)

    song = SongData(filename=filepath.name)
    tempo          = 500_000
    ticks_per_beat = mid.ticks_per_beat
    song.tempo_bpm = mido.tempo2bpm(tempo)
    abs_time_us    = 0

    for msg in mido.merge_tracks(mid.tracks):
        if msg.time > 0:
            delta_us = int(
                mido.tick2second(msg.time, ticks_per_beat, tempo) * 1_000_000
            )
            abs_time_us += delta_us

        if msg.type == 'set_tempo':
            tempo          = msg.tempo
            song.tempo_bpm = mido.tempo2bpm(tempo)
            continue

        if msg.type == 'note_on' and msg.velocity > 0:
            ch = midi_note_to_channel(msg.note)
            if ch is not None:
                song.events.append(SolenoidEvent(
                    timestamp_us=abs_time_us,
                    channel=ch,
                    event_type=EventType.NOTE_ON,
                    velocity=127,
                ))

        elif msg.type == 'note_off' or (
                msg.type == 'note_on' and msg.velocity == 0):
            ch = midi_note_to_channel(msg.note)
            if ch is not None:
                song.events.append(SolenoidEvent(
                    timestamp_us=abs_time_us,
                    channel=ch,
                    event_type=EventType.NOTE_OFF,
                    velocity=0,
                ))

    song.events.sort(key=lambda e: e.timestamp_us)

    # --- Timing cleanup pipeline -----------------------------------------
    # 1. Merge very fast OFF→ON restrikes into ON→ON (firmware handles those)
    song.events = merge_fast_restrikes(song.events, RESTRIKE_WINDOW_US) #COMMENTED THIS OUT BECAUSE ITS SHIT
    # 2. Drop duplicate ONs that arrive faster than a solenoid can reset
    #song.events = remove_fast_duplicate_ons(song.events, DUPLICATE_ON_US) COMMENTED THIS OUT BECAUSE ITS SHIT
    # 3. Enforce minimum inter-group gap WITHOUT splitting chords or
    #    re-ordering overlapping notes.
    #song.events = enforce_min_gap_chord_aware(song.events, MIN_GAP_US, CHORD_WINDOW_US)
    # was: song.events = enforce_min_gap_chord_aware(song.events, MIN_GAP_US, CHORD_WINDOW_US)
    song.events = enforce_min_gap_per_channel(song.events, MIN_GAP_US, CHORD_WINDOW_US)
    #song.events = decouple_offs_from_ons(song.events, OFF_SHIFT_US, MIN_NOTE_US)
    song.events = extend_note_durations(song.events, NOTE_EXTEND_US, MIN_NOTE_DURATION_US) #- this might work
    #song.events = extend_fast_run_offs(song.events, FAST_RUN_THRESHOLD_US, MIN_NOTE_DURATION_US)

    if song.events:
        song.duration_us = song.events[-1].timestamp_us

    return song

OFF_SHIFT_US = 30_000   # shift OFFs this much earlier to declutter transitions
MIN_NOTE_US  = 40_000   # never shorten a note below this

def decouple_offs_from_ons(events: list[SolenoidEvent],
                           shift_us: int = OFF_SHIFT_US,
                           min_note_us: int = MIN_NOTE_US
                           ) -> list[SolenoidEvent]:
    """
    Shift NOTE_OFFs earlier by up to shift_us, bounded by min_note_us so
    we never collapse a note too short. NOTE_ONs are never touched — their
    timestamps are the perceptually critical ones.

    Reduces peak events-per-timestamp at chord transitions where OFFs for
    the outgoing chord collide with ONs for the incoming chord, which is
    what causes the firmware's I²C dispatch queue to back up.
    """
    last_on_per_channel: dict[int, int] = {}
    result: list[SolenoidEvent] = []

    for ev in events:
        if ev.event_type == EventType.NOTE_ON:
            last_on_per_channel[ev.channel] = ev.timestamp_us
            result.append(ev)
        else:  # NOTE_OFF
            on_time = last_on_per_channel.get(ev.channel, 0)
            # How far earlier can this OFF go without shortening the note
            # below min_note_us?
            max_early      = ev.timestamp_us - (on_time + min_note_us)
            actual_shift   = max(0, min(shift_us, max_early))
            result.append(SolenoidEvent(
                timestamp_us=ev.timestamp_us - actual_shift,
                channel=ev.channel,
                event_type=ev.event_type,
                velocity=ev.velocity,
            ))

    result.sort(key=lambda e: e.timestamp_us)
    return result


# ---------------------------------------------------------------------------
# Serial protocol
# ---------------------------------------------------------------------------

def build_batch_packet(events: list[SolenoidEvent]) -> bytes:
    payload = struct.pack('<BH', CMD_EVENT_BATCH, len(events))
    for ev in events:
        payload += ev.pack()
    checksum = 0
    for b in payload:
        checksum ^= b
    return bytes([PACKET_HEADER]) + payload + bytes([checksum, PACKET_FOOTER])


def build_command_packet(cmd: int) -> bytes:
    payload  = bytes([cmd])
    checksum = cmd
    return bytes([PACKET_HEADER]) + payload + bytes([checksum, PACKET_FOOTER])


def send_packet(ser: serial.Serial, packet: bytes) -> None:
    """Write a packet and flush the OS buffer before waiting for a reply."""
    ser.write(packet)
    try:
        ser.flush()
    except Exception:
        pass


def wait_for_ack(ser: serial.Serial, timeout: float = ACK_TIMEOUT_S) -> int | None:
    """
    Scan incoming bytes for a valid ACK and return the firmware's reported
    free-slot count. Returns None on timeout.

    ACK frame layout (6 bytes):
        [PACKET_HEADER] [CMD_ACK] [free_lo] [free_hi] [checksum] [FOOTER]

    Any stray bytes before the header (firmware debug prints, boot garbage,
    etc.) are discarded. We intentionally don't validate checksum/footer
    strictly — same leniency as the original working protocol.
    """
    deadline = time.monotonic() + timeout
    ser.timeout = 0.05  # short per-read timeout so we can poll deadline

    while time.monotonic() < deadline:
        b = ser.read(1)
        if not b:
            continue
        if b[0] != PACKET_HEADER:
            continue
        cmd_byte = ser.read(1)
        if len(cmd_byte) < 1 or cmd_byte[0] != CMD_ACK:
            # Not an ACK after a header — keep scanning for a fresh header.
            continue
        # Read free_lo, free_hi, checksum, footer. We don't care about the
        # last two; draining them keeps them from polluting the next read.
        tail = ser.read(4)
        if len(tail) < 4:
            continue
        free_slots = tail[0] | (tail[1] << 8)
        return free_slots

    return None


def drain_input(ser: serial.Serial) -> None:
    """Dump whatever is currently waiting in the input buffer."""
    try:
        ser.reset_input_buffer()
    except Exception:
        pass


def get_serial_ports() -> list[str]:
    ports = serial.tools.list_ports.comports()
    return [f"{p.device} - {p.description}" for p in ports]


def extract_port_name(port_string: str) -> str:
    return port_string.split(" - ")[0].strip()


# ---------------------------------------------------------------------------
# GUI Application
# ---------------------------------------------------------------------------

class SolenoidPianoApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Solenoid Piano Controller")
        self.geometry("1920x1080")
        self.minsize(750, 620)

        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")
        self.configure(fg_color=WINDOW_BG)

        self.song: SongData | None = None
        self.is_transmitting = False
        self.is_scanning_folder = False
        self.folder_view_mode = "grid"
        self.current_folder_path: Path | None = None
        self.folder_songs_with_meta: list[tuple[Path, SongData | None]] = []
        self.selected_folder_song: SongData | None = None
        self.selected_folder_song_path: Path | None = None
        self.folder_item_widgets: dict[Path, list[ctk.CTkBaseClass]] = {}
        self.folder_item_base_colors: dict[Path, str] = {}
        self.folder_selected_color = "#F4B06A"
        self.folder_sort_field = "Filename"
        self.folder_sort_order = "A-Z"

        self._build_ui()

    # -----------------------------------------------------------------------
    # UI construction
    # -----------------------------------------------------------------------

    def _build_ui(self):
        self.tabview = ctk.CTkTabview(self, fg_color=WINDOW_BG)
        # Keep tab-highlight customization compatible across CTk versions.
        try:
            self.tabview.configure(
                segmented_button_selected_color=ACCENT,
                segmented_button_selected_hover_color=ACCENT_HOVER,
            )
        except Exception:
            try:
                self.tabview._segmented_button.configure(
                    selected_color=ACCENT,
                    selected_hover_color=ACCENT_HOVER,
                )
            except Exception:
                pass
        self.tabview.pack(fill="both", expand=True, padx=10, pady=10)

        self.player_tab = self.tabview.add("Single Song")
        self.folder_tab = self.tabview.add("Folder View")

        self._build_single_song_tab(self.player_tab)
        self._build_folder_tab(self.folder_tab)

    def _build_single_song_tab(self, parent):
        # --- File selection ------------------------------------------------
        file_frame = ctk.CTkFrame(parent, fg_color=SECTION_BG)
        file_frame.pack(fill="x", padx=15, pady=(15, 5))

        ctk.CTkLabel(file_frame, text="MIDI File:",
                     font=ctk.CTkFont(size=14, weight="bold")).pack(
                         side="left", padx=(10, 5), pady=10)

        self.file_label = ctk.CTkLabel(
            file_frame, text="No file selected",
            font=ctk.CTkFont(size=13), text_color="gray")
        self.file_label.pack(side="left", fill="x", expand=True,
                             padx=5, pady=10)

        self.browse_btn = ctk.CTkButton(
            file_frame, text="Browse...", width=100,
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            command=self._browse_file)
        self.browse_btn.pack(side="right", padx=10, pady=10)

        # --- Song info -----------------------------------------------------
        self.info_frame = ctk.CTkFrame(parent, fg_color=SECTION_BG)
        self.info_frame.pack(fill="x", padx=15, pady=5)

        info_inner = ctk.CTkFrame(self.info_frame, fg_color="transparent")
        info_inner.pack(fill="x", padx=10, pady=8)

        self.info_labels: dict[str, ctk.CTkLabel] = {}
        for col, key in enumerate(["Events", "Duration", "Tempo", "Key Range"]):
            ctk.CTkLabel(info_inner, text=f"{key}:",
                         font=ctk.CTkFont(size=12, weight="bold")).grid(
                             row=0, column=col * 2, padx=(10, 2), sticky="e")
            lbl = ctk.CTkLabel(info_inner, text="--",
                               font=ctk.CTkFont(size=12))
            lbl.grid(row=0, column=col * 2 + 1, padx=(2, 15), sticky="w")
            self.info_labels[key] = lbl
            info_inner.columnconfigure(col * 2 + 1, weight=1)

        # --- Event list ----------------------------------------------------
        event_frame = ctk.CTkFrame(parent, fg_color=SECTION_BG)
        event_frame.pack(fill="both", expand=True, padx=15, pady=5)

        header_frame = ctk.CTkFrame(event_frame, fg_color="transparent")
        header_frame.pack(fill="x", padx=5, pady=(8, 0))

        for text, width in [("Time", 90), ("Ch", 40), ("Note", 55),
                             ("Type", 50), ("Velocity", 65)]:
            ctk.CTkLabel(header_frame, text=text, width=width,
                         font=ctk.CTkFont(size=12, weight="bold"),
                         anchor="w").pack(side="left", padx=5)

        self.event_textbox = ctk.CTkTextbox(
            event_frame, font=ctk.CTkFont(family="Consolas", size=12),
            state="disabled")
        self.event_textbox.pack(fill="both", expand=True, padx=5, pady=(2, 8))

        # --- Serial / transmit ---------------------------------------------
        serial_frame = ctk.CTkFrame(parent, fg_color=SECTION_BG)
        serial_frame.pack(fill="x", padx=15, pady=5)

        serial_inner = ctk.CTkFrame(serial_frame, fg_color="transparent")
        serial_inner.pack(fill="x", padx=10, pady=8)

        ctk.CTkLabel(serial_inner, text="Serial Port:",
                     font=ctk.CTkFont(size=13)).pack(side="left", padx=(0, 5))

        # Combo box dropdown button
        self.port_combo = ctk.CTkComboBox(
            serial_inner, width=280, values=["(click refresh)"],
            button_color=ACCENT, button_hover_color=ACCENT_HOVER,
            state="readonly")
        self.port_combo.pack(side="left", padx=5)

        ctk.CTkButton(serial_inner, text="Refresh", width=70,
                    fg_color=ACCENT, hover_color=ACCENT_HOVER,
                    command=self._refresh_ports).pack(side="left", padx=5)

        ctk.CTkLabel(serial_inner, text="Baud:",
                     font=ctk.CTkFont(size=13)).pack(
                         side="left", padx=(20, 5))

        self.baud_combo = ctk.CTkComboBox(
            serial_inner, width=100,
            values=["9600", "57600", "115200", "230400", "460800"],
            button_color=ACCENT, button_hover_color=ACCENT_HOVER,
            state="readonly")
        self.baud_combo.set("115200")
        self.baud_combo.pack(side="left", padx=5)

        # --- Action buttons ------------------------------------------------
        btn_frame = ctk.CTkFrame(parent, fg_color=SECTION_BG)
        btn_frame.pack(fill="x", padx=15, pady=5)

        btn_inner = ctk.CTkFrame(btn_frame, fg_color="transparent")
        btn_inner.pack(pady=8)

        # Upload & Play button
        self.transmit_btn = ctk.CTkButton(
            btn_inner, text="Upload & Play", width=150, height=40,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            command=self._start_transmit, state="disabled")
        self.transmit_btn.pack(side="left", padx=10)

        self.stop_btn = ctk.CTkButton(
            btn_inner, text="Stop", width=100, height=40,
            font=ctk.CTkFont(size=14),
            fg_color="#c0392b", hover_color="#e74c3c",
            command=self._send_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=10)

        # --- Progress bar --------------------------------------------------
        self.progress = ctk.CTkProgressBar(parent, progress_color=ACCENT)
        self.progress.pack(fill="x", padx=15, pady=(2, 5))
        self.progress.set(0)

        # --- Note test terminal --------------------------------------------
        test_frame = ctk.CTkFrame(parent, fg_color=SECTION_BG)
        test_frame.pack(fill="x", padx=15, pady=5)

        ctk.CTkLabel(test_frame, text="Note Test:",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(
                         side="left", padx=(10, 5), pady=8)

        ctk.CTkLabel(test_frame, text="Note:",
                     font=ctk.CTkFont(size=12)).pack(
                         side="left", padx=(5, 2), pady=8)

        self.note_entry = ctk.CTkEntry(
            test_frame, width=70, placeholder_text="C4",
            font=ctk.CTkFont(size=13))
        self.note_entry.pack(side="left", padx=3, pady=8)

        ctk.CTkLabel(test_frame, text="Dur (ms):",
                     font=ctk.CTkFont(size=12)).pack(
                         side="left", padx=(10, 2), pady=8)

        self.dur_entry = ctk.CTkEntry(
            test_frame, width=60, placeholder_text="500",
            font=ctk.CTkFont(size=13))
        self.dur_entry.insert(0, "500")
        self.dur_entry.pack(side="left", padx=3, pady=8)

        ctk.CTkLabel(test_frame, text="Vel:",
                     font=ctk.CTkFont(size=12)).pack(
                         side="left", padx=(10, 2), pady=8)

        self.vel_entry = ctk.CTkEntry(
            test_frame, width=50, placeholder_text="100",
            font=ctk.CTkFont(size=13))
        self.vel_entry.insert(0, "100")
        self.vel_entry.pack(side="left", padx=3, pady=8)

        self.test_btn = ctk.CTkButton(
            test_frame, text="Test", width=70, height=32,
            font=ctk.CTkFont(size=13),
            fg_color="#27ae60", hover_color="#2ecc71",
            command=self._test_note)
        self.test_btn.pack(side="left", padx=10, pady=8)

        self.note_entry.bind("<Return>", lambda e: self._test_note())

        # --- Status bar ----------------------------------------------------
        self.status_label = ctk.CTkLabel(
            parent, text="Ready — load a MIDI file to begin",
            font=ctk.CTkFont(size=12), anchor="w")
        self.status_label.pack(fill="x", padx=15, pady=(0, 10))

        self._refresh_ports()

    def _build_folder_tab(self, parent):
        # --- Folder selection header --------------------------------------
        folder_frame = ctk.CTkFrame(parent, fg_color=SECTION_BG)
        folder_frame.pack(fill="x", padx=15, pady=(15, 5))

        ctk.CTkLabel(folder_frame, text="Song Folder:",
                     font=ctk.CTkFont(size=14, weight="bold")).pack(
                         side="left", padx=(10, 5), pady=10)

        self.folder_label = ctk.CTkLabel(
            folder_frame, text="No folder selected",
            font=ctk.CTkFont(size=13), text_color="gray")
        self.folder_label.pack(side="left", fill="x", expand=True,
                               padx=5, pady=10)

        self.folder_browse_btn = ctk.CTkButton(
            folder_frame, text="Browse...", width=100,
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            command=self._browse_folder)
        self.folder_browse_btn.pack(side="right", padx=10, pady=10)

        # --- MIDI file grid container -------------------------------------
        grid_frame = ctk.CTkFrame(parent, fg_color=SECTION_BG)
        grid_frame.pack(fill="both", expand=True, padx=15, pady=5)

        self.folder_summary_label = ctk.CTkLabel(
            grid_frame,
            text="Select a folder to list .mid and .midi files",
            font=ctk.CTkFont(size=12),
            anchor="w")

        view_controls = ctk.CTkFrame(grid_frame, fg_color="transparent")
        view_controls.pack(fill="x", padx=10, pady=(10, 6))

        self.songs_label = ctk.CTkLabel(
            view_controls,
            text="Songs",
            font=ctk.CTkFont(size=15, weight="bold"),
            anchor="w",
        )
        self.songs_label.pack(side="left", padx=(0, 10))

        self.folder_summary_label.pack(in_=view_controls, side="left",
                                       fill="x", expand=True)

        self.grid_view_btn = ctk.CTkButton(
            view_controls,
            text="Grid",
            width=70,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            command=lambda: self._set_folder_view_mode("grid"),
        )
        self.grid_view_btn.pack(side="right", padx=(6, 0))

        self.list_view_btn = ctk.CTkButton(
            view_controls,
            text="List",
            width=70,
            fg_color="#D7B79A",
            hover_color="#C8A585",
            text_color="black",
            command=lambda: self._set_folder_view_mode("list"),
        )
        self.list_view_btn.pack(side="right")

        self.sort_order_combo = ctk.CTkComboBox(
            view_controls,
            width=90,
            values=["A-Z", "Z-A"],
            state="readonly",
            button_color=ACCENT,
            button_hover_color=ACCENT_HOVER,
            command=self._on_folder_sort_changed,
        )
        self.sort_order_combo.set("A-Z")
        self.sort_order_combo.pack(side="right", padx=(10, 0))

        self.sort_field_combo = ctk.CTkComboBox(
            view_controls,
            width=120,
            values=["Filename", "Composer", "Duration"],
            state="readonly",
            button_color=ACCENT,
            button_hover_color=ACCENT_HOVER,
            command=self._on_folder_sort_changed,
        )
        self.sort_field_combo.set("Filename")
        self.sort_field_combo.pack(side="right", padx=(6, 0))

        self.sort_label = ctk.CTkLabel(
            view_controls,
            text="Sort:",
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        self.sort_label.pack(side="right", padx=(12, 0))

        self.song_grid_scroll = ctk.CTkScrollableFrame(
            grid_frame,
            fg_color="transparent")
        self.song_grid_scroll.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.song_tiles: list[ctk.CTkFrame] = []
        self.song_rows: list[ctk.CTkFrame] = []

        # --- Selection overlay (hidden until a song is selected) ---------
        self.folder_overlay = ctk.CTkFrame(parent, fg_color=SECTION_BG)

        overlay_top = ctk.CTkFrame(self.folder_overlay, fg_color="transparent")
        overlay_top.pack(fill="x", padx=10, pady=(8, 6))

        self.folder_overlay_song_label = ctk.CTkLabel(
            overlay_top,
            text="Selected: --",
            anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        self.folder_overlay_song_label.pack(side="left", fill="x", expand=True)

        self.folder_unselect_btn = ctk.CTkButton(
            overlay_top,
            text="Unselect",
            width=90,
            fg_color="#D7B79A",
            hover_color="#C8A585",
            text_color="black",
            command=self._clear_folder_selection,
        )
        self.folder_unselect_btn.pack(side="right")

        overlay_serial = ctk.CTkFrame(self.folder_overlay, fg_color="transparent")
        overlay_serial.pack(fill="x", padx=10, pady=(0, 6))

        ctk.CTkLabel(overlay_serial, text="Serial Port:",
                     font=ctk.CTkFont(size=13)).pack(side="left", padx=(0, 5))

        self.folder_port_combo = ctk.CTkComboBox(
            overlay_serial,
            width=280,
            values=["(click refresh)"],
            button_color=ACCENT,
            button_hover_color=ACCENT_HOVER,
            state="readonly",
        )
        self.folder_port_combo.pack(side="left", padx=5)

        ctk.CTkButton(
            overlay_serial,
            text="Refresh",
            width=70,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            command=self._refresh_ports,
        ).pack(side="left", padx=5)

        ctk.CTkLabel(overlay_serial, text="Baud:",
                     font=ctk.CTkFont(size=13)).pack(side="left", padx=(20, 5))

        self.folder_baud_combo = ctk.CTkComboBox(
            overlay_serial,
            width=100,
            values=["9600", "57600", "115200", "230400", "460800"],
            button_color=ACCENT,
            button_hover_color=ACCENT_HOVER,
            state="readonly",
        )
        self.folder_baud_combo.set("115200")
        self.folder_baud_combo.pack(side="left", padx=5)

        overlay_buttons = ctk.CTkFrame(self.folder_overlay, fg_color="transparent")
        overlay_buttons.pack(fill="x", padx=10, pady=(0, 6))

        self.folder_transmit_btn = ctk.CTkButton(
            overlay_buttons,
            text="Upload & Play",
            width=150,
            height=38,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            command=self._start_folder_transmit,
        )
        self.folder_transmit_btn.pack(side="left", padx=(0, 10))

        self.folder_stop_btn = ctk.CTkButton(
            overlay_buttons,
            text="Stop",
            width=100,
            height=38,
            font=ctk.CTkFont(size=14),
            fg_color="#c0392b",
            hover_color="#e74c3c",
            command=self._send_folder_stop,
            state="disabled",
        )
        self.folder_stop_btn.pack(side="left")

        self.folder_progress = ctk.CTkProgressBar(self.folder_overlay,
                                                  progress_color=ACCENT)
        self.folder_progress.pack(fill="x", padx=10, pady=(0, 6))
        self.folder_progress.set(0)

        self.folder_overlay_status = ctk.CTkLabel(
            self.folder_overlay,
            text="",
            anchor="w",
            font=ctk.CTkFont(size=12),
        )
        self.folder_overlay_status.pack(fill="x", padx=10, pady=(0, 8))

        self._update_folder_view_buttons()
        self._refresh_ports()

    # -----------------------------------------------------------------------
    # Note name parser
    # -----------------------------------------------------------------------

    def _parse_note_name(self, name: str) -> int | None:
        name = name.strip()
        if not name:
            return None

        i = 2 if len(name) > 1 and name[1] in ('#', 'b') else 1
        note_part  = name[:i].upper()
        octave_part = name[i:]

        note_map = {
            'C': 0,  'C#': 1,  'DB': 1,
            'D': 2,  'D#': 3,  'EB': 3,
            'E': 4,  'FB': 4,
            'F': 5,  'F#': 6,  'GB': 6,
            'G': 7,  'G#': 8,  'AB': 8,
            'A': 9,  'A#': 10, 'BB': 10,
            'B': 11, 'CB': 11,
        }

        semitone = note_map.get(note_part)
        if semitone is None:
            return None

        try:
            octave = int(octave_part)
        except ValueError:
            return None

        midi_note = (octave + 1) * 12 + semitone
        if midi_note < MIDI_NOTE_LOW or midi_note > MIDI_NOTE_HIGH:
            return None

        return midi_note

    # -----------------------------------------------------------------------
    # Actions
    # -----------------------------------------------------------------------

    def _set_status(self, text: str):
        self.status_label.configure(text=text)
        self.update_idletasks()

    def _browse_folder(self):
        if self.is_scanning_folder:
            return

        folder = filedialog.askdirectory(title="Select Song Folder")
        if not folder:
            return

        folder_path = Path(folder)
        self.current_folder_path = folder_path
        self.folder_label.configure(
            text=folder_path.name,
            text_color=ACCENT,
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        self.folder_summary_label.configure(
            text="Scanning MIDI files...",
            text_color="black",
        )

        self.is_scanning_folder = True
        self.folder_browse_btn.configure(state="disabled")
        threading.Thread(
            target=self._scan_folder_worker,
            args=(folder_path,),
            daemon=True,
        ).start()

    def _scan_folder_worker(self, folder_path: Path):
        midi_files = sorted(
            [p for p in folder_path.iterdir() if p.is_file() and p.suffix.lower() in {".mid", ".midi"}],
            key=lambda p: p.name.lower(),
        )

        songs_with_meta: list[tuple[Path, SongData | None]] = []
        for midi_file in midi_files:
            try:
                song = parse_midi_file(midi_file)
            except Exception:
                song = None
            songs_with_meta.append((midi_file, song))

        self.after(0, self._on_folder_scan_complete, folder_path, songs_with_meta)

    def _on_folder_scan_complete(self, folder_path: Path,
                                 songs_with_meta: list[tuple[Path, SongData | None]]):
        self.current_folder_path = folder_path
        self.folder_songs_with_meta = songs_with_meta
        self.is_scanning_folder = False
        self.folder_browse_btn.configure(state="normal")
        self._render_folder_files()

    def _clear_folder_view_widgets(self):
        for tile in self.song_tiles:
            tile.destroy()
        self.song_tiles.clear()

        for row in self.song_rows:
            row.destroy()
        self.song_rows.clear()

        self.folder_item_widgets.clear()
        self.folder_item_base_colors.clear()

    def _set_folder_overlay_visible(self, visible: bool):
        if visible:
            self.folder_overlay.pack(fill="x", padx=15, pady=(0, 10))
        else:
            self.folder_overlay.pack_forget()

    def _clear_folder_selection(self):
        if self.selected_folder_song_path in self.folder_item_widgets:
            for widget in self.folder_item_widgets[self.selected_folder_song_path]:
                base = self.folder_item_base_colors.get(self.selected_folder_song_path, SECTION_BG)
                widget.configure(fg_color=base)

        self.selected_folder_song = None
        self.selected_folder_song_path = None
        self.folder_progress.set(0)
        self.folder_overlay_status.configure(text="")
        self.folder_overlay_song_label.configure(text="Selected: --")
        self._set_folder_overlay_visible(False)

    def _bind_song_click(self, widgets: list[ctk.CTkBaseClass], midi_file: Path,
                         song: SongData | None, base_color: str):
        self.folder_item_widgets[midi_file] = widgets
        self.folder_item_base_colors[midi_file] = base_color

        for widget in widgets:
            widget.bind(
                "<Button-1>",
                lambda _e, mf=midi_file, s=song: self._on_folder_song_clicked(mf, s),
            )

    def _on_folder_song_clicked(self, midi_file: Path, song: SongData | None):
        if self.selected_folder_song_path == midi_file:
            self._clear_folder_selection()
            return

        if self.selected_folder_song_path in self.folder_item_widgets:
            for widget in self.folder_item_widgets[self.selected_folder_song_path]:
                base = self.folder_item_base_colors.get(self.selected_folder_song_path, SECTION_BG)
                widget.configure(fg_color=base)

        if midi_file in self.folder_item_widgets:
            for widget in self.folder_item_widgets[midi_file]:
                widget.configure(fg_color=self.folder_selected_color)

        self.selected_folder_song_path = midi_file
        self.selected_folder_song = song

        if song is None:
            self.folder_overlay_song_label.configure(
                text=f"Selected: {midi_file.name} (parse failed)")
            self.folder_overlay_status.configure(
                text="This file could not be parsed. Re-select another song.")
            self.folder_transmit_btn.configure(state="disabled")
        else:
            self.folder_overlay_song_label.configure(text=f"Selected: {song.filename}")
            self.folder_overlay_status.configure(
                text=f"Ready to transmit {song.filename}")
            self.folder_transmit_btn.configure(state="normal")

        self.folder_progress.set(0)
        self._set_folder_overlay_visible(True)

    def _start_folder_transmit(self):
        if self.selected_folder_song is None:
            messagebox.showwarning("No Song Selected", "Select a parsed song first.")
            return

        port_str = self.folder_port_combo.get()
        if "No ports" in port_str or "click refresh" in port_str:
            messagebox.showwarning("No Port",
                                   "Select a serial port first.\n"
                                   "Click Refresh to scan for devices.")
            return

        if self.is_transmitting:
            return

        port = extract_port_name(port_str)
        baud = int(self.folder_baud_combo.get())
        song = self.selected_folder_song

        self.is_transmitting = True
        self.folder_transmit_btn.configure(state="disabled")
        self.folder_stop_btn.configure(state="normal")
        self.folder_browse_btn.configure(state="disabled")
        self.folder_progress.set(0)

        threading.Thread(target=self._transmit_worker_folder,
                         args=(port, baud, song), daemon=True).start()

    def _transmit_worker_folder(self, port: str, baud: int, song: SongData):
        ser: serial.Serial | None = None
        try:
            self._set_folder_status_safe(f"Connecting to {port}...")
            ser = serial.Serial(port, baud, timeout=ACK_TIMEOUT_S)
            time.sleep(POST_OPEN_SETTLE_S)
            drain_input(ser)

            self._set_folder_status_safe("Pinging MCU...")
            send_packet(ser, build_command_packet(CMD_PING))
            free = wait_for_ack(ser)
            if free is None:
                self._show_error_safe("Connection Failed",
                                      "No response from MCU.")
                return

            send_packet(ser, build_command_packet(CMD_STOP))
            free = wait_for_ack(ser)
            if free is None:
                self._show_error_safe("Transmission Error",
                                      "Failed to clear old events before upload.")
                return

            total = song.num_events
            sent = 0
            started = False

            while sent < total:
                if not self.is_transmitting:
                    self._set_folder_status_safe("Transmission cancelled.")
                    send_packet(ser, build_command_packet(CMD_STOP))
                    return

                batch = song.events[sent:sent + BATCH_SIZE]
                batch_len = len(batch)

                while free < batch_len:
                    if not self.is_transmitting:
                        self._set_folder_status_safe("Transmission cancelled.")
                        send_packet(ser, build_command_packet(CMD_STOP))
                        return
                    time.sleep(BACKPRESSURE_POLL_S)
                    send_packet(ser, build_command_packet(CMD_PING))
                    free = wait_for_ack(ser)
                    if free is None:
                        self._show_error_safe("Transmission Error",
                                              "Lost contact with MCU during streaming.")
                        send_packet(ser, build_command_packet(CMD_STOP))
                        return

                send_packet(ser, build_batch_packet(batch))
                free = wait_for_ack(ser)
                if free is None:
                    self._show_error_safe("Transmission Error",
                                          f"Lost ACK at event {sent + batch_len}.")
                    send_packet(ser, build_command_packet(CMD_STOP))
                    return

                sent += batch_len
                self._update_folder_progress_safe(sent / total)

                if not started and (sent >= BATCH_SIZE * PRIME_BATCHES or sent >= total):
                    send_packet(ser, build_command_packet(CMD_START))
                    if wait_for_ack(ser) is None:
                        self._show_error_safe("Playback Error", "No ACK for START.")
                        return
                    started = True
                    self._set_folder_status_safe(
                        f"Streaming & playing: {sent:,}/{total:,} events")
                else:
                    prefix = "Streaming" if started else "Priming"
                    self._set_folder_status_safe(f"{prefix}: {sent:,}/{total:,} events")

                time.sleep(INTER_BATCH_DELAY_S)

            send_packet(ser, build_command_packet(CMD_EOS))
            wait_for_ack(ser)

            if not started:
                send_packet(ser, build_command_packet(CMD_START))
                wait_for_ack(ser)

            self._update_folder_progress_safe(1.0)
            self._set_folder_status_safe(
                f"Playing: {song.filename} ({song.duration_sec:.1f}s, {song.tempo_bpm:.0f} BPM)")

        except serial.SerialException as e:
            self._show_error_safe("Serial Error", str(e))
        except Exception as e:
            self._show_error_safe("Error", str(e))
        finally:
            if ser is not None:
                try:
                    ser.close()
                except Exception:
                    pass
            self._finish_folder_transmit_safe()

    def _send_folder_stop(self):
        self.is_transmitting = False
        ser: serial.Serial | None = None
        try:
            port = extract_port_name(self.folder_port_combo.get())
            baud = int(self.folder_baud_combo.get())
            ser = serial.Serial(port, baud, timeout=1)
            send_packet(ser, build_command_packet(CMD_STOP))
            self._set_folder_status_safe("Stop command sent.")
        except Exception as e:
            self._set_folder_status_safe(f"Stop failed: {e}")
        finally:
            if ser is not None:
                try:
                    ser.close()
                except Exception:
                    pass

    def _set_folder_view_mode(self, mode: str):
        if mode not in ("grid", "list"):
            return
        self.folder_view_mode = mode
        self._update_folder_view_buttons()
        if self.current_folder_path is not None:
            self._render_folder_files()

    def _on_folder_sort_changed(self, _value: str):
        self.folder_sort_field = self.sort_field_combo.get()
        self.folder_sort_order = self.sort_order_combo.get()
        if self.current_folder_path is not None:
            self._render_folder_files()

    def _get_sorted_folder_songs(self,
                                 songs_with_meta: list[tuple[Path, SongData | None]]
                                 ) -> list[tuple[Path, SongData | None]]:
        field = self.folder_sort_field
        reverse = self.folder_sort_order == "Z-A"

        def key_func(item: tuple[Path, SongData | None]):
            midi_file, song = item
            if field == "Composer":
                return parse_composer_from_filename(midi_file).lower()
            if field == "Duration":
                if song is None:
                    return float("-inf")
                return float(song.duration_sec)
            return midi_file.name.lower()

        return sorted(songs_with_meta, key=key_func, reverse=reverse)

    def _update_folder_view_buttons(self):
        if self.folder_view_mode == "grid":
            self.grid_view_btn.configure(
                fg_color=ACCENT,
                hover_color=ACCENT_HOVER,
                text_color="white",
            )
            self.list_view_btn.configure(
                fg_color="#D7B79A",
                hover_color="#C8A585",
                text_color="black",
            )
        else:
            self.list_view_btn.configure(
                fg_color=ACCENT,
                hover_color=ACCENT_HOVER,
                text_color="white",
            )
            self.grid_view_btn.configure(
                fg_color="#D7B79A",
                hover_color="#C8A585",
                text_color="black",
            )

    def _render_folder_files(self):
        folder_path = self.current_folder_path
        songs_with_meta = self.folder_songs_with_meta

        if folder_path is None:
            return

        self._clear_folder_view_widgets()

        if not songs_with_meta:
            self.folder_summary_label.configure(
                text=f"No MIDI files found in {folder_path.name}",
                text_color="gray",
            )
            return

        parsed_ok = sum(1 for _, song in songs_with_meta if song is not None)
        self.folder_summary_label.configure(
            text=(
                f"Found {len(songs_with_meta)} MIDI file(s) in {folder_path.name} "
                f"({parsed_ok} parsed)"
            ),
            text_color="black",
        )

        if self.folder_view_mode == "list":
            self._populate_song_list(self._get_sorted_folder_songs(songs_with_meta))
        else:
            self._populate_song_grid(self._get_sorted_folder_songs(songs_with_meta))

        if self.selected_folder_song_path and self.selected_folder_song_path in self.folder_item_widgets:
            for widget in self.folder_item_widgets[self.selected_folder_song_path]:
                widget.configure(fg_color=self.folder_selected_color)
            self._set_folder_overlay_visible(True)
        else:
            self._clear_folder_selection()

    def _populate_song_grid(self, songs_with_meta: list[tuple[Path, SongData | None]]):
        columns = 5
        tile_size = 150

        for c in range(columns):
            self.song_grid_scroll.grid_columnconfigure(c, weight=1)

        for i, (midi_file, song) in enumerate(songs_with_meta):
            row = i // columns
            col = i % columns

            tile = ctk.CTkFrame(
                self.song_grid_scroll,
                width=tile_size,
                height=tile_size,
                fg_color="#FFDAB3",
                corner_radius=12,
                border_width=1,
                border_color="#E3B27A",
            )
            tile.grid(row=row, column=col, padx=8, pady=8, sticky="nsew")
            tile.grid_propagate(False)

            if song is None:
                meta_text = (
                    "Composer: \n"
                    "Duration: parse failed\n"
                    "Events: parse failed\n"
                    "Tempo: parse failed\n"
                    "Key Range: parse failed"
                )
            else:
                composer_text = parse_composer_from_filename(midi_file)
                if song.events:
                    channels = [e.channel for e in song.events]
                    low = midi_note_to_name(min(channels) + MIDI_NOTE_LOW)
                    high = midi_note_to_name(max(channels) + MIDI_NOTE_LOW)
                    key_range_text = f"{low} - {high}"
                else:
                    key_range_text = "--"

                meta_text = (
                    f"Composer: {composer_text}\n"
                    f"Duration: {song.duration_sec:.2f}s\n"
                    f"Events: {song.num_events:,}\n"
                    f"Tempo: {song.tempo_bpm:.0f} BPM\n"
                    f"Key Range: {key_range_text}"
                )

            tile_text = f"{midi_file.name}\n\n{meta_text}"

            name_label = ctk.CTkLabel(
                tile,
                text=tile_text,
                wraplength=tile_size - 20,
                justify="center",
                font=ctk.CTkFont(size=11, weight="bold"),
            )
            name_label.place(relx=0.5, rely=0.5, anchor="center")

            self._bind_song_click([tile, name_label], midi_file, song, "#FFDAB3")

            self.song_tiles.append(tile)

    def _populate_song_list(self, songs_with_meta: list[tuple[Path, SongData | None]]):
        header = ctk.CTkFrame(
            self.song_grid_scroll,
            fg_color="#F4D6B7",
            corner_radius=8,
        )
        header.pack(fill="x", padx=8, pady=(4, 6))
        header.grid_columnconfigure(0, weight=6)
        header.grid_columnconfigure(1, weight=2)
        header.grid_columnconfigure(2, weight=2)
        header.grid_columnconfigure(3, weight=2)
        header.grid_columnconfigure(4, weight=2)
        header.grid_columnconfigure(5, weight=3)

        ctk.CTkLabel(
            header,
            text="Filename",
            anchor="w",
            font=ctk.CTkFont(size=12, weight="bold"),
        ).grid(row=0, column=0, padx=10, pady=8, sticky="w")
        ctk.CTkLabel(
            header,
            text="Composer",
            anchor="w",
            font=ctk.CTkFont(size=12, weight="bold"),
        ).grid(row=0, column=1, padx=10, pady=8, sticky="w")
        ctk.CTkLabel(
            header,
            text="Duration",
            anchor="e",
            font=ctk.CTkFont(size=12, weight="bold"),
        ).grid(row=0, column=2, padx=10, pady=8, sticky="e")
        ctk.CTkLabel(
            header,
            text="Events",
            anchor="e",
            font=ctk.CTkFont(size=12, weight="bold"),
        ).grid(row=0, column=3, padx=10, pady=8, sticky="e")
        ctk.CTkLabel(
            header,
            text="Tempo",
            anchor="e",
            font=ctk.CTkFont(size=12, weight="bold"),
        ).grid(row=0, column=4, padx=10, pady=8, sticky="e")
        ctk.CTkLabel(
            header,
            text="Key Range",
            anchor="w",
            font=ctk.CTkFont(size=12, weight="bold"),
        ).grid(row=0, column=5, padx=10, pady=8, sticky="w")
        self.song_rows.append(header)

        for i, (midi_file, song) in enumerate(songs_with_meta, start=1):
            row = ctk.CTkFrame(
                self.song_grid_scroll,
                fg_color="#FFDAB3" if i % 2 else "#FCE7CF",
                corner_radius=8,
                border_width=1,
                border_color="#E3B27A",
            )
            row.pack(fill="x", padx=8, pady=4)
            row.grid_columnconfigure(0, weight=6)
            row.grid_columnconfigure(1, weight=2)
            row.grid_columnconfigure(2, weight=2)
            row.grid_columnconfigure(3, weight=2)
            row.grid_columnconfigure(4, weight=2)
            row.grid_columnconfigure(5, weight=3)

            if song is None:
                composer_text = ""
                duration_text = "parse failed"
                events_text = "parse failed"
                tempo_text = "parse failed"
                key_range_text = "parse failed"
            else:
                composer_text = parse_composer_from_filename(midi_file)
                if song.events:
                    channels = [e.channel for e in song.events]
                    low = midi_note_to_name(min(channels) + MIDI_NOTE_LOW)
                    high = midi_note_to_name(max(channels) + MIDI_NOTE_LOW)
                    key_range_text = f"{low} - {high}"
                else:
                    key_range_text = "--"

                duration_text = f"{song.duration_sec:.2f}s"
                events_text = f"{song.num_events:,}"
                tempo_text = f"{song.tempo_bpm:.0f} BPM"

            ctk.CTkLabel(
                row,
                text=midi_file.name,
                anchor="w",
                font=ctk.CTkFont(size=12, weight="bold"),
            ).grid(row=0, column=0, padx=10, pady=8, sticky="w")
            ctk.CTkLabel(
                row,
                text=composer_text,
                anchor="w",
                font=ctk.CTkFont(size=12),
            ).grid(row=0, column=1, padx=10, pady=8, sticky="w")
            ctk.CTkLabel(
                row,
                text=duration_text,
                anchor="e",
                font=ctk.CTkFont(size=12),
            ).grid(row=0, column=2, padx=10, pady=8, sticky="e")
            ctk.CTkLabel(
                row,
                text=events_text,
                anchor="e",
                font=ctk.CTkFont(size=12),
            ).grid(row=0, column=3, padx=10, pady=8, sticky="e")
            ctk.CTkLabel(
                row,
                text=tempo_text,
                anchor="e",
                font=ctk.CTkFont(size=12),
            ).grid(row=0, column=4, padx=10, pady=8, sticky="e")
            ctk.CTkLabel(
                row,
                text=key_range_text,
                anchor="w",
                font=ctk.CTkFont(size=12),
            ).grid(row=0, column=5, padx=10, pady=8, sticky="w")

            row_widgets = [row]
            for child in row.winfo_children():
                row_widgets.append(child)
            self._bind_song_click(row_widgets, midi_file, song,
                                  "#FFDAB3" if i % 2 else "#FCE7CF")

            self.song_rows.append(row)

    def _browse_file(self):
        path = filedialog.askopenfilename(
            title="Select MIDI File",
            filetypes=[("MIDI files", "*.mid *.midi"), ("All files", "*.*")])
        if not path:
            return

        self._set_status(f"Parsing {Path(path).name}...")
        self.update_idletasks()

        try:
            self.song = parse_midi_file(path)
        except Exception as e:
            messagebox.showerror("Parse Error", f"Could not parse MIDI file:\n{e}")
            self._set_status("Error parsing file.")
            return

        self.file_label.configure(text=Path(path).name, text_color=ACCENT, font=ctk.CTkFont(size=13, weight="bold"))
        self._update_song_info()
        self._populate_event_list()
        self.transmit_btn.configure(state="normal")
        self._set_status(
            f"Loaded {self.song.num_events} events "
            f"({self.song.duration_sec:.1f}s) — ready to transmit")

    def _update_song_info(self):
        if not self.song:
            return
        self.info_labels["Events"].configure(text=f"{self.song.num_events:,}")
        self.info_labels["Duration"].configure(text=f"{self.song.duration_sec:.2f}s")
        self.info_labels["Tempo"].configure(text=f"{self.song.tempo_bpm:.0f} BPM")

        if self.song.events:
            channels = [e.channel for e in self.song.events]
            low  = midi_note_to_name(min(channels) + MIDI_NOTE_LOW)
            high = midi_note_to_name(max(channels) + MIDI_NOTE_LOW)
            self.info_labels["Key Range"].configure(text=f"{low} — {high}")
        else:
            self.info_labels["Key Range"].configure(text="--")

    def _populate_event_list(self):
        self.event_textbox.configure(state="normal")
        self.event_textbox.delete("1.0", "end")

        if not self.song:
            self.event_textbox.configure(state="disabled")
            return

        max_display = 5000
        lines = []
        for i, ev in enumerate(self.song.events):
            if i >= max_display:
                lines.append(
                    f"\n  ... {self.song.num_events - max_display:,} "
                    f"more events not shown ...")
                break
            t, ch, note, etype, vel = ev.format_row()
            lines.append(f" {t:>10s}   {ch:>3s}    {note:<5s}  "
                         f"{etype:<4s}    {vel:>3s}")

        self.event_textbox.insert("1.0", "\n".join(lines))
        self.event_textbox.configure(state="disabled")

    def _refresh_ports(self):
        ports = get_serial_ports()
        if ports:
            if hasattr(self, "port_combo"):
                self.port_combo.configure(values=ports)
                self.port_combo.set(ports[0])
            if hasattr(self, "folder_port_combo"):
                self.folder_port_combo.configure(values=ports)
                self.folder_port_combo.set(ports[0])
        else:
            if hasattr(self, "port_combo"):
                self.port_combo.configure(values=["No ports found"])
                self.port_combo.set("No ports found")
            if hasattr(self, "folder_port_combo"):
                self.folder_port_combo.configure(values=["No ports found"])
                self.folder_port_combo.set("No ports found")

    def _start_transmit(self):
        if not self.song or self.song.num_events == 0:
            messagebox.showwarning("No Data", "Load a MIDI file first.")
            return

        port_str = self.port_combo.get()
        if "No ports" in port_str or "click refresh" in port_str:
            messagebox.showwarning("No Port",
                                   "Select a serial port first.\n"
                                   "Click Refresh to scan for devices.")
            return

        port = extract_port_name(port_str)
        baud = int(self.baud_combo.get())

        self.is_transmitting = True
        self.transmit_btn.configure(state="disabled")
        self.browse_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.progress.set(0)

        threading.Thread(target=self._transmit_worker,
                         args=(port, baud), daemon=True).start()

    def _transmit_worker(self, port: str, baud: int):
        """
        Streaming upload:
          1. Ping + clear ring.
          2. Prime the ring with PRIME_BATCHES worth of events.
          3. Send CMD_START — playback begins using what we've primed.
          4. Keep feeding batches. Every ACK tells us the current free-slot
             count; if a batch wouldn't fit, we poll with PING until room
             opens up (back-pressure).
          5. When all events are sent, send CMD_EOS so the firmware knows
             to stop when the ring finally drains.
        """
        ser: serial.Serial | None = None
        try:
            self._set_status_safe(f"Connecting to {port}...")
            ser = serial.Serial(port, baud, timeout=ACK_TIMEOUT_S)
            time.sleep(POST_OPEN_SETTLE_S)
            drain_input(ser)  # dump any boot-time garbage from the R4

            # --- Handshake ------------------------------------------------
            self._set_status_safe("Pinging MCU...")
            send_packet(ser, build_command_packet(CMD_PING))
            free = wait_for_ack(ser)
            if free is None:
                self._show_error_safe(
                    "Connection Failed",
                    "No response from MCU.\n\n"
                    "Check that:\n"
                    "• The USB cable is connected\n"
                    "• The correct COM port is selected\n"
                    "• The MCU firmware is running")
                return

            # Clear any old events still in the ring.
            send_packet(ser, build_command_packet(CMD_STOP))
            free = wait_for_ack(ser)
            if free is None:
                self._show_error_safe("Transmission Error",
                                      "Failed to clear old events before upload.")
                return

            total   = self.song.num_events
            sent    = 0
            started = False

            while sent < total:
                if not self.is_transmitting:
                    self._set_status_safe("Transmission cancelled.")
                    send_packet(ser, build_command_packet(CMD_STOP))
                    return

                batch     = self.song.events[sent:sent + BATCH_SIZE]
                batch_len = len(batch)

                # --- Back-pressure: wait for room in the firmware ring ---
                # The firmware reports free_slots on every ACK. If it's
                # less than our next batch size, poll with PING until room
                # opens up. This is cheap: a PING round-trip is ~1 ms and
                # only happens once playback has caught up with upload.
                while free < batch_len:
                    if not self.is_transmitting:
                        self._set_status_safe("Transmission cancelled.")
                        send_packet(ser, build_command_packet(CMD_STOP))
                        return
                    time.sleep(BACKPRESSURE_POLL_S)
                    send_packet(ser, build_command_packet(CMD_PING))
                    free = wait_for_ack(ser)
                    if free is None:
                        self._show_error_safe(
                            "Transmission Error",
                            "Lost contact with MCU during streaming.")
                        send_packet(ser, build_command_packet(CMD_STOP))
                        return

                # --- Send the batch -------------------------------------
                send_packet(ser, build_batch_packet(batch))
                free = wait_for_ack(ser)
                if free is None:
                    self._show_error_safe(
                        "Transmission Error",
                        f"Lost ACK at event {sent + batch_len}.\n\n"
                        "Possible causes:\n"
                        "• Firmware is printing debug text that "
                        "desyncs the protocol\n"
                        "• USB cable or connection issue")
                    send_packet(ser, build_command_packet(CMD_STOP))
                    return

                sent += batch_len
                self._update_progress_safe(sent / total)

                # --- Kick off playback once we've primed the ring --------
                # We prime with a couple of batches' worth of events so
                # the playback engine has a head start before serial
                # throughput has to keep up with real time.
                if not started and (sent >= BATCH_SIZE * PRIME_BATCHES or
                                    sent >= total):
                    send_packet(ser, build_command_packet(CMD_START))
                    if wait_for_ack(ser) is None:
                        self._show_error_safe("Playback Error",
                                              "No ACK for START.")
                        return
                    started = True
                    self._set_status_safe(
                        f"Streaming & playing: {sent:,}/{total:,} events")
                else:
                    status_prefix = "Streaming" if started else "Priming"
                    self._set_status_safe(
                        f"{status_prefix}: {sent:,}/{total:,} events")

                # Small breather so the MCU can drain its ingest queue.
                time.sleep(INTER_BATCH_DELAY_S)

            # --- All events uploaded --------------------------------------
            # Tell the firmware no more events are coming so it can end
            # playback after the ring finally drains.
            send_packet(ser, build_command_packet(CMD_EOS))
            wait_for_ack(ser)

            # Edge case: very short song never met the prime threshold
            # (e.g. <2 batches total), so we never sent START.
            if not started:
                send_packet(ser, build_command_packet(CMD_START))
                wait_for_ack(ser)

            self._update_progress_safe(1.0)
            self._set_status_safe(
                f"Playing: {self.song.filename} "
                f"({self.song.duration_sec:.1f}s, "
                f"{self.song.tempo_bpm:.0f} BPM)")

        except serial.SerialException as e:
            self._show_error_safe("Serial Error", str(e))
        except Exception as e:
            self._show_error_safe("Error", str(e))
        finally:
            if ser is not None:
                try:
                    ser.close()
                except Exception:
                    pass
            self._finish_transmit_safe()

    def _test_note(self):
        note_text = self.note_entry.get().strip()
        if not note_text:
            self._set_status("Enter a note name (e.g. C4, A0, G#5)")
            return

        midi_note = self._parse_note_name(note_text)
        if midi_note is None:
            self._set_status(
                f"Invalid note: '{note_text}' — use format like C4, A#0, "
                f"Gb5 (range A0–C8)")
            return

        try:
            dur_ms = int(self.dur_entry.get())
            if dur_ms <= 0:
                raise ValueError
        except ValueError:
            self._set_status("Invalid duration — enter a number in ms")
            return

        try:
            velocity = max(1, min(127, int(self.vel_entry.get())))
        except ValueError:
            velocity = 127

        port_str = self.port_combo.get()
        if "No ports" in port_str or "click refresh" in port_str:
            self._set_status("Select a serial port first")
            return

        port      = extract_port_name(port_str)
        baud      = int(self.baud_combo.get())
        channel   = midi_note - MIDI_NOTE_LOW
        dur_us    = dur_ms * 1_000
        note_name = midi_note_to_name(midi_note)

        self._set_status(
            f"Testing {note_name} (ch={channel}, vel={velocity}, {dur_ms}ms)...")

        events = [
            SolenoidEvent(0,      channel, EventType.NOTE_ON,  velocity),
            SolenoidEvent(dur_us, channel, EventType.NOTE_OFF, 0),
        ]

        threading.Thread(target=self._test_note_worker,
                         args=(port, baud, events, note_name),
                         daemon=True).start()

    def _test_note_worker(self, port, baud, events, note_name):
        ser: serial.Serial | None = None
        try:
            ser = serial.Serial(port, baud, timeout=ACK_TIMEOUT_S)
            time.sleep(0.5)
            drain_input(ser)

            send_packet(ser, build_command_packet(CMD_PING))
            if wait_for_ack(ser) is None:
                self._set_status_safe("No response from MCU — check connection")
                return

            send_packet(ser, build_command_packet(CMD_STOP))
            if wait_for_ack(ser) is None:
                self._set_status_safe("Failed to clear old events")
                return

            send_packet(ser, build_batch_packet(events))
            if wait_for_ack(ser) is None:
                self._set_status_safe("Lost ACK during test note")
                return

            send_packet(ser, build_command_packet(CMD_START))
            wait_for_ack(ser)

            # For a test note, tell the firmware the "stream" is done so
            # playback cleanly stops after the OFF fires.
            send_packet(ser, build_command_packet(CMD_EOS))
            wait_for_ack(ser)

            self._set_status_safe(f"Played {note_name}")

        except serial.SerialException as e:
            self._set_status_safe(f"Serial error: {e}")
        except Exception as e:
            self._set_status_safe(f"Error: {e}")
        finally:
            if ser is not None:
                try:
                    ser.close()
                except Exception:
                    pass

    def _send_stop(self):
        self.is_transmitting = False
        ser: serial.Serial | None = None
        try:
            port = extract_port_name(self.port_combo.get())
            baud = int(self.baud_combo.get())
            ser  = serial.Serial(port, baud, timeout=1)
            send_packet(ser, build_command_packet(CMD_STOP))
            self._set_status("Stop command sent.")
        except Exception as e:
            self._set_status(f"Stop failed: {e}")
        finally:
            if ser is not None:
                try:
                    ser.close()
                except Exception:
                    pass

    # -----------------------------------------------------------------------
    # Thread-safe GUI helpers
    # -----------------------------------------------------------------------

    def _set_status_safe(self, text: str):
        self.after(0, self._set_status, text)

    def _update_progress_safe(self, value: float):
        self.after(0, self.progress.set, value)

    def _show_error_safe(self, title: str, message: str):
        self.after(0, messagebox.showerror, title, message)

    def _set_folder_status_safe(self, text: str):
        self.after(0, self.folder_overlay_status.configure, {"text": text})

    def _update_folder_progress_safe(self, value: float):
        self.after(0, self.folder_progress.set, value)

    def _finish_folder_transmit_safe(self):
        def _finish():
            self.is_transmitting = False
            self.folder_transmit_btn.configure(
                state="normal" if self.selected_folder_song is not None else "disabled")
            self.folder_stop_btn.configure(state="disabled")
            self.folder_browse_btn.configure(state="normal")
        self.after(0, _finish)

    def _finish_transmit_safe(self):
        def _finish():
            self.is_transmitting = False
            self.transmit_btn.configure(state="normal")
            self.browse_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
        self.after(0, _finish)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    ctk.set_appearance_mode("light")
    ctk.set_default_color_theme("blue")
    app = SolenoidPianoApp()
    app.mainloop()


if __name__ == '__main__':
    main()