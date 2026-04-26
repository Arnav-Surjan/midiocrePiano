"""
Solenoid Piano Controller
==========================
Desktop GUI for parsing MIDI files and transmitting to
the Arduino UNO R4 Minima solenoid piano controller.

(Original docstring preserved — see header in your repo.)

Adds a synthesia-style visualizer window that opens alongside the main
controller window. Falling notes are synced to the firmware playback
clock so what you see hitting the keyboard line is exactly what the
solenoids are firing.
"""

import bisect
import customtkinter as ctk
import tkinter as tk
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
CMD_EOS         = 0x05
CMD_ACK         = 0x10

BATCH_SIZE = 64

RING_CAPACITY = 1024
PRIME_BATCHES = 2

RESTRIKE_WINDOW_US  = 100_000
DUPLICATE_ON_US     = 210_000
MIN_GAP_US          = 15_000
CHORD_WINDOW_US     = 1_000

INTER_BATCH_DELAY_S = 0.005
POST_OPEN_SETTLE_S  = 2.0
ACK_TIMEOUT_S       = 2.0
BACKPRESSURE_POLL_S = 0.010

NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F',
              'F#', 'G', 'G#', 'A', 'A#', 'B']

ACCENT       = "#BF5700"
ACCENT_HOVER = "#C48654"
SECTION_BG   = "#FFE6C8"
WINDOW_BG    = "#FFF7EE"

# --- Visualizer constants -------------------------------------------------
# Full piano = MIDI 21..108 (A0..C8) = 88 keys. The solenoid rig only
# covers NUM_KEYS of those (channel 0 == MIDI_NOTE_LOW), but we still
# draw the full 88-key keyboard like in the reference screenshots.
VIS_FIRST_MIDI = 21
VIS_LAST_MIDI  = 108
VIS_NUM_WHITE  = 52   # white keys in 88-key piano
VIS_FALL_PX_PER_SEC = 220     # how fast notes fall
VIS_LOOKAHEAD_S     = 4.0     # how far ahead of the playhead to draw
VIS_FRAME_INTERVAL_MS = 33    # ~30 FPS

VIS_BG          = "#FFE6C8"
VIS_HEADER_BG   = "#BF5700"
VIS_HEADER_FG   = "#ffffff"
VIS_GUIDELINE   = "#F8971F"
VIS_PLAYLINE    = "#BF5700"
# Note colors: bright body + darker outline/tail like the reference
VIS_NOTE_GREEN_BODY    = "#F8971F"
VIS_NOTE_GREEN_OUTLINE = "#BF5700"
# When a note is "active" (currently sounding), use a slightly different
# look so the bar visually anchors to the key.
VIS_KEY_ACTIVE  = "#F8971F"
VIS_WHITE_KEY   = "#ffffff"
VIS_BLACK_KEY   = "#000000"
VIS_KEY_BORDER  = "#000000"

# --- Header marquee constants --------------------------------------------
# Filename gets a fixed-percentage region on the left; if the filename is
# longer than that region, it scrolls marquee-style (left to right) with
# a "tail" gap so the loop is seamless. The right side (Song Duration /
# Current Song Progress) is anchored to the right edge and never moves.
VIS_HEADER_FILENAME_FRAC = 0.55   # filename region = 55% of header width
VIS_HEADER_PAD_X         = 31     # left/right padding inside the header
VIS_HEADER_PAD_Y         = 27     # top/bottom padding inside the header
VIS_HEADER_FONT          = ("Consolas", 26, "bold")
VIS_MARQUEE_PX_PER_SEC   = 60     # scroll speed
VIS_MARQUEE_TAIL_PX      = 80     # gap between filename copies in loop


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
    # Pre-paired (start_us, end_us, channel) triples for the visualizer.
    # Computed once after the timing-cleanup pipeline runs.
    note_segments: list[tuple[int, int, int]] = field(default_factory=list)

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


def is_black_key(midi_note: int) -> bool:
    return (midi_note % 12) in (1, 3, 6, 8, 10)


def strip_midi_extension(filename: str) -> str:
    """
    Strip a trailing .mid / .midi extension (case-insensitive) for display.
    Examples:
        "Fur Elise - Beethoven.mid"   -> "Fur Elise - Beethoven"
        "Angry Birds Theme.MIDI"      -> "Angry Birds Theme"
        "no_extension_song"           -> "no_extension_song"
    """
    if filename.lower().endswith(".midi"):
        return filename[:-5]
    if filename.lower().endswith(".mid"):
        return filename[:-4]
    return filename


def build_note_segments(events: list[SolenoidEvent]
                        ) -> list[tuple[int, int, int]]:
    """
    Pair NOTE_ON with the next NOTE_OFF on the same channel.
    Returns list of (start_us, end_us, channel) triples.
    Unmatched ONs are extended to start_us + 200ms as a sane default.
    """
    open_notes: dict[int, int] = {}   # channel -> on_time
    segments: list[tuple[int, int, int]] = []

    for ev in events:
        if ev.event_type == EventType.NOTE_ON:
            # If we somehow have an unmatched ON already, close it at this
            # new ON (synthesia-style — repeat means previous segment ends).
            prev = open_notes.get(ev.channel)
            if prev is not None:
                segments.append((prev, ev.timestamp_us, ev.channel))
            open_notes[ev.channel] = ev.timestamp_us
        else:
            start = open_notes.pop(ev.channel, None)
            if start is not None:
                end = max(ev.timestamp_us, start + 30_000)  # min 30ms
                segments.append((start, end, ev.channel))

    # Any unmatched ONs left over
    for ch, start in open_notes.items():
        segments.append((start, start + 200_000, ch))

    segments.sort(key=lambda s: s[0])
    return segments


# ---------------------------------------------------------------------------
# Timing / event-cleanup pipeline (unchanged)
# ---------------------------------------------------------------------------

def merge_fast_restrikes(
    events: list[SolenoidEvent],
    restrike_window_us: int = RESTRIKE_WINDOW_US,
    firmware_restrike_delay_us: int = 110_000,
    min_on_gap_us: int = 100_000,
) -> list[SolenoidEvent]:
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

    merged.sort(key=lambda e: e.timestamp_us)

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
    result: list[SolenoidEvent] = []
    last_on_time: dict[int, int] = {}

    for ev in events:
        if ev.event_type == EventType.NOTE_ON:
            last_time = last_on_time.get(ev.channel)
            if last_time is not None and (ev.timestamp_us - last_time) < window_us:
                continue
            last_on_time[ev.channel] = ev.timestamp_us
            result.append(ev)
        else:
            result.append(ev)

    return result


def enforce_min_gap_chord_aware(events: list[SolenoidEvent],
                                min_gap_us: int = MIN_GAP_US,
                                chord_window_us: int = CHORD_WINDOW_US
                                ) -> list[SolenoidEvent]:
    if not events:
        return []

    groups: list[list[SolenoidEvent]] = [[events[0]]]
    for ev in events[1:]:
        if ev.timestamp_us - groups[-1][0].timestamp_us <= chord_window_us:
            groups[-1].append(ev)
        else:
            groups.append([ev])

    result: list[SolenoidEvent] = []
    scheduled_time: int = 0

    for idx, g in enumerate(groups):
        original_time = g[0].timestamp_us
        if idx == 0:
            t = original_time
        else:
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
    if not events:
        return []

    groups: list[list[SolenoidEvent]] = [[events[0]]]
    for ev in events[1:]:
        if ev.timestamp_us - groups[-1][0].timestamp_us <= chord_window_us:
            groups[-1].append(ev)
        else:
            groups.append([ev])

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

    adjusted.sort(key=lambda x: x[0])

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


NOTE_EXTEND_US       = 50_000
MIN_NOTE_DURATION_US = 120_000


def extend_note_durations(events: list[SolenoidEvent],
                          extend_us: int = NOTE_EXTEND_US,
                          min_duration_us: int = MIN_NOTE_DURATION_US,
                          ) -> list[SolenoidEvent]:
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

        on_time = last_on_per_channel.get(ev.channel)
        if on_time is None:
            result.append(ev)
            continue

        desired_off = max(on_time + min_duration_us,
                          ev.timestamp_us + extend_us)

        ons = on_times_by_channel.get(ev.channel, [])
        idx = bisect.bisect_right(ons, ev.timestamp_us)
        if idx < len(ons):
            desired_off = min(desired_off, ons[idx] - 1)

        new_off = max(ev.timestamp_us, desired_off)

        result.append(SolenoidEvent(
            timestamp_us=new_off,
            channel=ev.channel,
            event_type=ev.event_type,
            velocity=ev.velocity,
        ))

    result.sort(key=lambda e: e.timestamp_us)
    return result


FAST_RUN_THRESHOLD_US = 80_000


def extend_fast_run_offs(events: list[SolenoidEvent],
                         fast_threshold_us: int = FAST_RUN_THRESHOLD_US,
                         min_duration_us: int = MIN_NOTE_DURATION_US,
                         ) -> list[SolenoidEvent]:
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
            result.append(ev)
            continue

        idx = bisect.bisect_right(all_on_times, ev.timestamp_us)
        in_fast_run = (
            idx < len(all_on_times)
            and (all_on_times[idx] - ev.timestamp_us) <= fast_threshold_us
        )

        if not in_fast_run:
            result.append(ev)
            continue

        desired_off = on_time + min_duration_us
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


OFF_SHIFT_US = 30_000
MIN_NOTE_US  = 40_000


def decouple_offs_from_ons(events: list[SolenoidEvent],
                           shift_us: int = OFF_SHIFT_US,
                           min_note_us: int = MIN_NOTE_US
                           ) -> list[SolenoidEvent]:
    last_on_per_channel: dict[int, int] = {}
    result: list[SolenoidEvent] = []

    for ev in events:
        if ev.event_type == EventType.NOTE_ON:
            last_on_per_channel[ev.channel] = ev.timestamp_us
            result.append(ev)
        else:
            on_time = last_on_per_channel.get(ev.channel, 0)
            max_early    = ev.timestamp_us - (on_time + min_note_us)
            actual_shift = max(0, min(shift_us, max_early))
            result.append(SolenoidEvent(
                timestamp_us=ev.timestamp_us - actual_shift,
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

    song.events = merge_fast_restrikes(song.events, RESTRIKE_WINDOW_US)
    song.events = enforce_min_gap_per_channel(song.events, MIN_GAP_US, CHORD_WINDOW_US)
    song.events = extend_note_durations(song.events, NOTE_EXTEND_US, MIN_NOTE_DURATION_US)

    if song.events:
        song.duration_us = song.events[-1].timestamp_us

    # Pre-pair notes for the visualizer.
    song.note_segments = build_note_segments(song.events)

    return song


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
    ser.write(packet)
    try:
        ser.flush()
    except Exception:
        pass


def wait_for_ack(ser: serial.Serial, timeout: float = ACK_TIMEOUT_S) -> int | None:
    deadline = time.monotonic() + timeout
    ser.timeout = 0.05

    while time.monotonic() < deadline:
        b = ser.read(1)
        if not b:
            continue
        if b[0] != PACKET_HEADER:
            continue
        cmd_byte = ser.read(1)
        if len(cmd_byte) < 1 or cmd_byte[0] != CMD_ACK:
            continue
        tail = ser.read(4)
        if len(tail) < 4:
            continue
        free_slots = tail[0] | (tail[1] << 8)
        return free_slots

    return None


def drain_input(ser: serial.Serial) -> None:
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
# Visualizer Window
# ---------------------------------------------------------------------------

class VisualizerWindow(ctk.CTkToplevel):
    """
    Synthesia-style falling-notes visualizer. Reads playback state from
    the parent app and renders at ~30 FPS.

    Sync: parent app exposes `playback_start_monotonic` and
    `playback_time_offset_us`. Current song-time (in microseconds) is:
        time_offset_us + (time.monotonic() - playback_start_monotonic) * 1e6
    When playback isn't active we sit at offset 0 (or the saved pause point).
    """

    def __init__(self, master_app: "SolenoidPianoApp"):
        super().__init__(master_app)
        self.master_app = master_app

        self.title("Solenoid Piano — Visualizer")
        self.geometry("1400x700")
        self.minsize(900, 450)
        self.configure(fg_color=VIS_BG)

        # Hide instead of destroy on close — the main app owns lifetime.
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Fullscreen state. When True the OS titlebar is hidden and we
        # show our own custom titlebar only when the mouse is near the
        # top of the window.
        self._fullscreen = False
        self._custom_titlebar_visible = False
        # Original geometry remembered so exiting fullscreen restores it.
        self._pre_fullscreen_geom: str | None = None

        # --- Header bar (split layout: scrolling filename + fixed info) ---
        # Left side: a clipping Canvas that draws the filename text. If the
        # filename is too long, it scrolls marquee-style with a tail gap.
        # Right side: Song Duration + Current Song Progress, anchored to
        # the right edge so they never get pushed off-screen.
        self._build_header()

        # --- Canvas: notes fall here, keyboard at bottom ------------------
        self.canvas = tk.Canvas(
            self, bg=VIS_BG, highlightthickness=0, bd=0,
        )
        self.canvas.pack(fill="both", expand=True)

        # Cached layout — recomputed on resize
        self._layout_dirty = True
        self._key_rects: dict[int, tuple[int, int, int, int]] = {}  # midi -> (x0,y0,x1,y1)
        self._white_key_width = 0
        self._black_key_width = 0
        self._keyboard_top_y = 0
        self._canvas_w = 0
        self._canvas_h = 0

        self.canvas.bind("<Configure>", self._on_resize)

        # --- Custom titlebar overlay (only visible in fullscreen) ---------
        # In fullscreen we strip the OS window decorations with
        # overrideredirect, then show this overlay when the user moves the
        # mouse near the top edge. It sits above all other widgets via
        # `place()` and gets hidden with `place_forget()`.
        self._build_custom_titlebar()

        # --- Hotkeys ------------------------------------------------------
        # F11 toggles fullscreen, Esc exits it. Standard for media players.
        self.bind("<F11>", lambda e: self.toggle_fullscreen())
        self.bind("<Escape>", lambda e: self._exit_fullscreen())

        # Track mouse motion across the whole window so we can show/hide
        # the custom titlebar based on cursor proximity to the top edge.
        self.bind("<Motion>", self._on_mouse_motion)

        # Animation tick
        self._running = True
        self.after(VIS_FRAME_INTERVAL_MS, self._tick)

    # -- Header construction (split layout with scrolling filename) ---------

    def _build_header(self):
        """
        Layout:
          [ filename canvas (scrolling)         ] [ duration | progress ]
          <-------- ~55% width --------><pad>     <-- anchored right -->

        Implementation:
          - Outer `header_frame` is the orange bar; it's packed at top
            with fill="x" so it grows with the window.
          - `filename_canvas` is `place()`d on the left at (PAD_X, 0) with
            its width recomputed on every resize. We draw the filename
            text into it — twice, with a tail gap, when scrolling.
          - `right_label` is `place()`d at (relx=1.0, anchor="ne") so it
            hugs the right edge regardless of window width.

        We use `place()` instead of `pack()` for both children because we
        need precise pixel control and the right-anchored field has to be
        independent of how wide the left field is.
        """
        # Header frame stand-in for the old single Label. The bar uses the
        # same orange (VIS_HEADER_BG) and the same vertical padding the
        # old Label used, so the bar height matches the previous look.
        self.header_frame = tk.Frame(
            self, bg=VIS_HEADER_BG,
            height=self._compute_header_height(),
        )
        # `pack_propagate(False)` prevents the frame from shrinking to fit
        # its children (the children are placed, not packed, so by default
        # the frame would collapse to height=1).
        self.header_frame.pack_propagate(False)
        self.header_frame.pack(fill="x", side="top")

        # Right-side fixed labels (Duration | Progress). We use a single
        # Label instead of two because the text is always rendered as one
        # string and one widget is cheaper than two.
        self.right_label = tk.Label(
            self.header_frame,
            text="Song Duration: 0.0s | Current Song Progress: 0.0s",
            font=VIS_HEADER_FONT,
            fg=VIS_HEADER_FG, bg=VIS_HEADER_BG,
            anchor="e",
        )
        self.right_label.place(
            relx=1.0, rely=0.5, x=-VIS_HEADER_PAD_X, anchor="e",
        )

        # Left-side scrolling filename canvas. Width is set in
        # `_layout_header()` based on the actual frame width.
        self.filename_canvas = tk.Canvas(
            self.header_frame,
            bg=VIS_HEADER_BG,
            highlightthickness=0, bd=0,
        )
        self.filename_canvas.place(
            x=VIS_HEADER_PAD_X, rely=0.5, anchor="w",
        )

        # Marquee state. `_marquee_offset_px` is how far the text has
        # scrolled left (always >= 0). When it exceeds one full text+tail
        # width, we wrap it back to 0.
        self._filename_text         = "----------Waiting for Upload----------"
        self._filename_needs_scroll = False
        self._filename_text_width   = 0   # measured on draw
        self._marquee_offset_px     = 0.0
        self._marquee_last_tick_s   = time.monotonic()

        # Recompute the header layout when the window resizes.
        self.header_frame.bind("<Configure>", self._on_header_resize)

    def _compute_header_height(self) -> int:
        """
        Header height needs to match what the old single-Label used so the
        bar looks identical. The Label sized itself based on font height
        plus pady; we replicate that here.
        """
        # Tk doesn't give us font metrics until the root exists, so we
        # approximate: font size in points * ~1.5 line-height + 2*pady.
        # The font is ("Consolas", 26), so ~26 * 1.5 = 39 + 2*27 = 93.
        # That matches the visible bar height in your screenshots.
        font_size = VIS_HEADER_FONT[1]
        line_height = int(font_size * 1.5)
        return line_height + 2 * VIS_HEADER_PAD_Y

    def _on_header_resize(self, event):
        self._layout_header()

    def _layout_header(self):
        """Resize the filename canvas to match the current header width."""
        try:
            frame_w = self.header_frame.winfo_width()
            frame_h = self.header_frame.winfo_height()
        except tk.TclError:
            return
        if frame_w <= 1 or frame_h <= 1:
            return

        # Measure the right label's actual rendered width so we can reserve
        # exactly that much space (plus a gap) on the right side. Without
        # this, the filename canvas can extend underneath the right label
        # and its text will visually overlap.
        try:
            self.right_label.update_idletasks()
            right_w = self.right_label.winfo_reqwidth()
        except tk.TclError:
            right_w = 0

        # Gap between the filename region and the right-anchored info,
        # plus the existing right-edge padding the label already uses.
        gap_between = 24
        right_reserved = right_w + VIS_HEADER_PAD_X + gap_between

        # Filename region: bounded above by the percentage cap, bounded
        # below by what's actually free after reserving the right side.
        max_by_frac    = int(frame_w * VIS_HEADER_FILENAME_FRAC)
        max_by_space   = frame_w - VIS_HEADER_PAD_X - right_reserved
        filename_w = max(50, min(max_by_frac, max_by_space))

        canvas_h = frame_h
        self.filename_canvas.configure(width=filename_w, height=canvas_h)

        # Re-draw with the new region width (changes whether scrolling
        # is needed and re-measures text width if font changed).
        self._redraw_filename()

    def _set_filename_display(self, text: str):
        """Public-ish helper — call when the song changes."""
        if text == self._filename_text:
            return
        self._filename_text = text
        self._marquee_offset_px = 0.0
        self._marquee_last_tick_s = time.monotonic()
        self._redraw_filename()

    def _redraw_filename(self):
        """
        Draw the filename text into the canvas. If the text fits inside
        the canvas width, draw it once, statically. If not, draw it twice
        (separated by a tail gap) and let the marquee tick scroll it.
        """
        c = self.filename_canvas
        try:
            c.delete("all")
        except tk.TclError:
            return

        try:
            cw = c.winfo_width()
            ch = c.winfo_height()
        except tk.TclError:
            return
        if cw <= 1 or ch <= 1:
            return

        # Measure the text width by drawing it at an offscreen position,
        # querying its bbox, then erasing. This is the only reliable way
        # to know rendered text width with a given font in Tk.
        probe = c.create_text(
            -10000, ch // 2,
            text=self._filename_text,
            font=VIS_HEADER_FONT,
            fill=VIS_HEADER_FG,
            anchor="w",
        )
        bbox = c.bbox(probe)
        c.delete(probe)
        text_w = (bbox[2] - bbox[0]) if bbox else 0
        self._filename_text_width = text_w

        # Decide: scroll or static?
        self._filename_needs_scroll = text_w > cw

        if not self._filename_needs_scroll:
            # Static render at the left edge.
            c.create_text(
                0, ch // 2,
                text=self._filename_text,
                font=VIS_HEADER_FONT,
                fill=VIS_HEADER_FG,
                anchor="w",
                tags=("marquee",),
            )
            return

        # Scrolling render: draw two copies with a tail gap. The marquee
        # tick adjusts both x positions every frame.
        gap = VIS_MARQUEE_TAIL_PX
        x0 = -int(self._marquee_offset_px)
        x1 = x0 + text_w + gap
        for x in (x0, x1):
            c.create_text(
                x, ch // 2,
                text=self._filename_text,
                font=VIS_HEADER_FONT,
                fill=VIS_HEADER_FG,
                anchor="w",
                tags=("marquee",),
            )

    def _tick_marquee(self):
        """
        Advance the marquee offset based on real elapsed time. Called from
        the main render tick so we share a single timer instead of
        spawning a second `after()` loop.
        """
        now = time.monotonic()
        dt = now - self._marquee_last_tick_s
        self._marquee_last_tick_s = now

        if not self._filename_needs_scroll:
            return

        # Advance offset; wrap when one full (text + gap) has scrolled by.
        self._marquee_offset_px += dt * VIS_MARQUEE_PX_PER_SEC
        loop_w = self._filename_text_width + VIS_MARQUEE_TAIL_PX
        if loop_w > 0 and self._marquee_offset_px >= loop_w:
            self._marquee_offset_px -= loop_w

        # Update positions of existing text items instead of redrawing
        # — much cheaper.
        c = self.filename_canvas
        try:
            items = c.find_withtag("marquee")
        except tk.TclError:
            return
        if len(items) != 2:
            # Out of sync (e.g. a resize just happened). Force a redraw.
            self._redraw_filename()
            return

        ch = c.winfo_height()
        x0 = -int(self._marquee_offset_px)
        x1 = x0 + loop_w
        try:
            c.coords(items[0], x0, ch // 2)
            c.coords(items[1], x1, ch // 2)
        except tk.TclError:
            pass

    # -- Custom titlebar (fullscreen mode only) ------------------------------

    def _build_custom_titlebar(self):
        """
        A thin floating bar with title text + minimize/close buttons.
        Only shown when in fullscreen mode AND the mouse is near the top.
        Built once, placed/forgotten as needed.
        """
        bar_h = 32
        self._titlebar_height = bar_h

        self.custom_titlebar = tk.Frame(
            self, bg="#1a1a1a", height=bar_h,
        )
        # Don't pack now — we use place() to overlay it later.

        # Title text on the left
        tk.Label(
            self.custom_titlebar,
            text="Solenoid Piano — Visualizer",
            font=("Segoe UI", 11),
            fg="#e0e0e0", bg="#1a1a1a",
            padx=12,
        ).pack(side="left", fill="y")

        # Window-control buttons on the right (close, then minimize, so
        # they pack outward in the conventional order from the right edge).
        btn_common = dict(
            font=("Segoe UI", 11, "bold"),
            fg="#e0e0e0", bg="#1a1a1a",
            activebackground="#3a3a3a", activeforeground="#ffffff",
            bd=0, relief="flat", padx=14, pady=2, cursor="hand2",
        )

        close_btn = tk.Button(
            self.custom_titlebar, text="✕",
            command=self._on_close, **btn_common,
        )
        close_btn.pack(side="right", fill="y")
        # Close button gets a red hover specifically (matches OS conventions).
        close_btn.bind("<Enter>",
                       lambda e: close_btn.configure(bg="#c0392b"))
        close_btn.bind("<Leave>",
                       lambda e: close_btn.configure(bg="#1a1a1a"))

        min_btn = tk.Button(
            self.custom_titlebar, text="—",
            command=self._minimize, **btn_common,
        )
        min_btn.pack(side="right", fill="y")
        min_btn.bind("<Enter>",
                     lambda e: min_btn.configure(bg="#3a3a3a"))
        min_btn.bind("<Leave>",
                     lambda e: min_btn.configure(bg="#1a1a1a"))

        # Mouse motion inside the titlebar should keep it visible
        # (without this it'd flicker as you move toward the buttons).
        self.custom_titlebar.bind("<Motion>", self._on_mouse_motion)

    def _show_custom_titlebar(self):
        if self._custom_titlebar_visible:
            return
        # place() over the top of everything else, full width.
        self.custom_titlebar.place(
            x=0, y=0, relwidth=1.0, height=self._titlebar_height,
        )
        self.custom_titlebar.lift()
        self._custom_titlebar_visible = True

    def _hide_custom_titlebar(self):
        if not self._custom_titlebar_visible:
            return
        self.custom_titlebar.place_forget()
        self._custom_titlebar_visible = False

    def _on_mouse_motion(self, event):
        """
        Show the custom titlebar when the cursor is near the top of the
        window in fullscreen mode; hide it otherwise. We translate the
        event coordinates to window-local space because <Motion> on a
        child widget reports event.y relative to that child.
        """
        if not self._fullscreen:
            return

        # Window-local Y of the cursor
        try:
            win_y = self.winfo_pointery() - self.winfo_rooty()
        except tk.TclError:
            return

        # Show whenever the cursor is in the top 40px reveal zone, OR
        # already inside the titlebar itself (so it doesn't snap away
        # when you move toward the close button).
        reveal_zone = 40
        if win_y <= reveal_zone or (
            self._custom_titlebar_visible and win_y <= self._titlebar_height
        ):
            self._show_custom_titlebar()
        else:
            self._hide_custom_titlebar()

    def _minimize(self):
        # overrideredirect windows can't be iconified directly on most
        # platforms; the workaround is to drop overrideredirect, iconify,
        # then restore it on the next Map event.
        if self._fullscreen:
            self.overrideredirect(False)
            self.iconify()
            # When the user un-minimizes, re-apply borderless fullscreen.
            self.bind("<Map>", self._reapply_fullscreen_after_minimize)
        else:
            self.iconify()

    def _reapply_fullscreen_after_minimize(self, event):
        # One-shot: only fire on the first map after a minimize.
        self.unbind("<Map>")
        if self._fullscreen:
            # Restore borderless + screen-sized geometry.
            self.overrideredirect(True)
            self._apply_borderless_fullscreen_geometry()

    # -- Fullscreen toggling ------------------------------------------------

    def _apply_borderless_fullscreen_geometry(self):
        """Size the window to the full screen (work area + taskbar)."""
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        self.geometry(f"{sw}x{sh}+0+0")

    def toggle_fullscreen(self):
        if self._fullscreen:
            self._exit_fullscreen()
        else:
            self._enter_fullscreen()

    def _enter_fullscreen(self):
        if self._fullscreen:
            return
        # Remember current geometry so we can restore it.
        self._pre_fullscreen_geom = self.geometry()
        self._fullscreen = True

        # overrideredirect strips the OS titlebar, borders, and resize
        # handles. The window becomes a plain rectangle we control.
        self.overrideredirect(True)
        self._apply_borderless_fullscreen_geometry()
        self.lift()

        # Don't show the custom titlebar immediately — wait for hover.
        self._hide_custom_titlebar()

    def _exit_fullscreen(self):
        if not self._fullscreen:
            return
        self._fullscreen = False
        self._hide_custom_titlebar()

        # Restore OS decorations and previous geometry.
        self.overrideredirect(False)
        if self._pre_fullscreen_geom:
            self.geometry(self._pre_fullscreen_geom)

    # -- Lifecycle ----------------------------------------------------------

    def _on_close(self):
        # Hide rather than destroy so reopening is cheap.
        # Bail out of fullscreen first so the next show() comes back in
        # a normal window state.
        if self._fullscreen:
            self._exit_fullscreen()
        self.withdraw()

    def show(self):
        self.deiconify()
        self.lift()

    # -- Layout -------------------------------------------------------------

    def _on_resize(self, event):
        self._canvas_w = event.width
        self._canvas_h = event.height
        self._layout_dirty = True

    def _compute_layout(self):
        """Compute key rectangles for an 88-key piano (MIDI 21..108)."""
        w = max(1, self._canvas_w)
        h = max(1, self._canvas_h)

        # Keyboard takes ~22% of canvas height, capped so it doesn't
        # eat the whole window on tall layouts.
        kb_h = max(80, min(int(h * 0.22), 160))
        self._keyboard_top_y = h - kb_h

        white_w = w / VIS_NUM_WHITE
        # Black keys are ~60% white-key width in a real piano
        black_w = white_w * 0.60
        self._white_key_width = white_w
        self._black_key_width = black_w

        # First, lay out white keys left-to-right
        white_x_for_midi: dict[int, float] = {}
        white_idx = 0
        for midi in range(VIS_FIRST_MIDI, VIS_LAST_MIDI + 1):
            if not is_black_key(midi):
                white_x_for_midi[midi] = white_idx * white_w
                white_idx += 1

        # White key rects fill their slot
        rects: dict[int, tuple[int, int, int, int]] = {}
        for midi, x0 in white_x_for_midi.items():
            x1 = x0 + white_w
            rects[midi] = (
                int(x0), int(self._keyboard_top_y),
                int(x1), int(h),
            )

        # Black keys are positioned between adjacent white keys.
        # In each octave: C# sits between C and D, D# between D and E, etc.
        for midi in range(VIS_FIRST_MIDI, VIS_LAST_MIDI + 1):
            if not is_black_key(midi):
                continue
            # The white key just to the left of this black key is midi-1.
            left_white = midi - 1
            if left_white not in white_x_for_midi:
                continue
            left_x = white_x_for_midi[left_white]
            # Center the black key over the boundary between left and right whites
            cx = left_x + white_w
            x0 = cx - black_w / 2
            x1 = cx + black_w / 2
            black_h = int(kb_h * 0.62)
            rects[midi] = (
                int(x0), int(self._keyboard_top_y),
                int(x1), int(self._keyboard_top_y + black_h),
            )

        self._key_rects = rects
        self._layout_dirty = False

    # -- Time sync ----------------------------------------------------------

    def _current_song_us(self) -> int:
        """
        The instantaneous playhead position in song-time microseconds.
        Mirrors the worker's computation but uses whatever values the
        worker has published. When not playing, returns the saved
        pause point (or 0).
        """
        app = self.master_app
        start_mono = app.playback_start_monotonic
        offset_us  = app.playback_time_offset_us

        if app.is_transmitting and start_mono is not None:
            elapsed_s = time.monotonic() - start_mono
            return offset_us + int(elapsed_s * 1_000_000)

        # Not playing — show pause position if any, else 0.
        if app._paused_song_us is not None:
            return app._paused_song_us
        return 0

    # -- Rendering ----------------------------------------------------------

    def _tick(self):
        if not self._running:
            return
        try:
            self._render()
            self._tick_marquee()
        except tk.TclError:
            # Window destroyed mid-render
            return
        self.after(VIS_FRAME_INTERVAL_MS, self._tick)

    def _render(self):
        if self._layout_dirty:
            self._compute_layout()

        c = self.canvas
        c.delete("all")

        w = self._canvas_w
        h = self._canvas_h
        kb_top = self._keyboard_top_y

        # Update header text
        self._update_header()

        # Background grid lines (subtle vertical guides every octave)
        for midi in range(VIS_FIRST_MIDI, VIS_LAST_MIDI + 1):
            if midi % 12 == 0:  # every C
                rect = self._key_rects.get(midi)
                if rect:
                    x = rect[0]
                    c.create_line(x, 0, x, kb_top,
                                  fill=VIS_GUIDELINE, width=1)

        # Falling notes
        playhead_us = self._current_song_us()
        song = self.master_app.song
        if song and song.note_segments:
            self._draw_falling_notes(song.note_segments, playhead_us, kb_top)

        # Red playhead line at the top of the keyboard
        c.create_line(0, kb_top, w, kb_top,
                      fill=VIS_PLAYLINE, width=2)

        # Keyboard (compute active keys from current playhead)
        active_channels = self._active_channels_at(playhead_us)
        self._draw_keyboard(active_channels)

    def _update_header(self):
        """
        Push the latest filename + duration/progress strings into the
        split header. Filename text itself is set via _set_filename_display
        so the marquee state stays consistent.
        """
        song = self.master_app.song
        playhead_us = self._current_song_us()

        def fmt_mmss(seconds: float) -> str:
            # Floor to whole seconds so the display ticks cleanly each
            # second instead of showing fractional rollover.
            total = int(seconds)
            return f"{total // 60}:{total % 60:02d}"

        if song is None:
            display_name = "----------Waiting for Upload----------"
            right_text = "Current Song Progess: 0:00 / 0:00"
        else:
            display_name = strip_midi_extension(song.filename)
            progress_s = playhead_us / 1_000_000
            right_text = (f"Current Song Progress: {fmt_mmss(progress_s)} / "
                          f"{fmt_mmss(song.duration_sec)}")

        self._set_filename_display(display_name)
        # Cheap to call configure() with the same text repeatedly; Tk
        # short-circuits when it hasn't changed.
        self.right_label.configure(text=right_text)

    def _active_channels_at(self, playhead_us: int) -> set[int]:
        """Channels currently sounding at the playhead."""
        song = self.master_app.song
        if not song:
            return set()
        active: set[int] = set()
        # Linear scan is fine for typical song sizes; if note counts get
        # huge, swap for a binary search over starts + interval tree.
        for start_us, end_us, ch in song.note_segments:
            if start_us > playhead_us:
                break
            if end_us > playhead_us:
                active.add(ch)
        return active

    def _draw_falling_notes(self,
                            segments: list[tuple[int, int, int]],
                            playhead_us: int,
                            kb_top: int):
        """
        Note bars descend at VIS_FALL_PX_PER_SEC. A note's bottom edge
        crosses kb_top at exactly its start_us. Bar height encodes its
        duration. Only segments inside the visible window are drawn.
        """
        c = self.canvas
        px_per_us = VIS_FALL_PX_PER_SEC / 1_000_000.0

        # Visible time window: [playhead_us, playhead_us + lookahead]
        lookahead_us = int(VIS_LOOKAHEAD_S * 1_000_000)
        # Also include in-progress notes whose end is still ahead
        # (their bottoms are below kb_top — they're "playing").

        # Binary-search the first segment whose end is after playhead
        # to skip the bulk of past notes cheaply.
        # segments is sorted by start_us, but ends aren't monotonic;
        # we still need to scan from the first plausible start.
        starts = [s[0] for s in segments]
        # Earliest start we'd care about: a note that started up to
        # `lookahead` ago could still be on screen if it's long.
        first_idx = bisect.bisect_left(starts, playhead_us - lookahead_us)
        # And we stop once start > playhead + lookahead (note hasn't
        # fallen into view yet).
        last_start = playhead_us + lookahead_us

        for i in range(first_idx, len(segments)):
            start_us, end_us, ch = segments[i]
            if start_us > last_start:
                break
            if end_us < playhead_us:
                continue  # already finished

            midi = ch + MIDI_NOTE_LOW
            rect = self._key_rects.get(midi)
            if rect is None:
                continue

            kx0, _, kx1, _ = rect

            # Pixel positions: bar bottom at kb_top when start_us == playhead_us
            bar_bottom = kb_top + (playhead_us - start_us) * px_per_us
            bar_top    = kb_top + (playhead_us - end_us)   * px_per_us

            # Skip if entirely off-screen
            if bar_bottom < 0 or bar_top > kb_top:
                continue

            # Clamp the visible portion
            draw_top    = max(0, bar_top)
            draw_bottom = min(kb_top, bar_bottom)
            if draw_bottom <= draw_top:
                continue

            # Visual style: green body with darker outline; if the note
            # is currently playing (its bottom is at/below kb_top and
            # top is above it... but we clip at kb_top), the bar will
            # appear to "lock" at the keyboard — this matches the
            # reference image where notes turn solid green at the line.
            # Keep black-key notes a touch narrower for visual clarity.
            inset = 2
            x0 = kx0 + inset
            x1 = kx1 - inset
            if x1 - x0 < 2:
                x0, x1 = kx0, kx1

            c.create_rectangle(
                x0, draw_top, x1, draw_bottom,
                fill=VIS_NOTE_GREEN_BODY,
                outline=VIS_NOTE_GREEN_OUTLINE,
                width=1,
            )

    def _draw_keyboard(self, active_channels: set[int]):
        c = self.canvas

        # Pass 1: white keys
        for midi in range(VIS_FIRST_MIDI, VIS_LAST_MIDI + 1):
            if is_black_key(midi):
                continue
            rect = self._key_rects.get(midi)
            if not rect:
                continue
            x0, y0, x1, y1 = rect
            ch = midi - MIDI_NOTE_LOW
            fill = VIS_KEY_ACTIVE if ch in active_channels else VIS_WHITE_KEY
            c.create_rectangle(x0, y0, x1, y1,
                               fill=fill, outline=VIS_KEY_BORDER, width=1)

        # Pass 2: black keys (drawn on top)
        for midi in range(VIS_FIRST_MIDI, VIS_LAST_MIDI + 1):
            if not is_black_key(midi):
                continue
            rect = self._key_rects.get(midi)
            if not rect:
                continue
            x0, y0, x1, y1 = rect
            ch = midi - MIDI_NOTE_LOW
            fill = VIS_KEY_ACTIVE if ch in active_channels else VIS_BLACK_KEY
            c.create_rectangle(x0, y0, x1, y1,
                               fill=fill, outline=VIS_KEY_BORDER, width=1)


# ---------------------------------------------------------------------------
# GUI Application
# ---------------------------------------------------------------------------

class SolenoidPianoApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Solenoid Piano Controller")
        self.geometry("1100x820")
        self.minsize(750, 620)

        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")
        self.configure(fg_color=WINDOW_BG)

        self.song: SongData | None = None
        self.is_transmitting = False
        self.is_scanning_folder = False
        self.folder_view_mode = "grid"
        self.current_folder_path: Path | None = None
        self.folder_refresh_request_id = 0
        self.selected_folder_song: SongData | None = None
        self.selected_folder_song_path: Path | None = None
        self.folder_item_widgets: dict[Path, list[ctk.CTkBaseClass]] = {}
        self.folder_item_base_colors: dict[Path, str] = {}
        self.folder_selected_color = "#F4B06A"
        self.folder_sort_field = "Filename"
        self.folder_sort_order = "A-Z"

        self._paused_song_us: int | None = None

        # --- Playback clock state, exposed for the visualizer ------------
        # Set when the worker sends CMD_START. Used together with
        # playback_time_offset_us to compute the current song-time.
        # Cleared (set to None) when not actively playing.
        self.playback_start_monotonic: float | None = None
        self.playback_time_offset_us: int = 0

        self._build_ui()

        # Visualizer window — created once, kept alive, hidden on close.
        self.visualizer: VisualizerWindow | None = None
        self.after(200, self._open_visualizer)

    # -----------------------------------------------------------------------
    # Visualizer plumbing
    # -----------------------------------------------------------------------

    def _open_visualizer(self):
        if self.visualizer is None:
            self.visualizer = VisualizerWindow(self)
        else:
            self.visualizer.show()

    # -----------------------------------------------------------------------
    # UI construction
    # -----------------------------------------------------------------------

    def _build_ui(self):
        self.tabview = ctk.CTkTabview(self, fg_color=WINDOW_BG)
        try:
            self.tabview.configure(
                segmented_button_selected_color=ACCENT,
                segmented_button_selected_hover_color=ACCENT_HOVER,
            )
        except Exception:
            pass
        self.tabview.pack(fill="both", expand=True, padx=10, pady=10)

        self.player_tab = self.tabview.add("Single Song")
        self.folder_tab = self.tabview.add("Folder View")

        try:
            self.tabview.configure(command=self._on_tab_changed)
        except Exception:
            pass

        self._build_single_song_tab(self.player_tab)
        self._build_folder_tab(self.folder_tab)

    def _build_single_song_tab(self, parent):
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

        self.show_vis_btn = ctk.CTkButton(
            file_frame, text="Show Visualizer", width=130,
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            command=self._open_visualizer)
        self.show_vis_btn.pack(side="right", padx=10, pady=10)

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

        serial_frame = ctk.CTkFrame(parent, fg_color=SECTION_BG)
        serial_frame.pack(fill="x", padx=15, pady=5)

        serial_inner = ctk.CTkFrame(serial_frame, fg_color="transparent")
        serial_inner.pack(fill="x", padx=10, pady=8)

        ctk.CTkLabel(serial_inner, text="Serial Port:",
                     font=ctk.CTkFont(size=13)).pack(side="left", padx=(0, 5))

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

        btn_frame = ctk.CTkFrame(parent, fg_color=SECTION_BG)
        btn_frame.pack(fill="x", padx=15, pady=5)

        btn_inner = ctk.CTkFrame(btn_frame, fg_color="transparent")
        btn_inner.pack(pady=8)

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

        self.progress = ctk.CTkProgressBar(parent, progress_color=ACCENT)
        self.progress.pack(fill="x", padx=15, pady=(2, 5))
        self.progress.set(0)

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

        self.status_label = ctk.CTkLabel(
            parent, text="Ready — load a MIDI file to begin",
            font=ctk.CTkFont(size=12), anchor="w")
        self.status_label.pack(fill="x", padx=15, pady=(0, 10))

        self._refresh_ports()

    def _build_folder_tab(self, parent):
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
        note_part   = name[:i].upper()
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
        self._request_folder_reparse(folder_path)

    def _parse_folder_songs(self, folder_path: Path) -> list[tuple[Path, SongData | None]]:
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
        return songs_with_meta

    def _request_folder_reparse(self, folder_path: Path):
        self.current_folder_path = folder_path
        self.is_scanning_folder = True
        self.folder_browse_btn.configure(state="disabled")
        self.folder_refresh_request_id += 1
        request_id = self.folder_refresh_request_id

        threading.Thread(
            target=self._folder_reparse_worker,
            args=(folder_path, request_id),
            daemon=True,
        ).start()

    def _folder_reparse_worker(self, folder_path: Path, request_id: int):
        songs_with_meta = self._parse_folder_songs(folder_path)
        self.after(0, self._on_folder_reparse_complete,
                   folder_path, songs_with_meta, request_id)

    def _on_folder_reparse_complete(self, folder_path: Path,
                                    songs_with_meta: list[tuple[Path, SongData | None]],
                                    request_id: int):
        if request_id != self.folder_refresh_request_id:
            return

        self.current_folder_path = folder_path
        self.is_scanning_folder = False
        self.folder_browse_btn.configure(state="normal")
        self._render_folder_files(songs_with_meta)

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

    def _set_folder_view_mode(self, mode: str):
        if mode not in ("grid", "list"):
            return
        self.folder_view_mode = mode
        self._update_folder_view_buttons()
        if self.current_folder_path is not None and not self.is_scanning_folder:
            self.folder_summary_label.configure(
                text="Refreshing MIDI files...",
                text_color="black",
            )
            self._request_folder_reparse(self.current_folder_path)

    def _on_folder_sort_changed(self, _value: str):
        self.folder_sort_field = self.sort_field_combo.get()
        self.folder_sort_order = self.sort_order_combo.get()
        if self.current_folder_path is not None and not self.is_scanning_folder:
            self.folder_summary_label.configure(
                text="Refreshing MIDI files...",
                text_color="black",
            )
            self._request_folder_reparse(self.current_folder_path)

    def _on_tab_changed(self, *_args):
        if self.tabview.get() != "Folder View":
            return
        if self.current_folder_path is None or self.is_scanning_folder:
            return

        self.folder_summary_label.configure(
            text="Refreshing MIDI files...",
            text_color="black",
        )
        self._request_folder_reparse(self.current_folder_path)

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

    def _render_folder_files(self, songs_with_meta: list[tuple[Path, SongData | None]]):
        folder_path = self.current_folder_path
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

        sorted_songs = self._get_sorted_folder_songs(songs_with_meta)
        if self.folder_view_mode == "list":
            self._populate_song_list(sorted_songs)
        else:
            self._populate_song_grid(sorted_songs)

        if self.selected_folder_song_path and self.selected_folder_song_path in self.folder_item_widgets:
            selected_song = None
            for midi_file, song in songs_with_meta:
                if midi_file == self.selected_folder_song_path:
                    selected_song = song
                    break
            self.selected_folder_song = selected_song

            for widget in self.folder_item_widgets[self.selected_folder_song_path]:
                widget.configure(fg_color=self.folder_selected_color)

            if selected_song is None:
                self.folder_overlay_song_label.configure(
                    text=f"Selected: {self.selected_folder_song_path.name} (parse failed)")
                self.folder_overlay_status.configure(
                    text="This file could not be parsed. Re-select another song.")
                self.folder_transmit_btn.configure(state="disabled")
            else:
                self.folder_overlay_song_label.configure(text=f"Selected: {selected_song.filename}")
                self.folder_overlay_status.configure(
                    text=f"Ready to transmit {selected_song.filename}")
                self.folder_transmit_btn.configure(state="normal")

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
        for i, wt in enumerate([6, 2, 2, 2, 2, 3]):
            header.grid_columnconfigure(i, weight=wt)

        columns = ["Filename", "Composer", "Duration", "Events", "Tempo", "Key Range"]
        anchors = ["w", "w", "e", "e", "e", "w"]
        for i, col in enumerate(columns):
            ctk.CTkLabel(
                header,
                text=col,
                anchor=anchors[i],
                font=ctk.CTkFont(size=12, weight="bold"),
            ).grid(row=0, column=i, padx=10, pady=8, sticky=anchors[i])
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
            for j, wt in enumerate([6, 2, 2, 2, 2, 3]):
                row.grid_columnconfigure(j, weight=wt)

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

            values = [midi_file.name, composer_text, duration_text, events_text, tempo_text, key_range_text]
            anchors = ["w", "w", "e", "e", "e", "w"]
            for j, val in enumerate(values):
                ctk.CTkLabel(
                    row,
                    text=val,
                    anchor=anchors[j],
                    font=ctk.CTkFont(size=12, weight="bold" if j == 0 else "normal"),
                ).grid(row=0, column=j, padx=10, pady=8, sticky=anchors[j])

            row_widgets = [row] + list(row.winfo_children())
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

        self._paused_song_us = None
        # Reset playback clock state so visualizer shows song at t=0
        self.playback_start_monotonic = None
        self.playback_time_offset_us = 0

        self.file_label.configure(
            text=Path(path).name,
            text_color=ACCENT,
            font=ctk.CTkFont(size=13, weight="bold"),
        )
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

        if self._paused_song_us is not None:
            timestamps = [e.timestamp_us for e in self.song.events]
            start_index = bisect.bisect_left(timestamps, self._paused_song_us)
            if start_index >= len(self.song.events):
                start_index = 0
                time_offset_us = 0
            else:
                time_offset_us = self._paused_song_us
        else:
            start_index = 0
            time_offset_us = 0

        self._paused_song_us = None

        self.is_transmitting = True
        self.transmit_btn.configure(state="disabled")
        self.browse_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.progress.set(0)

        # Pre-set the offset so the visualizer header reflects our start
        # point even before CMD_START fires.
        self.playback_time_offset_us = time_offset_us
        self.playback_start_monotonic = None  # set when CMD_START is sent

        threading.Thread(
            target=self._transmit_worker,
            args=(port, baud, start_index, time_offset_us),
            daemon=True,
        ).start()

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

        self.song = song
        self._paused_song_us = None
        self.playback_time_offset_us = 0
        self.playback_start_monotonic = None

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
                self._show_error_safe("Connection Failed", "No response from MCU.")
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
                self._update_folder_progress_safe(sent / max(1, total))

                if not started and (sent >= BATCH_SIZE * PRIME_BATCHES or sent >= total):
                    send_packet(ser, build_command_packet(CMD_START))
                    if wait_for_ack(ser) is None:
                        self._show_error_safe("Playback Error", "No ACK for START.")
                        return
                    started = True
                    self.playback_time_offset_us = 0
                    self.playback_start_monotonic = time.monotonic()
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
                self.playback_time_offset_us = 0
                self.playback_start_monotonic = time.monotonic()

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

    def _transmit_worker(self, port: str, baud: int,
                          start_index: int = 0, time_offset_us: int = 0):
        ser: serial.Serial | None = None

        def current_song_position_us() -> int:
            if self.playback_start_monotonic is None:
                return time_offset_us
            elapsed_us = int(
                (time.monotonic() - self.playback_start_monotonic) * 1_000_000)
            return time_offset_us + elapsed_us

        def handle_stop():
            pos = current_song_position_us()
            self._paused_song_us = pos
            try:
                send_packet(ser, build_command_packet(CMD_STOP))
                wait_for_ack(ser)
            except Exception:
                pass
            self._set_status_safe(
                f"Stopped at {pos / 1_000_000:.2f}s — "
                f"press Upload & Play to resume")

        try:
            self._set_status_safe(f"Connecting to {port}...")
            ser = serial.Serial(port, baud, timeout=ACK_TIMEOUT_S)
            time.sleep(POST_OPEN_SETTLE_S)
            drain_input(ser)

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

            send_packet(ser, build_command_packet(CMD_STOP))
            free = wait_for_ack(ser)
            if free is None:
                self._show_error_safe("Transmission Error",
                                      "Failed to clear MCU before upload.")
                return

            total = self.song.num_events
            sent  = start_index
            started = False

            while sent < total:
                if not self.is_transmitting:
                    handle_stop()
                    return

                raw_batch = self.song.events[sent:sent + BATCH_SIZE]

                if time_offset_us > 0:
                    batch = [SolenoidEvent(
                        timestamp_us=max(0, ev.timestamp_us - time_offset_us),
                        channel=ev.channel,
                        event_type=ev.event_type,
                        velocity=ev.velocity,
                    ) for ev in raw_batch]
                else:
                    batch = raw_batch

                batch_len = len(batch)

                while free < batch_len:
                    if not self.is_transmitting:
                        handle_stop()
                        return
                    time.sleep(BACKPRESSURE_POLL_S)
                    send_packet(ser, build_command_packet(CMD_PING))
                    free = wait_for_ack(ser)
                    if free is None:
                        self._show_error_safe(
                            "Transmission Error",
                            "Lost contact with MCU during streaming.")
                        return

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
                    return

                sent += batch_len
                denom = max(1, total - start_index)
                self._update_progress_safe((sent - start_index) / denom)

                if not started and (
                    (sent - start_index) >= BATCH_SIZE * PRIME_BATCHES
                    or sent >= total
                ):
                    send_packet(ser, build_command_packet(CMD_START))
                    if wait_for_ack(ser) is None:
                        self._show_error_safe("Playback Error",
                                              "No ACK for START.")
                        return
                    started = True
                    # Publish playback clock — visualizer reads this.
                    self.playback_start_monotonic = time.monotonic()
                    self._set_status_safe(
                        f"Streaming & playing: {sent:,}/{total:,} events")
                else:
                    prefix = "Streaming" if started else "Priming"
                    self._set_status_safe(
                        f"{prefix}: {sent:,}/{total:,} events")

                time.sleep(INTER_BATCH_DELAY_S)

            send_packet(ser, build_command_packet(CMD_EOS))
            wait_for_ack(ser)

            if not started:
                send_packet(ser, build_command_packet(CMD_START))
                wait_for_ack(ser)
                started = True
                self.playback_start_monotonic = time.monotonic()

            self._update_progress_safe(1.0)
            self._set_status_safe(
                f"Playing: {self.song.filename} "
                f"({self.song.duration_sec:.1f}s, "
                f"{self.song.tempo_bpm:.0f} BPM)")

            song_remaining_us = self.song.duration_us - time_offset_us
            deadline = (self.playback_start_monotonic
                        + song_remaining_us / 1_000_000
                        + 0.5)

            while time.monotonic() < deadline:
                if not self.is_transmitting:
                    handle_stop()
                    return
                time.sleep(0.1)

            self._paused_song_us = None
            self._set_status_safe(f"Finished: {self.song.filename}")

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
        if self.is_transmitting:
            self._set_status("Can't test a note while a song is playing — "
                             "stop the song first.")
            return

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
        if not self.is_transmitting:
            return
        self.is_transmitting = False
        self.stop_btn.configure(state="disabled")
        self._set_status("Stopping...")

    def _send_folder_stop(self):
        if not self.is_transmitting:
            return
        self.is_transmitting = False
        self.folder_stop_btn.configure(state="disabled")
        self._set_folder_status_safe("Stopping...")

    # -----------------------------------------------------------------------
    # Thread-safe UI helpers
    # -----------------------------------------------------------------------

    def _set_status_safe(self, text: str):
        self.after(0, lambda: self.status_label.configure(text=text))

    def _update_progress_safe(self, fraction: float):
        f = max(0.0, min(1.0, fraction))
        self.after(0, lambda: self.progress.set(f))

    def _show_error_safe(self, title: str, message: str):
        self.after(0, lambda: messagebox.showerror(title, message))

    def _set_folder_status_safe(self, text: str):
        if hasattr(self, "folder_overlay_status"):
            self.after(0, lambda: self.folder_overlay_status.configure(text=text))

    def _update_folder_progress_safe(self, value: float):
        if hasattr(self, "folder_progress"):
            f = max(0.0, min(1.0, value))
            self.after(0, lambda: self.folder_progress.set(f))

    def _finish_folder_transmit_safe(self):
        def _finish():
            self.is_transmitting = False
            self.playback_start_monotonic = None
            if hasattr(self, "folder_transmit_btn"):
                self.folder_transmit_btn.configure(
                    state="normal" if self.selected_folder_song is not None else "disabled")
            if hasattr(self, "folder_stop_btn"):
                self.folder_stop_btn.configure(state="disabled")
            if hasattr(self, "folder_browse_btn"):
                self.folder_browse_btn.configure(state="normal")
        self.after(0, _finish)

    def _finish_transmit_safe(self):
        def _finish():
            self.is_transmitting = False
            self.playback_start_monotonic = None
            self.transmit_btn.configure(state="normal")
            self.browse_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
        self.after(0, _finish)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app = SolenoidPianoApp()
    app.mainloop()