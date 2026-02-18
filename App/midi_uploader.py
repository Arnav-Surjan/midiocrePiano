import tkinter as tk
from tkinter import filedialog, messagebox
from tkinter import ttk
from pathlib import Path
from typing import List, Dict
import threading
import json

import requests
from bs4 import BeautifulSoup
from urllib.parse import quote_plus, urljoin

from midi_to_solenoid import midi_to_solenoid_events


COMMON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class MidiUploader(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("MIDI File Uploader")
        self.resizable(True, True)

        self.selected_file: Path | None = None

        # Selected file label
        self.file_label = tk.Label(self, text="No MIDI file selected.", padx=20, pady=10)
        self.file_label.pack()

        # Process button (created but not shown until a MIDI file is selected)
        # Explicit colors avoid the "greyed out" look on some platforms.
        self.process_button = tk.Button(
            self,
            text="Process",
            command=self.open_process_page,
            bg="white",
            fg="black",
        )

        # Upload MIDI file button
        self.upload_button = tk.Button(self, text="Upload MIDI File", command=self.select_file)
        self.upload_button.pack(pady=(5, 5))
        
        # "OR" separator
        or_label = tk.Label(self, text="OR")
        or_label.pack(pady=(5, 5))

        # Search controls: [Search Bar] [Dropdown for source]
        search_frame = tk.Frame(self)
        search_frame.pack(padx=10, pady=(10, 5), fill=tk.X)

        self.search_entry = tk.Entry(search_frame, width=40)
        self.search_entry.pack(side=tk.LEFT, padx=(0, 5), fill=tk.X, expand=True)

        self.search_source = tk.StringVar(value="midiworld")
        source_dropdown = tk.OptionMenu(search_frame, self.search_source, "midiworld", "bitmidi")
        source_dropdown.pack(side=tk.LEFT)

        # Clear results when switching source.
        self.search_source.trace_add("write", self._on_source_change)

        search_button = tk.Button(search_frame, text="Search", command=self.perform_search)
        search_button.pack(side=tk.LEFT, padx=(5, 0))

        # Hitting Enter in the search bar performs the search.
        self.search_entry.bind("<Return>", self.perform_search)

        # Placeholder text for search bar.
        self.search_placeholder = "Search for a MIDI file"
        self.search_has_placeholder = False
        self._set_search_placeholder()
        self.search_entry.bind("<FocusIn>", self._on_search_focus_in)
        self.search_entry.bind("<FocusOut>", self._on_search_focus_out)

        # Search results list (initially hidden)
        self.results_listbox = tk.Listbox(self, width=60, height=10)
        self.results_listbox.bind("<<ListboxSelect>>", self._on_result_select)

        # Backing store for search results metadata
        self.search_results: List[Dict[str, str]] = []

        # Download button for selected result (initially hidden and disabled)
        self.download_button = tk.Button(self, text="Download Selected", command=self.download_selected_result, state=tk.DISABLED)

        # Track visibility of results widgets
        self.results_visible = False


    def select_file(self) -> None:
        file_path = filedialog.askopenfilename(
            title="Select a MIDI file",
            filetypes=[("MIDI files", "*.mid *.midi")],
        )

        if not file_path:
            return

        path = Path(file_path)

        # Guard against files with the wrong extension slipping through the dialog filters.
        if path.suffix.lower() not in {".mid", ".midi"}:
            messagebox.showerror("Invalid File", "Please choose a file with a .mid or .midi extension.")
            return

        self.selected_file = path
        self.file_label.config(text=f"Selected file: {path.name}")
        self._show_process_button()

    def perform_search(self, event=None) -> None:
        query = self.search_entry.get().strip()
        if self.search_has_placeholder or query == self.search_placeholder:
            query = ""
        if not query:
            messagebox.showinfo("Search", "Please enter a search term.")
            return

        source = self.search_source.get()
        if source not in {"midiworld", "bitmidi"}:
            messagebox.showerror("Search Error", "Invalid search source selected.")
            return

        try:
            if source == "midiworld":
                search_results = search_midiworld(query)
            else:
                search_results = search_bitmidi(query)
        except Exception as exc:  # pragma: no cover - network errors
            messagebox.showerror("Search Error", f"Could not search {source}: {exc}")
            return

        self.results_listbox.delete(0, tk.END)
        self.search_results.clear()

        for item in search_results:
            self.search_results.append(item)
            self.results_listbox.insert(tk.END, item["title"])

        # Show the results list and download button after a search.
        self._show_results_widgets()

        # Clear any previous selection and disable download until a result is selected.
        self.results_listbox.selection_clear(0, tk.END)
        self.download_button.config(state=tk.DISABLED)

        if not self.search_results:
            messagebox.showinfo("No Results", "No MIDI files found for that search.")

    def download_selected_result(self) -> None:
        if not self.search_results:
            messagebox.showinfo("Download", "Please run a search and select a result first.")
            return

        selection = self.results_listbox.curselection()
        if not selection:
            messagebox.showinfo("Download", "Please select a MIDI file from the list.")
            return

        item = self.search_results[selection[0]]
        download_url = item.get("download_url")
        if not download_url:
            messagebox.showerror("Download Error", "No download URL available for the selected item.")
            return

        suggested_name = item.get("filename") or f"{item['title']}.mid"

        save_path = filedialog.asksaveasfilename(
            parent=self,
            title="Save MIDI file",
            defaultextension=".mid",
            initialfile=suggested_name,
            filetypes=[("MIDI files", "*.mid *.midi")],
        )

        if not save_path:
            return

        # Disable the button while downloading to prevent duplicate clicks.
        self.download_button.config(state=tk.DISABLED)

        def do_download() -> None:
            error: Exception | None = None
            try:
                download_midi_file(download_url, save_path)
            except Exception as exc:  # pragma: no cover - network errors
                error = exc

            def on_complete() -> None:
                if error is not None:
                    messagebox.showerror("Download Error", f"Could not download MIDI file: {error}")
                else:
                    self.selected_file = Path(save_path)
                    self.file_label.config(text=f"Selected file: {self.selected_file.name}")
                    messagebox.showinfo("Download Complete", "MIDI file downloaded and loaded successfully.")

                    # A MIDI file is now available to process.
                    self._show_process_button()

                # Re-enable the button only if there is a current selection.
                if self.search_results and self.results_listbox.curselection():
                    self.download_button.config(state=tk.NORMAL)
                else:
                    self.download_button.config(state=tk.DISABLED)

            # Marshal UI updates back to the main Tkinter thread.
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
            # Typed search text should be white for readability on dark backgrounds.
            self.search_entry.config(fg="white")
            self.search_has_placeholder = False

    def _on_search_focus_out(self, event) -> None:
        if not self.search_entry.get().strip():
            self._set_search_placeholder()

    def _on_source_change(self, *args) -> None:
        """Clear results and hide results widgets when switching source."""

        self.search_results.clear()
        self.results_listbox.delete(0, tk.END)
        if self.results_visible:
            self.results_listbox.pack_forget()
            self.download_button.pack_forget()
            self.results_visible = False
        self.download_button.config(state=tk.DISABLED)

    def _show_results_widgets(self) -> None:
        if not self.results_visible:
            self.results_listbox.pack(padx=10, pady=(5, 0), fill=tk.BOTH, expand=True)
            self.download_button.pack(pady=(5, 10))
            self.results_visible = True

    def _on_result_select(self, event) -> None:
        selection = self.results_listbox.curselection()
        if selection:
            self.download_button.config(state=tk.NORMAL)
        else:
            self.download_button.config(state=tk.DISABLED)

    def _show_process_button(self) -> None:
        """Show the Process button once a MIDI file is selected."""

        if self.selected_file is None:
            return

        if not self.process_button.winfo_ismapped():
                # Place the Process button directly under the selected-file label
                # and above the Upload button for a clear linear flow.
                self.process_button.pack(pady=(10, 20), before=self.upload_button)

    def open_process_page(self) -> None:
        """Open a simple processing window and convert the selected MIDI file.

        Conversion is done in a background thread while this window shows
        status/progress so the main UI stays responsive.
        """

        if self.selected_file is None:
            messagebox.showinfo("Process", "Please select a MIDI file first.")
            return

        process_window = tk.Toplevel(self)
        process_window.title("Processing MIDI")
        process_window.resizable(False, False)

        status_label = tk.Label(process_window, text="Starting conversion...")
        status_label.pack(padx=20, pady=(20, 10))

        progress_bar = ttk.Progressbar(process_window, mode="indeterminate", length=260)
        progress_bar.pack(padx=20, pady=(0, 10))
        progress_bar.start(10)

        details_label = tk.Label(process_window, text="", wraplength=320, justify=tk.LEFT)
        details_label.pack(padx=20, pady=(0, 10))

        close_button = tk.Button(process_window, text="Close", command=process_window.destroy, state=tk.DISABLED)
        close_button.pack(pady=(0, 20))

        # Prevent starting multiple conversions at once from the main window.
        self.process_button.config(state=tk.DISABLED)

        def worker() -> None:
            error: Exception | None = None
            data = None
            try:
                data = midi_to_solenoid_events(self.selected_file)
            except Exception as exc:  # pragma: no cover - unexpected errors
                error = exc

            def on_complete() -> None:
                progress_bar.stop()

                if error is not None:
                    status_label.config(text="Conversion failed.")
                    details_label.config(text=f"Error: {error}")
                else:
                    status_label.config(text="Conversion complete.")
                    num_events = len(data.get("events", [])) if isinstance(data, dict) else 0
                    details_label.config(
                        text=(
                            f"Generated {num_events} solenoid events.\n\n"
                            "You can now send this package to the ESP32."
                        )
                    )

                    # Store the last conversion result on the app instance
                    # for potential future use.
                    self.last_solenoid_package = data

                    # Print the generated JSON package to the console for inspection.
                    try:
                        file_stem = self.selected_file.stem if self.selected_file else "solenoid_package"
                        print(f"--- Solenoid package for '{file_stem}' ---")
                        print(json.dumps(data, indent=2))
                    except Exception as e:  # pragma: no cover - printing errors
                        print("Error printing solenoid package:", e)

                close_button.config(state=tk.NORMAL)
                self.process_button.config(state=tk.NORMAL)

            self.after(0, on_complete)

        threading.Thread(target=worker, daemon=True).start()


def download_midi_file(url: str, destination: str) -> None:
    headers = dict(COMMON_HEADERS)
    if "midiworld.com" in url:
        headers["Referer"] = "https://www.midiworld.com/"
    elif "bitmidi.com" in url:
        headers["Referer"] = "https://bitmidi.com/"

    response = requests.get(url, stream=True, timeout=20, headers=headers)
    response.raise_for_status()

    with open(destination, "wb") as file:
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                file.write(chunk)


def search_midiworld(query: str) -> List[Dict[str, str]]:
    """Search midiworld.com for MIDI files and return a list of results.

    Each result is a dict with keys: title, download_url, filename.
    """

    url = f"https://www.midiworld.com/search/?q={quote_plus(query)}"

    headers = dict(COMMON_HEADERS)
    headers["Referer"] = "https://www.midiworld.com/"

    response = requests.get(url, timeout=20, headers=headers)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    results: List[Dict[str, str]] = []

    for link in soup.find_all("a", href=True):
        href = link["href"]
        if "/download/" not in href:
            continue

        download_url = urljoin("https://www.midiworld.com/", href)
        parent_text = link.parent.get_text(" ", strip=True) if link.parent else link.get_text(" ", strip=True)
        title = parent_text.replace("download", "").strip(" -• \u2022") or "MIDIWorld file"
        filename = f"{title}.mid" if not title.lower().endswith((".mid", ".midi")) else title

        results.append({
            "title": title,
            "download_url": download_url,
            "filename": filename,
        })

    return results


def search_bitmidi(query: str) -> List[Dict[str, str]]:
    """Search bitmidi.com for MIDI files and return a list of results.

    Each result is a dict with keys: title, download_url, filename.

    Note: This relies on the public HTML layout of bitmidi.com and
    may need adjustments if the site changes.
    """

    base_url = "https://bitmidi.com"
    url = f"{base_url}/search?q={quote_plus(query)}"

    search_headers = dict(COMMON_HEADERS)
    search_headers["Referer"] = base_url

    response = requests.get(url, timeout=20, headers=search_headers)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    track_links = []
    for link in soup.find_all("a", href=True):
        href = link["href"]
        # Heuristic: track pages are relative URLs containing '-mid'.
        if href.startswith("/") and "-mid" in href:
            track_links.append((link.get_text(strip=True) or "BitMidi file", urljoin(base_url, href)))

    results: List[Dict[str, str]] = []

    for title, track_url in track_links:
        try:
            track_headers = dict(COMMON_HEADERS)
            track_headers["Referer"] = url
            track_resp = requests.get(track_url, timeout=20, headers=track_headers)
            track_resp.raise_for_status()
        except Exception:
            continue

        track_soup = BeautifulSoup(track_resp.text, "html.parser")
        download_url = None

        # Prefer explicit download links that end with .mid.
        for a in track_soup.find_all("a", href=True):
            href = a["href"]
            if href.lower().endswith(".mid"):
                download_url = urljoin(base_url, href)
                break

        if not download_url:
            continue

        filename = f"{title}.mid" if not title.lower().endswith((".mid", ".midi")) else title

        results.append({
            "title": title,
            "download_url": download_url,
            "filename": filename,
        })

    return results


if __name__ == "__main__":
    app = MidiUploader()
    app.mainloop()
