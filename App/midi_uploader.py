import tkinter as tk
from tkinter import filedialog, messagebox
from pathlib import Path


class MidiUploader(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("MIDI File Uploader")
        self.resizable(False, False)

        self.file_label = tk.Label(self, text="No MIDI file selected.", padx=20, pady=10)
        self.file_label.pack()

        select_button = tk.Button(self, text="Choose MIDI File", command=self.select_file)
        select_button.pack(pady=(0, 20))

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

        self.file_label.config(text=f"Selected file: {path.name}")


if __name__ == "__main__":
    app = MidiUploader()
    app.mainloop()
