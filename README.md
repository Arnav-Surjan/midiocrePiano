# midiocrePiano

Desktop-timed MIDI playback for a 12-solenoid piano driven by PCA9685.

## Architecture

- Desktop (Python): parses MIDI, handles tempo changes, builds timestamped command schedule, and streams commands over serial.
- Microcontroller (Arduino): only executes channel commands (`ON`, `OFF`, `ALL_OFF`).

This moves all timing and parsing complexity off the microcontroller and makes playback easier to debug and improve.

## Serial Protocol (line_serial_v1)

One ASCII command per line:

- `ON <channel>`: turn channel on (`0` to `11`)
- `OFF <channel>`: turn channel off (`0` to `11`)
- `ALL_OFF`: turn all channels off
- `PING`: optional health check, returns `PONG`

Example:

```text
ON 4
OFF 4
ALL_OFF
```

## Firmware

Flash [SolenoidController/solenoid_controller.ino](SolenoidController/solenoid_controller.ino) to your board.

For TI C2000 LAUNCHXL-F28P55X, use [SolenoidController/c2000_solenoid_controller.c](SolenoidController/c2000_solenoid_controller.c).

Default pin mux in that firmware:

- SCIA TX: GPIO29
- SCIA RX: GPIO28
- I2CA SDA: GPIO32
- I2CA SCL: GPIO33

Notes:

- Uses I2C PCA9685 at address `0x40`
- Uses serial baud rate `115200`
- Expects desktop-side command timing

### C2000 + PCA9685 Compatibility

- Yes, the LAUNCHXL-F28P55X can control PCA9685 over I2C.
- Keep the I2C pull-ups at 3.3V when connecting to the C2000.
- If your PCA9685 breakout is pulled up to 5V by default, move pull-ups to 3.3V or use level shifting.
- Solenoids must still be driven through proper transistor/MOSFET driver stages, not directly from PCA9685 pins.

## Desktop Setup

From [App](App):

```bash
pip install mido pyserial requests beautifulsoup4
```

## Convert MIDI to a Playback Package

```bash
python App/midi_to_solenoid.py path/to/song.mid
```

Output includes:

- `events`: note on/off with absolute `time_ms`
- `note_to_channel`: MIDI note mapping used
- `serial_schedule`: timestamped serial commands

## Real-Time Streaming Playback

```bash
python App/midi_stream_player.py path/to/song.mid --port /dev/tty.usbmodemXXXX --baud 115200
```

Optional:

- `--startup-delay 2.0` to adjust post-connect wait time

## Note Mapping

Default mapping in [App/midi_to_solenoid.py](App/midi_to_solenoid.py) is one octave:

- MIDI `60..71` -> channels `0..11`

If your wiring or key range is different, edit `NOTE_TO_CHANNEL`.
