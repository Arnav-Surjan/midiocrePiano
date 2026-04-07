"""Desktop-side serial streamer for MIDI playback.

This script does all timing on the desktop and sends lightweight commands to
microcontroller firmware:
  ON <channel>
  OFF <channel>
  ALL_OFF
"""

from __future__ import annotations

import argparse
import importlib
import time
from pathlib import Path
from typing import Iterable

from midi_to_solenoid import midi_to_playback_package


def _send_line(ser: object, line: str) -> None:
    ser.write((line + "\n").encode("ascii"))


def stream_schedule(
    serial_port: str,
    midi_file: str | Path,
    baud_rate: int = 115200,
    startup_delay_s: float = 2.0,
) -> None:
    """Stream MIDI-derived commands to the controller at precise intervals."""

    package = midi_to_playback_package(midi_file)
    schedule = package["serial_schedule"]
    if not isinstance(schedule, list):
        raise ValueError("Invalid playback package: serial_schedule missing")

    try:
        serial_module = importlib.import_module("serial")
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "pyserial is required. Install with: pip install pyserial"
        ) from exc

    with serial_module.Serial(serial_port, baud_rate, timeout=0.1) as ser:
        # Give boards like UNO/ESP32 time to reset and boot after opening serial.
        time.sleep(startup_delay_s)

        _send_line(ser, "ALL_OFF")

        start = time.perf_counter()
        for entry in schedule:
            if not isinstance(entry, dict):
                continue

            time_ms = entry.get("time_ms")
            command = entry.get("command")
            if not isinstance(time_ms, int) or not isinstance(command, str):
                continue

            target_time = start + (time_ms / 1000.0)
            while True:
                now = time.perf_counter()
                remaining = target_time - now
                if remaining <= 0:
                    break
                # Sleep in small slices for better timing precision.
                time.sleep(min(remaining, 0.005))

            _send_line(ser, command)

        _send_line(ser, "ALL_OFF")


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stream MIDI to solenoid controller")
    parser.add_argument("midi", type=Path, help="Path to input MIDI file")
    parser.add_argument("--port", required=True, help="Serial port, e.g. /dev/tty.usbmodemXXXX")
    parser.add_argument("--baud", type=int, default=115200, help="Serial baud rate")
    parser.add_argument(
        "--startup-delay",
        type=float,
        default=2.0,
        help="Seconds to wait after opening serial port",
    )
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    stream_schedule(
        serial_port=args.port,
        midi_file=args.midi,
        baud_rate=args.baud,
        startup_delay_s=args.startup_delay,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
