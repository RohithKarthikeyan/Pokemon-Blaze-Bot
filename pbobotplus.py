import cv2
import numpy as np
import pytesseract
import time
from difflib import SequenceMatcher
import os
import win32gui
import win32con
import win32api
import win32ui
from PIL import Image

# do pip install for all the above libraries
# make sure to install tesseract first
pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'

GAME_WINDOW_TITLE = "Pokemon Blaze Online"

REDDISH_CHECK_COORD = (1390, 340)  # (x, y) # change this

# Region where the Pokémon name appears (within the client area)
POKEMON_NAME_REGION = (1224, 339, 145, 20)  # change this
# left, top, width, height

target_pokemons = ["Electivire", "Dustox"]  # pokemon to not eliminate

def debug_save_image(im, name_prefix="debug"):
    filename = f"{name_prefix}_{int(time.time())}.png"
    im.save(filename)
    print(f"Saved debug image to: {os.path.abspath(filename)}")

def get_game_hwnd(title=GAME_WINDOW_TITLE):
    hwnd = win32gui.FindWindow(None, title)
    if not hwnd:
        raise Exception(f"Window not found: {title}")
    return hwnd


# dont change this
def capture_window(hwnd, region=None):
    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    width = right - left
    height = bottom - top

    hwndDC = win32gui.GetWindowDC(hwnd)
    mfcDC  = win32ui.CreateDCFromHandle(hwndDC)
    saveDC = mfcDC.CreateCompatibleDC()

    saveBitMap = win32ui.CreateBitmap()
    saveBitMap.CreateCompatibleBitmap(mfcDC, width, height)

    saveDC.SelectObject(saveBitMap)

    saveDC.BitBlt((0, 0), (width, height), mfcDC, (0, 0), win32con.SRCCOPY)

    bmpinfo = saveBitMap.GetInfo()
    bmpstr  = saveBitMap.GetBitmapBits(True)
    im = Image.frombuffer(
        'RGB',
        (bmpinfo['bmWidth'], bmpinfo['bmHeight']),
        bmpstr, 'raw', 'BGRX', 0, 1
    )

    win32gui.DeleteObject(saveBitMap.GetHandle())
    saveDC.DeleteDC()
    mfcDC.DeleteDC()
    win32gui.ReleaseDC(hwnd, hwndDC)

    if region:
        x, y, w, h = region
        im = im.crop((x, y, x + w, y + h))

    return im

def get_pixel_color(hwnd, x, y):
    region_img = capture_window(hwnd, region=(x, y, 20, 20))
    # debug_save_image(region_img, name_prefix="pokeball_region")
    return region_img.getpixel((0, 0))  # (R, G, B)

def is_predefined_coord_reddish(hwnd, x, y):
    r, g, b = get_pixel_color(hwnd, x, y)
    print(f"Pixel color at ({x}, {y}): R={r}, G={g}, B={b}")
    return (r > 150) and (g < 100) and (b < 100)

# DO NOT CHANGE THIS
def press_key(hwnd, key_code, hold_time=0.25):
    # For extended keys like arrows, set bit 24
    # lParam bits: (repeat=1) | (extended=1<<24)
    lParam_down = 1 | (1 << 24)

    # For WM_KEYUP, also set bits 30 (previous key state) and 31 (transition state)
    # i.e. 1 | (1<<24) | (1<<30) | (1<<31)
    lParam_up = 1 | (1 << 24) | (1 << 30) | (1 << 31)

    # Key down
    win32api.PostMessage(hwnd, win32con.WM_KEYDOWN, key_code, lParam_down)
    time.sleep(hold_time)
    # Key up
    win32api.PostMessage(hwnd, win32con.WM_KEYUP, key_code, lParam_up)

def move_in_bushes(hwnd):
    # use virtual key codes for arrow keys
    global last_direction
    if last_direction == "right":
        press_key(hwnd, 0x25)  # Left arrow
        last_direction = "left"
    else:
        press_key(hwnd, 0x27)  # Right arrow
        last_direction = "right"

def get_pokemon_name(hwnd):
    im = capture_window(hwnd, region=POKEMON_NAME_REGION)
    np_img = np.array(im)
    gray = cv2.cvtColor(np_img, cv2.COLOR_RGB2GRAY)
    _, binary = cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY)
    text = pytesseract.image_to_string(binary, config='--psm 7')
    return text.strip()

def is_similar(name, target, threshold=0.7):
    return SequenceMatcher(None, name.lower(), target.lower()).ratio() >= threshold

def defeat_wild_pokemon(hwnd):
    # R keycode is 0x52
    # 1 keycode is 0x31
    # 2 keycode is 0x32
    # 3 keycode is 0x33
    # 4 keycode is 0x34
    press_key(hwnd, 0x33, hold_time=0.1)

# Main script
last_direction = "right"

def main():
    hwnd = get_game_hwnd(GAME_WINDOW_TITLE)
    print("Starting script...")

    while True:
        move_in_bushes(hwnd)

        # Check if encounter occurred
        if is_predefined_coord_reddish(hwnd, *REDDISH_CHECK_COORD):
            print("Wild Pokémon encountered!")

            # OCR to get the Pokémon's name
            pokemon_name = get_pokemon_name(hwnd)
            print(f"Encountered Pokémon: {pokemon_name}")

            # Check if it's a target Pokémon
            if any(is_similar(pokemon_name, target) for target in target_pokemons):
                print(f"Target Pokémon {pokemon_name} encountered! Skipping battle.")
                break
            else:
                print("Defeating wild Pokémon...")
                defeat_wild_pokemon(hwnd)
                time.sleep(0.25)
if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Script stopped by user.")
