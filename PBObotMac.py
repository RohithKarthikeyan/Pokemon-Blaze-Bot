import tkinter as tk
import threading
import time
import cv2
import numpy as np
import pytesseract

import os
import json
import queue
import tkinter.scrolledtext as st  # For the scrolling text log
from PIL import Image, ImageTk
from difflib import SequenceMatcher

# ===== macOS-specific imports (pyobjc / Quartz) =====
import subprocess
from Quartz import (
    CGWindowListCopyWindowInfo,
    CGWindowListCreateImage,
    CGWindowListOptionIncludingWindow,
    CGWindowListOptionOnScreenOnly,
    CGWindowListOptionAll,
    kCGNullWindowID,
    kCGWindowImageDefault,
    CGRectMake,
    CGEventCreateKeyboardEvent,
    CGEventPost,
    kCGSessionEventTap,
)

# If you need more Apple constants:
# from Quartz import kCGWindowName, ...

CONFIG_FILENAME = "PBO_bot_config.json"

# -------------------------
# Utility: Load/Save config
# -------------------------
def load_config():
    """Load settings from CONFIG_FILENAME (JSON). Return as dict."""
    if not os.path.exists(CONFIG_FILENAME):
        return {}
    with open(CONFIG_FILENAME, "r", encoding="utf-8") as f:
        return json.load(f)

def save_config(data):
    """Save settings to CONFIG_FILENAME (JSON)."""
    with open(CONFIG_FILENAME, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)

# -------------------------
# macOS: Find window by title
# -------------------------
def find_window_by_title(title_substring):
    """
    Return (window_id, bounds_dict) for the first window whose title
    contains `title_substring`. If not found, return (None, None).

    bounds_dict is typically of the form:
      {"X": 100, "Y": 100, "Width": 800, "Height": 600}
    """
    # For convenience, let's look at all windows:
    window_list = CGWindowListCopyWindowInfo(
        CGWindowListOptionAll,  # or CGWindowListOptionOnScreenOnly, etc.
        kCGNullWindowID
    )
    for w in window_list:
        # Some keys in the dictionary (depends on your OS version):
        #   'kCGWindowOwnerName' -> the app name
        #   'kCGWindowName'      -> the window’s actual title
        #   'kCGWindowNumber'    -> the window ID (used to capture it)
        #   'kCGWindowBounds'    -> bounding box info
        owner_name = w.get('kCGWindowOwnerName', '')
        window_title = w.get('kCGWindowName', '')
        if title_substring.lower() in owner_name.lower() or title_substring.lower() in window_title.lower():
            window_id = w.get('kCGWindowNumber')
            bounds = w.get('kCGWindowBounds')
            if window_id and bounds:
                return (window_id, bounds)
    return (None, None)

# -------------------------
# macOS: Capture a region from a given window
# -------------------------
def capture_window_mac(window_id, window_bounds, region=None):
    """
    Captures the entire window with ID=window_id, then optionally crops
    the region=(x, y, w, h) *relative to the window’s top-left*.

    Returns a PIL Image.
    """
    # 1) Create a CGImage from that window
    full_image_ref = CGWindowListCreateImage(
        CGRectMake(
            float(window_bounds["X"]),
            float(window_bounds["Y"]),
            float(window_bounds["Width"]),
            float(window_bounds["Height"])
        ),
        CGWindowListOptionIncludingWindow,
        window_id,
        kCGWindowImageDefault
    )
    if not full_image_ref:
        return None  # e.g. if the window is minimized or permission is missing

    # 2) Convert CGImage to PIL image
    width = int(window_bounds["Width"])
    height = int(window_bounds["Height"])
    bytes_per_row = width * 4  # RGBA
    data = full_image_ref.data()
    # PyObjC’s get_data() or .data() can give a buffer; we can build a PIL Image from that:
    pil_img = Image.frombuffer(
        "RGBA",
        (width, height),
        data,
        "raw",
        "RGBA",
        bytes_per_row,
        1
    )

    # 3) If region is given, we do a sub-crop relative to the top-left
    #    of the window
    if region:
        x, y, w, h = region
        # Ensure we stay in-bounds
        x2 = x + w
        y2 = y + h
        pil_img = pil_img.crop((x, y, x2, y2))

    return pil_img.convert("RGB")  # For consistency w/ your existing code

# -------------------------
# Checking for red region
# -------------------------
def is_reddish_in_region(window_id, window_bounds, region, lower_thresh=(150,0,0), upper_thresh=(255,100,100)):
    region_img = capture_window_mac(window_id, window_bounds, region)
    if region_img is None:
        return False
    np_img = np.array(region_img)  # shape: (H, W, 3) in RGB
    np_img_bgr = cv2.cvtColor(np_img, cv2.COLOR_RGB2BGR)

    # BGR thresholds
    lower = np.array([lower_thresh[2], lower_thresh[1], lower_thresh[0]])
    upper = np.array([upper_thresh[2], upper_thresh[1], upper_thresh[0]])
    mask = cv2.inRange(np_img_bgr, lower, upper)
    return np.any(mask > 0)

# -------------------------
# OCR for name
# -------------------------
def get_pokemon_name(window_id, window_bounds, region):
    im = capture_window_mac(window_id, window_bounds, region)
    if im is None:
        return ""
    np_img = np.array(im)
    gray = cv2.cvtColor(np_img, cv2.COLOR_RGB2GRAY)
    _, binary = cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY)
    text = pytesseract.image_to_string(binary, config='--psm 7')
    return text.strip()

# -------------------------
# macOS: Send keystrokes in the background
# -------------------------
# We'll define a small dictionary that maps your "virtual key" idea to macOS scancodes.
# For reference:
# - 0x00 to 0x7F: these are not the same as Windows. 
# - For arrow keys: Left=123, Right=124, Down=125, Up=126
# - For digits: '1'=18, '2'=19, '3'=20, '4'=21
# - For letters: 'r'=15, ...
MAC_KEYCODES = {
    "left_arrow": 123,
    "right_arrow": 124,
    "down_arrow": 125,
    "up_arrow": 126,
    "1": 18,
    "2": 19,
    "3": 20,
    "4": 21,
    "r": 15,
}

def press_key_mac(key_str, hold_time=0.25):
    """
    Posts a key-down, waits hold_time, then key-up event to macOS.
    key_str should be something from the MAC_KEYCODES dictionary.
    """
    if key_str not in MAC_KEYCODES:
        print(f"Unknown key_str: {key_str}")
        return

    key_code = MAC_KEYCODES[key_str]
    # Create event: key down
    event_down = CGEventCreateKeyboardEvent(None, key_code, True)
    # Create event: key up
    event_up   = CGEventCreateKeyboardEvent(None, key_code, False)

    # Post them
    CGEventPost(kCGSessionEventTap, event_down)
    time.sleep(hold_time)
    CGEventPost(kCGSessionEventTap, event_up)

# -------------------------
# Movement logic
# -------------------------
def move_in_bushes(window_id, last_dir):
    """
    Simple toggle between left and right arrow.
    """
    if last_dir[0] == 0:
        press_key_mac("right_arrow")  # Right arrow
        last_dir[0] = 1
    else:
        press_key_mac("left_arrow")  # Left arrow
        last_dir[0] = 0

# -------------------------
# Worker thread class
# -------------------------
class BotThread(threading.Thread):
    def __init__(
        self,
        window_id,
        window_bounds,
        pokeball_region,
        name_region,
        stop_event,
        pause_event,
        log_queue,
        target_pokemons=None,
        move_choice="1"
    ):
        super().__init__()
        self.window_id = window_id
        self.window_bounds = window_bounds
        self.pokeball_region = pokeball_region
        self.name_region = name_region
        self.stop_event = stop_event
        self.pause_event = pause_event
        self.log_queue = log_queue
        if target_pokemons is None:
            target_pokemons = []
        self.target_pokemons = target_pokemons
        self.move_choice = move_choice

    def run(self):
        self._log("Bot thread started.")
        encounter_count = 0

        last_encounter_time = time.time()
        next_check_time = last_encounter_time + 10
        last_dir = [0]  # track movement direction

        while not self.stop_event.is_set():
            if self.pause_event.is_set():
                time.sleep(0.2)
                continue

            move_in_bushes(self.window_id, last_dir)

            if is_reddish_in_region(self.window_id, self.window_bounds, self.pokeball_region):
                encounter_count += 1
                last_encounter_time = time.time()
                next_check_time = last_encounter_time + 10
                self._log(f"Wild Pokémon encountered! (#{encounter_count})")

                pokemon_name = get_pokemon_name(self.window_id, self.window_bounds, self.name_region)
                self._log(f"Encountered Pokémon: {pokemon_name}")

                # Check if it's a target
                if any(self._is_similar(pokemon_name, t) for t in self.target_pokemons):
                    self._beep(f"Target Pokémon {pokemon_name} encountered! Stopping.")
                    break
                else:
                    self._log(f"Defeating wild Pokémon using move '{self.move_choice}'...")
                    self.defeat_wild_pokemon()
                    time.sleep(0.1)

            now = time.time()
            if now >= next_check_time:
                if (now - last_encounter_time) >= 10:
                    self._log("Check PBO window for issues. No encounters detected.")
                    self._beep("No encounters beep")  # short beep to alert
                next_check_time = now + 10

            time.sleep(0.1)

        self._log("Bot thread stopping.")

    def defeat_wild_pokemon(self):
        """
        Press the chosen move key.
        We'll map "1","2","3","4","r" to the correct mac key code strings.
        """
        move_map = {
            "1": "1",
            "2": "2",
            "3": "3",
            "4": "4",
            "r": "r"
        }
        key_str = move_map.get(self.move_choice, "1")
        press_key_mac(key_str, hold_time=0.1)

    def _is_similar(self, name, target, threshold=0.7):
        return SequenceMatcher(None, name.lower(), target.lower()).ratio() >= threshold

    def _log(self, message):
        self.log_queue.put(("LOG", message))

    def _beep(self, message):
        """
        On macOS, we can call 'afplay' to play a system sound, or say() TTS, etc.
        We'll do a short system beep using afplay. 
        """
        # This call returns immediately, so if you want blocking beep,
        # wrap with a small thread or use "afplay ... & wait"
        subprocess.Popen(["afplay", "/System/Library/Sounds/Ping.aiff"])
        self.log_queue.put(("LOG", message))

# -------------------------
# Calibration
# -------------------------
def calibrate_two_regions(window_id, window_bounds, log_queue):
    """
    We'll show a snapshot of the entire window and let the user draw 2 boxes.
    Return (box1, box2) as ( (x,y,w,h), (x,y,w,h) ).
    """
    full_img_pil = capture_window_mac(window_id, window_bounds, region=None)
    if full_img_pil is None:
        log_queue.put(("LOG", "Could not capture window for calibration."))
        return (None, None)

    calib_win = tk.Toplevel()
    calib_win.title("Calibrate Regions (Draw 2 boxes in order)")

    tk_img = ImageTk.PhotoImage(full_img_pil)
    canvas = tk.Canvas(calib_win, width=tk_img.width(), height=tk_img.height())
    canvas.pack()

    canvas.create_image(0, 0, anchor="nw", image=tk_img)

    start_x = start_y = 0
    current_rect_id = None
    result = [None, None]
    region_index = [0]

    def on_button_press(event):
        nonlocal start_x, start_y, current_rect_id
        start_x, start_y = event.x, event.y
        current_rect_id = canvas.create_rectangle(start_x, start_y, start_x, start_y,
                                                  outline="red", width=2)

    def on_move_press(event):
        nonlocal current_rect_id
        if current_rect_id:
            canvas.coords(current_rect_id, start_x, start_y, event.x, event.y)

    def on_button_release(event):
        nonlocal current_rect_id
        end_x, end_y = event.x, event.y
        x1, x2 = sorted([start_x, end_x])
        y1, y2 = sorted([start_y, end_y])
        w = x2 - x1
        h = y2 - y1

        idx = region_index[0]
        result[idx] = (x1, y1, w, h)
        log_queue.put(("LOG", f"Calibrated box {idx+1} => ( x= {x1} , y= {y1} , w= {w} , h= {h} )"))

        if idx == 1:
            calib_win.destroy()
        else:
            region_index[0] += 1

        current_rect_id = None

    canvas.bind("<ButtonPress-1>", on_button_press)
    canvas.bind("<B1-Motion>", on_move_press)
    canvas.bind("<ButtonRelease-1>", on_button_release)

    calib_win.mainloop()
    return result[0], result[1]

# -------------------------
# GUI
# -------------------------
class BotGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Pokémon Bot (macOS Version)")

        # 1) Load config
        self.config_data = load_config()

        if "pokeball_region" not in self.config_data:
            self.config_data["pokeball_region"] = [1380, 330, 60, 20]
        if "name_region" not in self.config_data:
            self.config_data["name_region"] = [1224, 339, 145, 20]
        if "target_pokemons" not in self.config_data:
            self.config_data["target_pokemons"] = ["Electivire", "Xurkitree", "Garchomp"]
        if "preferred_move" not in self.config_data:
            self.config_data["preferred_move"] = "1"

        # Window info (will be set after user finds window)
        self.window_id = None
        self.window_bounds = None

        # Regions
        tk.Label(root, text="Poké Ball Region (x, y, w, h):").grid(row=0, column=0, sticky="w")
        self.pb_x = tk.Entry(root, width=5)
        self.pb_y = tk.Entry(root, width=5)
        self.pb_w = tk.Entry(root, width=5)
        self.pb_h = tk.Entry(root, width=5)
        self.pb_x.grid(row=0, column=1)
        self.pb_y.grid(row=0, column=2)
        self.pb_w.grid(row=0, column=3)
        self.pb_h.grid(row=0, column=4)

        tk.Label(root, text="Name Region (x, y, w, h):").grid(row=1, column=0, sticky="w")
        self.nm_x = tk.Entry(root, width=5)
        self.nm_y = tk.Entry(root, width=5)
        self.nm_w = tk.Entry(root, width=5)
        self.nm_h = tk.Entry(root, width=5)
        self.nm_x.grid(row=1, column=1)
        self.nm_y.grid(row=1, column=2)
        self.nm_w.grid(row=1, column=3)
        self.nm_h.grid(row=1, column=4)

        pb_def = self.config_data["pokeball_region"]
        nm_def = self.config_data["name_region"]
        self.pb_x.insert(0, str(pb_def[0]))
        self.pb_y.insert(0, str(pb_def[1]))
        self.pb_w.insert(0, str(pb_def[2]))
        self.pb_h.insert(0, str(pb_def[3]))

        self.nm_x.insert(0, str(nm_def[0]))
        self.nm_y.insert(0, str(nm_def[1]))
        self.nm_w.insert(0, str(nm_def[2]))
        self.nm_h.insert(0, str(nm_def[3]))

        # Target Pokémon
        self.target_pokemons = list(self.config_data["target_pokemons"])
        target_frame = tk.LabelFrame(root, text="Target Pokémon")
        target_frame.grid(row=5, column=0, columnspan=5, padx=5, pady=5, sticky="w")

        self.target_list_text = st.ScrolledText(target_frame, wrap="word", width=30, height=3)
        self.target_list_text.grid(row=0, column=0, columnspan=4, padx=5, pady=5)
        self.target_list_text.configure(state='disabled')

        tk.Label(target_frame, text="Pokémon Name:").grid(row=1, column=0, sticky="e")
        self.new_pokemon_entry = tk.Entry(target_frame, width=20)
        self.new_pokemon_entry.grid(row=1, column=1, sticky="w")

        self.add_pokemon_btn = tk.Button(target_frame, text="Add", command=self.add_pokemon)
        self.add_pokemon_btn.grid(row=1, column=2, padx=5)

        self.remove_pokemon_btn = tk.Button(target_frame, text="Remove", command=self.remove_pokemon)
        self.remove_pokemon_btn.grid(row=1, column=3, padx=5)

        self._update_target_list_display()

        # Move Selection
        move_frame = tk.LabelFrame(root, text="Move to Use")
        move_frame.grid(row=6, column=0, columnspan=5, padx=5, pady=5, sticky="w")
        tk.Label(move_frame, text="Choose Move:").grid(row=0, column=0, sticky="w")

        self.move_choice_var = tk.StringVar()
        self.move_choice_var.set(self.config_data["preferred_move"])
        move_options = ["1", "2", "3", "4", "r"]
        self.move_dropdown = tk.OptionMenu(move_frame, self.move_choice_var, *move_options, command=self.on_move_changed)
        self.move_dropdown.grid(row=0, column=1, padx=10, sticky="w")

        # Control buttons
        self.find_window_btn = tk.Button(root, text="Find PBO Window", command=self.find_pbo_window)
        self.calibrate_btn = tk.Button(root, text="Calibrate", command=self.calibrate_regions)
        self.start_btn = tk.Button(root, text="Start", command=self.start_bot)
        self.pause_btn = tk.Button(root, text="Pause", command=self.pause_bot)
        self.stop_btn  = tk.Button(root, text="Stop", command=self.stop_bot)

        self.find_window_btn.grid(row=2, column=0, pady=10)
        self.calibrate_btn.grid(row=2, column=1, pady=10)
        self.start_btn.grid(row=2, column=2, pady=10)
        self.pause_btn.grid(row=2, column=3, pady=10)
        self.stop_btn.grid(row=2, column=4, pady=10)

        # Log area
        tk.Label(root, text="Log:").grid(row=7, column=0, sticky="w")
        self.log_text = st.ScrolledText(root, wrap="word", width=60, height=10)
        self.log_text.grid(row=8, column=0, columnspan=5, padx=5, pady=5)
        self.log_text.configure(state='disabled')

        # Threading
        self.bot_thread = None
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.log_queue = queue.Queue()

        self._poll_log_queue()
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def on_closing(self):
        self._update_config_data()
        save_config(self.config_data)
        self.root.destroy()

    # Move changed
    def on_move_changed(self, choice):
        self.config_data["preferred_move"] = choice
        save_config(self.config_data)
        self.log_queue.put(("LOG", f"Preferred move changed to: {choice}"))

    # Target Pokémon logic
    def add_pokemon(self):
        new_poke = self.new_pokemon_entry.get().strip()
        if new_poke:
            self.target_pokemons.append(new_poke)
            self.log_queue.put(("LOG", f"Added new target Pokémon: {new_poke}"))
            self._update_target_list_display()
            self.new_pokemon_entry.delete(0, tk.END)
            self.config_data["target_pokemons"] = self.target_pokemons
            save_config(self.config_data)

    def remove_pokemon(self):
        poke_to_remove = self.new_pokemon_entry.get().strip()
        if poke_to_remove:
            if poke_to_remove in self.target_pokemons:
                self.target_pokemons.remove(poke_to_remove)
                self.log_queue.put(("LOG", f"Removed target Pokémon: {poke_to_remove}"))
            else:
                self.log_queue.put(("LOG", f"Pokémon '{poke_to_remove}' not found in list."))
            self._update_target_list_display()
            self.new_pokemon_entry.delete(0, tk.END)
            self.config_data["target_pokemons"] = self.target_pokemons
            save_config(self.config_data)

    def _update_target_list_display(self):
        self.target_list_text.configure(state='normal')
        self.target_list_text.delete("1.0", tk.END)
        for p in self.target_pokemons:
            self.target_list_text.insert(tk.END, p + "\n")
        self.target_list_text.configure(state='disabled')

    # Find the game window
    def find_pbo_window(self):
        GAME_WINDOW_TITLE = "Pokemon Blaze Online"
        w_id, w_bounds = find_window_by_title(GAME_WINDOW_TITLE)
        if w_id is None:
            self.log_queue.put(("LOG", f"Window not found: {GAME_WINDOW_TITLE}"))
        else:
            self.window_id = w_id
            self.window_bounds = w_bounds
            self.log_queue.put(("LOG", f"Found window ID={w_id}, bounds={w_bounds}"))

    def calibrate_regions(self):
        if not self.window_id or not self.window_bounds:
            self.log_queue.put(("LOG", "You must find the PBO window first!"))
            return
        pb_box, nm_box = calibrate_two_regions(self.window_id, self.window_bounds, log_queue=self.log_queue)
        if pb_box is not None:
            x, y, w, h = pb_box
            self.pb_x.delete(0, tk.END)
            self.pb_y.delete(0, tk.END)
            self.pb_w.delete(0, tk.END)
            self.pb_h.delete(0, tk.END)
            self.pb_x.insert(0, str(x))
            self.pb_y.insert(0, str(y))
            self.pb_w.insert(0, str(w))
            self.pb_h.insert(0, str(h))

        if nm_box is not None:
            x, y, w, h = nm_box
            self.nm_x.delete(0, tk.END)
            self.nm_y.delete(0, tk.END)
            self.nm_w.delete(0, tk.END)
            self.nm_h.delete(0, tk.END)
            self.nm_x.insert(0, str(x))
            self.nm_y.insert(0, str(y))
            self.nm_w.insert(0, str(w))
            self.nm_h.insert(0, str(h))

        self.log_queue.put(("LOG", "Calibration complete."))
        self._update_config_data()
        save_config(self.config_data)

    def start_bot(self):
        if not self.window_id or not self.window_bounds:
            self.log_queue.put(("LOG", "You must find the PBO window first!"))
            return

        self.stop_event.clear()
        self.pause_event.clear()

        try:
            pb_region = (
                int(self.pb_x.get()),
                int(self.pb_y.get()),
                int(self.pb_w.get()),
                int(self.pb_h.get())
            )
            nm_region = (
                int(self.nm_x.get()),
                int(self.nm_y.get()),
                int(self.nm_w.get()),
                int(self.nm_h.get())
            )
        except ValueError:
            self.log_queue.put(("LOG", "Error: Invalid integer in region fields."))
            return

        self.log_queue.put(("LOG", "Starting bot..."))

        # Update config
        self._update_config_data()
        save_config(self.config_data)

        selected_move = self.move_choice_var.get()

        self.bot_thread = BotThread(
            window_id=self.window_id,
            window_bounds=self.window_bounds,
            pokeball_region=pb_region,
            name_region=nm_region,
            stop_event=self.stop_event,
            pause_event=self.pause_event,
            log_queue=self.log_queue,
            target_pokemons=self.target_pokemons,
            move_choice=selected_move
        )
        self.bot_thread.start()

    def pause_bot(self):
        if not self.pause_event.is_set():
            self.pause_event.set()
            self.log_queue.put(("LOG", "Bot paused."))
            self.pause_btn.config(text="Resume")
        else:
            self.pause_event.clear()
            self.log_queue.put(("LOG", "Bot resumed."))
            self.pause_btn.config(text="Pause")

    def stop_bot(self):
        if self.bot_thread and self.bot_thread.is_alive():
            self.log_queue.put(("LOG", "Stopping bot..."))
            self.stop_event.set()
        self.pause_event.clear()
        self.pause_btn.config(text="Pause")

    def _update_config_data(self):
        try:
            self.config_data["pokeball_region"] = [
                int(self.pb_x.get()),
                int(self.pb_y.get()),
                int(self.pb_w.get()),
                int(self.pb_h.get())
            ]
            self.config_data["name_region"] = [
                int(self.nm_x.get()),
                int(self.nm_y.get()),
                int(self.nm_w.get()),
                int(self.nm_h.get())
            ]
        except ValueError:
            pass
        self.config_data["target_pokemons"] = self.target_pokemons
        self.config_data["preferred_move"] = self.move_choice_var.get()

    # Thread-safe logging
    def _poll_log_queue(self):
        while True:
            try:
                message_tuple = self.log_queue.get_nowait()
            except queue.Empty:
                break
            else:
                msg_type, msg_text = message_tuple
                if msg_type == "LOG":
                    self._log_message_to_widget(msg_text)
        self.root.after(100, self._poll_log_queue)

    def _log_message_to_widget(self, message):
        self.log_text.configure(state='normal')
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see('end')
        self.log_text.configure(state='disabled')

# -------------------------
# Main
# -------------------------
if __name__ == "__main__":
    # Point to your Tesseract if needed:
    # pytesseract.pytesseract.tesseract_cmd = r"/usr/local/bin/tesseract"

    root = tk.Tk()
    app = BotGUI(root)
    root.mainloop()
