import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path
from typing import Dict, List
import threading
import json
import subprocess
import sys

from midi_uploader import (
    MID_EXT,
    MIDI_FILE_GLOB,
    MIDI_EXTENSION_LABEL,
    MIDI_SUFFIXES,
    download_midi_file,
    search_bitmidi,
    search_local_midi,
    search_midiworld,
)
from midi_to_solenoid import midi_to_solenoid_events

SELECT_FILE_FIRST_MSG = "Please select a MIDI file first."
VIS_SCRIPT_PASS_LINE = "        pass"
READY_STATUS_MSG = "Ready."
APP_DIR = Path(__file__).resolve().parent
STREAMING_FAILED_MSG = "Playback failed."


class MidiControllerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("MIDI Controller App")
        self.geometry("920x640")
        self.resizable(True, True)

        self.selected_file: Path | None = None
        self.local_directory: Path | None = None
        self.search_results: List[Dict[str, str]] = []
        self.last_solenoid_package: Dict[str, object] | None = None
        self.visualiser_process: subprocess.Popen[str] | None = None
        self.streaming_process: subprocess.Popen[str] | None = None
        self.playback_stop_requested = False

        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.container = tk.Frame(self)
        self.container.pack(fill=tk.BOTH, expand=True)

        self.upload_screen = tk.Frame(self.container)
        self.process_screen = tk.Frame(self.container)
        self.play_screen = tk.Frame(self.container)

        for frame in (self.upload_screen, self.process_screen, self.play_screen):
            frame.place(relx=0, rely=0, relwidth=1, relheight=1)

        self._build_upload_screen()
        self._build_process_screen()
        self._build_play_screen()

        self.show_screen(self.upload_screen)

    def show_screen(self, screen: tk.Frame) -> None:
        screen.lift()

    def _build_upload_screen(self) -> None:
        frame = self.upload_screen

        self.file_label = tk.Label(
            frame,
            text="No MIDI file selected.",
            padx=20,
            pady=10,
            font=("TkDefaultFont", 18, "bold"),
        )
        self.file_label.pack()

        self.to_process_button = tk.Button(
            frame,
            text="Process Song",
            command=self.open_process_screen,
            bg="white",
            fg="black",
        )

        self.upload_button = tk.Button(frame, text="Upload MIDI File", command=self.select_file)
        self.upload_button.pack(pady=(5, 5))

        tk.Label(frame, text="OR").pack(pady=(5, 5))

        search_frame = tk.Frame(frame)
        search_frame.pack(padx=10, pady=(10, 5), fill=tk.X)

        self.search_entry = tk.Entry(search_frame, width=40)
        self.search_entry.pack(side=tk.LEFT, padx=(0, 5), fill=tk.X, expand=True)

        self.search_source = tk.StringVar(value="local")
        source_dropdown = tk.OptionMenu(search_frame, self.search_source, "local", "midiworld", "bitmidi")
        source_dropdown.pack(side=tk.LEFT)

        self.local_folder_button = tk.Button(search_frame, text="Choose Folder", command=self.choose_local_directory)
        self.local_folder_button.pack(side=tk.LEFT, padx=(5, 0))

        search_button = tk.Button(search_frame, text="Search", command=self.perform_search)
        search_button.pack(side=tk.LEFT, padx=(5, 0))

        self.local_dir_label = tk.Label(frame, text="Local folder: not selected", anchor="w")
        self.local_dir_label.pack(padx=10, pady=(0, 5), fill=tk.X)

        self.search_source.trace_add("write", self._on_source_change)
        self.search_entry.bind("<Return>", self.perform_search)

        self.search_placeholder = "Search for a MIDI file"
        self.search_has_placeholder = False
        self._set_search_placeholder()
        self.search_entry.bind("<FocusIn>", self._on_search_focus_in)
        self.search_entry.bind("<FocusOut>", self._on_search_focus_out)

        self.results_listbox = tk.Listbox(frame, width=60, height=11)
        self.results_listbox.bind("<<ListboxSelect>>", self._on_result_select)

        self.download_button = tk.Button(
            frame,
            text="Download Selected",
            command=self.download_selected_result,
            state=tk.DISABLED,
        )

        self.results_visible = False

    def _build_process_screen(self) -> None:
        frame = self.process_screen

        top_bar = tk.Frame(frame)
        top_bar.pack(fill=tk.X, padx=10, pady=(10, 0))
        tk.Button(top_bar, text="<- Back", command=self.go_to_upload_screen).pack(side=tk.LEFT)

        tk.Label(frame, text="Process Song", font=("TkDefaultFont", 18, "bold")).pack(pady=(20, 10))

        self.process_file_label = tk.Label(frame, text="", font=("TkDefaultFont", 12))
        self.process_file_label.pack(pady=(0, 10))

        self.process_status_label = tk.Label(frame, text="Ready to process.")
        self.process_status_label.pack(pady=(0, 10))

        self.process_progress = ttk.Progressbar(frame, mode="indeterminate", length=320)
        self.process_progress.pack(pady=(0, 10))

        self.process_details_label = tk.Label(frame, text="", wraplength=620, justify=tk.LEFT)
        self.process_details_label.pack(pady=(0, 20))

        actions = tk.Frame(frame)
        actions.pack(pady=(0, 20))

        self.reprocess_button = tk.Button(actions, text="Reprocess", command=self.start_processing)
        self.reprocess_button.pack(side=tk.LEFT, padx=5)

        self.to_play_button = tk.Button(actions, text="Go To Play", command=self.open_play_screen, state=tk.DISABLED)
        self.to_play_button.pack(side=tk.LEFT, padx=5)

    def _build_play_screen(self) -> None:
        frame = self.play_screen

        top_bar = tk.Frame(frame)
        top_bar.pack(fill=tk.X, padx=10, pady=(10, 0))
        tk.Button(top_bar, text="<- Back", command=self.go_to_upload_screen).pack(side=tk.LEFT)

        tk.Label(frame, text="Play Song", font=("TkDefaultFont", 18, "bold")).pack(pady=(20, 10))

        self.play_file_label = tk.Label(frame, text="No file selected", font=("TkDefaultFont", 12))
        self.play_file_label.pack(pady=(0, 12))

        port_frame = tk.Frame(frame)
        port_frame.pack(pady=4)
        tk.Label(port_frame, text="Serial Port:").pack(side=tk.LEFT, padx=(0, 8))
        self.port_entry = tk.Entry(port_frame, width=34)
        self.port_entry.insert(0, "/dev/tty.usbmodem")
        self.port_entry.pack(side=tk.LEFT)

        baud_frame = tk.Frame(frame)
        baud_frame.pack(pady=4)
        tk.Label(baud_frame, text="Baud Rate:").pack(side=tk.LEFT, padx=(0, 18))
        self.baud_entry = tk.Entry(baud_frame, width=12)
        self.baud_entry.insert(0, "115200")
        self.baud_entry.pack(side=tk.LEFT)

        startup_frame = tk.Frame(frame)
        startup_frame.pack(pady=4)
        tk.Label(startup_frame, text="Startup Delay (s):").pack(side=tk.LEFT, padx=(0, 6))
        self.startup_entry = tk.Entry(startup_frame, width=12)
        self.startup_entry.insert(0, "2.0")
        self.startup_entry.pack(side=tk.LEFT)

        self.play_button = tk.Button(frame, text="Start Streaming", command=self.start_streaming)
        self.play_button.pack(pady=(18, 8))

        self.stop_button = tk.Button(frame, text="Stop Streaming", command=self.stop_streaming, state=tk.DISABLED)
        self.stop_button.pack(pady=(0, 8))

        self.play_progress = ttk.Progressbar(frame, mode="indeterminate", length=320)
        self.play_progress.pack(pady=(0, 10))

        self.play_status_label = tk.Label(frame, text=READY_STATUS_MSG)
        self.play_status_label.pack(pady=(0, 6))

        self.play_details_label = tk.Label(
            frame,
            text="This will stream ON/OFF commands to the microcontroller in real-time.",
            wraplength=620,
            justify=tk.LEFT,
        )
        self.play_details_label.pack(pady=(0, 10))

    def select_file(self) -> None:
        file_path = filedialog.askopenfilename(
            title="Select a MIDI file",
            filetypes=[("MIDI files", MIDI_FILE_GLOB)],
        )

        if not file_path:
            return

        path = Path(file_path)
        if path.suffix.lower() not in MIDI_SUFFIXES:
            messagebox.showerror("Invalid File", f"Please choose a file with a {MIDI_EXTENSION_LABEL} extension.")
            return

        self._set_selected_file(path)

    def _set_selected_file(self, path: Path) -> None:
        self.selected_file = path
        self.file_label.config(text=f"Selected file: {path.name}")
        self._show_process_button()
        self.process_file_label.config(text=f"Selected file: {path.name}")
        self.play_file_label.config(text=f"Selected file: {path.name}")

    def _show_process_button(self) -> None:
        if self.selected_file is None:
            return

        if not self.to_process_button.winfo_ismapped():
            self.to_process_button.pack(pady=(10, 20), before=self.upload_button)

    def perform_search(self, event=None) -> None:
        query = self.search_entry.get().strip()
        if self.search_has_placeholder or query == self.search_placeholder:
            query = ""

        source = self.search_source.get()
        if source not in {"local", "midiworld", "bitmidi"}:
            messagebox.showerror("Search Error", "Invalid search source selected.")
            return

        if source in {"midiworld", "bitmidi"} and not query:
            messagebox.showinfo("Search", "Please enter a search term.")
            return

        try:
            if source == "local":
                if self.local_directory is None:
                    messagebox.showinfo("Local Folder", "Choose a local folder first.")
                    self.choose_local_directory()
                    if self.local_directory is None:
                        return
                search_results = search_local_midi(query, self.local_directory)
            elif source == "midiworld":
                search_results = search_midiworld(query)
            else:
                search_results = search_bitmidi(query)
        except Exception as exc:
            messagebox.showerror("Search Error", f"Could not search {source}: {exc}")
            return

        self._set_search_results(search_results)

        if not self.search_results:
            if source == "local":
                messagebox.showinfo("No Results", "No MIDI files found in that folder for this filter.")
            else:
                messagebox.showinfo("No Results", "No MIDI files found for that search.")

    def _set_search_results(self, search_results: List[Dict[str, str]]) -> None:
        self.results_listbox.delete(0, tk.END)
        self.search_results.clear()

        for item in search_results:
            self.search_results.append(item)
            self.results_listbox.insert(tk.END, item["title"])

        self._show_results_widgets()
        self.results_listbox.selection_clear(0, tk.END)
        self.download_button.config(state=tk.DISABLED)

    def _show_results_widgets(self) -> None:
        if not self.results_visible:
            self.results_listbox.pack(padx=10, pady=(5, 0), fill=tk.BOTH, expand=True)
            self.download_button.pack(pady=(5, 10))
            self.results_visible = True

    def _on_result_select(self, event) -> None:
        if self.results_listbox.curselection():
            self.download_button.config(state=tk.NORMAL)
        else:
            self.download_button.config(state=tk.DISABLED)

    def download_selected_result(self) -> None:
        if not self.search_results:
            messagebox.showinfo("Download", "Please run a search and select a result first.")
            return

        selection = self.results_listbox.curselection()
        if not selection:
            messagebox.showinfo("Download", "Please select a MIDI file from the list.")
            return

        item = self.search_results[selection[0]]
        local_path = item.get("local_path")
        if local_path:
            selected_path = Path(local_path)
            if not selected_path.exists():
                messagebox.showerror("Load Error", "Selected local MIDI file no longer exists.")
                return
            self._set_selected_file(selected_path)
            return

        download_url = item.get("download_url")
        if not download_url:
            messagebox.showerror("Download Error", "No download URL available for the selected item.")
            return

        suggested_name = item.get("filename") or f"{item['title']}{MID_EXT}"
        save_path = filedialog.asksaveasfilename(
            parent=self,
            title="Save MIDI file",
            defaultextension=MID_EXT,
            initialfile=suggested_name,
            filetypes=[("MIDI files", MIDI_FILE_GLOB)],
        )

        if not save_path:
            return

        self.download_button.config(state=tk.DISABLED)

        def do_download() -> None:
            error: Exception | None = None
            try:
                download_midi_file(download_url, save_path)
            except Exception as exc:
                error = exc

            def on_complete() -> None:
                if error is not None:
                    messagebox.showerror("Download Error", f"Could not download MIDI file: {error}")
                else:
                    self._set_selected_file(Path(save_path))
                    messagebox.showinfo("Download Complete", "MIDI file downloaded and loaded successfully.")

                if self.search_results and self.results_listbox.curselection():
                    self.download_button.config(state=tk.NORMAL)
                else:
                    self.download_button.config(state=tk.DISABLED)

            self.after(0, on_complete)

        threading.Thread(target=do_download, daemon=True).start()

    def _set_search_placeholder(self) -> None:
        self.search_entry.delete(0, tk.END)
        self.search_entry.insert(0, self.search_placeholder)
        self.search_entry.config(fg="gray")
        self.search_has_placeholder = True

    def _on_search_focus_in(self, event) -> None:
        if self.search_has_placeholder:
            self.search_entry.delete(0, tk.END)
            self.search_entry.config(fg="black")
            self.search_has_placeholder = False

    def _on_search_focus_out(self, event) -> None:
        if not self.search_entry.get().strip():
            self._set_search_placeholder()

    def _on_source_change(self, *args) -> None:
        source = self.search_source.get()
        if source == "local":
            self.download_button.config(text="Load Selected")
            self.local_folder_button.config(state=tk.NORMAL)
        else:
            self.download_button.config(text="Download Selected")
            self.local_folder_button.config(state=tk.DISABLED)

        if source == "local" and self.local_directory is not None:
            self._set_search_results(search_local_midi("", self.local_directory))
            return

        self.search_results.clear()
        self.results_listbox.delete(0, tk.END)
        if self.results_visible:
            self.results_listbox.pack_forget()
            self.download_button.pack_forget()
            self.results_visible = False
        self.download_button.config(state=tk.DISABLED)

    def choose_local_directory(self) -> None:
        selected_dir = filedialog.askdirectory(parent=self, title="Select Local MIDI Folder")
        if not selected_dir:
            return

        self.local_directory = Path(selected_dir)
        self.local_dir_label.config(text=f"Local folder: {self.local_directory}")

        if self.search_source.get() == "local":
            self._set_search_results(search_local_midi("", self.local_directory))

    def go_to_upload_screen(self) -> None:
        self.show_screen(self.upload_screen)

    def open_process_screen(self) -> None:
        if self.selected_file is None:
            messagebox.showinfo("Process", SELECT_FILE_FIRST_MSG)
            return

        self.show_screen(self.process_screen)
        self.start_processing()

    def start_processing(self) -> None:
        if self.selected_file is None:
            messagebox.showinfo("Process", SELECT_FILE_FIRST_MSG)
            return

        self.process_file_label.config(text=f"Selected file: {self.selected_file.name}")
        self.process_status_label.config(text="Starting conversion...")
        self.process_details_label.config(text="")
        self.to_play_button.config(state=tk.DISABLED)
        self.reprocess_button.config(state=tk.DISABLED)
        self.process_progress.start(10)

        def worker() -> None:
            error: Exception | None = None
            data: Dict[str, object] | None = None
            try:
                data = midi_to_solenoid_events(self.selected_file)
            except Exception as exc:
                error = exc

            def on_complete() -> None:
                self.process_progress.stop()
                self.reprocess_button.config(state=tk.NORMAL)

                if error is not None:
                    self.process_status_label.config(text="Conversion failed.")
                    self.process_details_label.config(text=f"Error: {error}")
                    self.to_play_button.config(state=tk.DISABLED)
                    return

                self.last_solenoid_package = data
                self.process_status_label.config(text="Conversion complete.")

                num_events = len(data.get("events", [])) if isinstance(data, dict) else 0
                num_commands = len(data.get("serial_schedule", [])) if isinstance(data, dict) else 0
                self.process_details_label.config(
                    text=(
                        f"Generated {num_events} note events and {num_commands} serial commands.\n\n"
                        "You can now go to the Play screen and stream to the microcontroller."
                    )
                )
                self.to_play_button.config(state=tk.NORMAL)

                try:
                    file_stem = self.selected_file.stem if self.selected_file else "solenoid_package"
                    print(f"--- Solenoid package for '{file_stem}' ---")
                    print(json.dumps(data, indent=2))
                except Exception as exc:
                    print("Error printing solenoid package:", exc)

            self.after(0, on_complete)

        threading.Thread(target=worker, daemon=True).start()

    def open_play_screen(self) -> None:
        if self.selected_file is None:
            messagebox.showinfo("Play", SELECT_FILE_FIRST_MSG)
            return

        self.play_file_label.config(text=f"Selected file: {self.selected_file.name}")
        self.play_status_label.config(text="Ready.")
        self.play_details_label.config(
            text="This will stream ON/OFF commands to the microcontroller in real-time."
        )
        self.show_screen(self.play_screen)

    def start_streaming(self) -> None:
        if self.selected_file is None:
            messagebox.showinfo("Play", SELECT_FILE_FIRST_MSG)
            return

        if self._playback_is_active():
            messagebox.showinfo("Play", "Streaming is already active.")
            return

        self.play_button.config(state=tk.DISABLED)
        self.stop_button.config(state=tk.NORMAL)
        self.play_status_label.config(text="Launching visualiser...")
        self.play_details_label.config(text="Starting the visualiser and preparing the streamer.")
        self.play_progress.start(10)
        self.playback_stop_requested = False

        visualiser_error: Exception | None = None
        try:
            self._launch_visualiser_no_audio(self.selected_file)
        except Exception as exc:
            visualiser_error = exc

        serial_port = self._normalize_serial_port(self.port_entry.get())
        if not serial_port:
            self._set_playback_idle()
            self.play_status_label.config(text=STREAMING_FAILED_MSG)
            self.play_details_label.config(text="Please enter a serial port.")
            return

        if not self._serial_port_exists(serial_port):
            available_ports = self._get_available_serial_ports()
            self._set_playback_idle()
            self.play_status_label.config(text=STREAMING_FAILED_MSG)
            if available_ports:
                self.play_details_label.config(
                    text="That serial port does not exist. Available ports: " + ", ".join(available_ports)
                )
            else:
                self.play_details_label.config(
                    text="That serial port does not exist, and no serial ports were detected."
                )
            return

        try:
            baud_rate = int(self.baud_entry.get().strip())
        except ValueError:
            self._set_playback_idle()
            self.play_status_label.config(text=STREAMING_FAILED_MSG)
            self.play_details_label.config(text="Baud rate must be an integer.")
            return

        try:
            startup_delay = float(self.startup_entry.get().strip())
        except ValueError:
            self._set_playback_idle()
            self.play_status_label.config(text=STREAMING_FAILED_MSG)
            self.play_details_label.config(text="Startup delay must be a number.")
            return

        self.play_status_label.config(text="Streaming started...")
        self.play_details_label.config(text="Sending commands to the microcontroller.")

        try:
            self._launch_streaming_process(serial_port, self.selected_file, baud_rate, startup_delay)
        except Exception as exc:
            self._set_playback_idle()
            self.play_status_label.config(text=STREAMING_FAILED_MSG)
            self.play_details_label.config(text=f"Error launching streamer: {exc}")
            return

        def worker() -> None:
            process = self.streaming_process
            if process is None:
                return

            stderr_text = ""
            returncode = process.wait()
            if process.stderr is not None:
                stderr_text = process.stderr.read().strip()

            def on_complete() -> None:
                if self.playback_stop_requested:
                    self.play_status_label.config(text="Streaming stopped.")
                    self.play_details_label.config(text="Streaming and visualiser were stopped.")
                    self._set_playback_idle(stop_requested=True)
                    return

                self.play_progress.stop()
                self._set_playback_idle()

                if returncode != 0:
                    self.play_status_label.config(text=STREAMING_FAILED_MSG)
                    error_text = stderr_text or f"Streamer exited with code {returncode}"
                    self.play_details_label.config(text=error_text)
                    return

                self.play_status_label.config(text="Streaming complete.")
                if visualiser_error is not None:
                    self.play_details_label.config(
                        text=(
                            "Playback commands were sent successfully, but visualiser failed to open: "
                            f"{visualiser_error}"
                        )
                    )
                else:
                    self.play_details_label.config(
                        text="Playback commands were sent successfully. Close the visualiser window when done."
                    )

            self.after(0, on_complete)

        threading.Thread(target=worker, daemon=True).start()

    def stop_streaming(self) -> None:
        if not self._playback_is_active():
            self._set_playback_idle()
            self.play_status_label.config(text=READY_STATUS_MSG)
            return

        self.playback_stop_requested = True
        self._terminate_process(self.streaming_process)
        self._terminate_process(self.visualiser_process)
        self.streaming_process = None
        self.visualiser_process = None
        self._set_playback_idle(stop_requested=True)
        self.play_status_label.config(text="Streaming stopped.")
        self.play_details_label.config(text="Streaming and visualiser were stopped.")

    def _playback_is_active(self) -> bool:
        return any(
            process is not None and process.poll() is None
            for process in (self.streaming_process, self.visualiser_process)
        )

    def _set_playback_idle(self, stop_requested: bool = False) -> None:
        self.play_button.config(state=tk.NORMAL)
        self.stop_button.config(state=tk.DISABLED)
        self.play_progress.stop()
        if stop_requested:
            self.playback_stop_requested = False

    def _terminate_process(self, process: subprocess.Popen[str] | None) -> None:
        if process is None or process.poll() is not None:
            return

        try:
            process.terminate()
            process.wait(timeout=1.0)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    def _normalize_serial_port(self, serial_port: str) -> str:
        return serial_port.strip().strip('"').strip("'")

    def _get_available_serial_ports(self) -> List[str]:
        try:
            from serial.tools import list_ports
        except Exception:
            return []

        return [port.device for port in list_ports.comports()]

    def _serial_port_exists(self, serial_port: str) -> bool:
        available_ports = self._get_available_serial_ports()
        if not available_ports:
            return True
        return serial_port in available_ports

    def _launch_streaming_process(
        self,
        serial_port: str,
        midi_file: Path,
        baud_rate: int,
        startup_delay: float,
    ) -> None:
        streaming_script = "\n".join(
            [
                "import sys",
                f"sys.path.insert(0, {str(APP_DIR)!r})",
                "from midi_stream_player import stream_schedule",
                "",
                "stream_schedule(",
                "    serial_port=sys.argv[1],",
                "    midi_file=sys.argv[2],",
                "    baud_rate=int(sys.argv[3]),",
                "    startup_delay_s=float(sys.argv[4]),",
                ")",
            ]
        )

        self.streaming_process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                streaming_script,
                serial_port,
                str(midi_file),
                str(baud_rate),
                str(startup_delay),
            ],
            cwd=str(APP_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

    def _launch_visualiser_no_audio(self, midi_file: Path) -> None:
        # Reuse an existing visualiser window if one is already open.
        if self.visualiser_process is not None and self.visualiser_process.poll() is None:
            return

        visualiser_script = "\n".join(
            [
                "import sys",
                f"sys.path.insert(0, {str(APP_DIR)!r})",
                "import mido",
                "from midi_visualiser.visualiser import Visualiser",
                "",
                "class _SilentOutput:",
                "    def send(self, *args, **kwargs):",
                VIS_SCRIPT_PASS_LINE,
                "",
                "    def reset(self):",
                VIS_SCRIPT_PASS_LINE,
                "",
                "    def close(self):",
                VIS_SCRIPT_PASS_LINE,
                "",
                "mido.open_output = lambda *args, **kwargs: _SilentOutput()",
                "app = Visualiser(sys.argv[1])",
                "if app.song:",
                "    app.song.start()",
                "app.run()",
            ]
        )

        self.visualiser_process = subprocess.Popen(
            [sys.executable, "-c", visualiser_script, str(midi_file)],
            cwd=str(APP_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

        try:
            self.visualiser_process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            return

        stderr_text = ""
        if self.visualiser_process.stderr is not None:
            stderr_text = self.visualiser_process.stderr.read().strip()

        self.visualiser_process = None
        raise RuntimeError(stderr_text or "midi-visualiser exited immediately")

    def _on_close(self) -> None:
        self.stop_streaming()
        self.destroy()


if __name__ == "__main__":
    app = MidiControllerApp()
    app.mainloop()
