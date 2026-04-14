#!/usr/bin/env python3
"""
Double-clap screen blocker + triple-clap YouTube — PS2 EyeToy mic

  2x klaśnięcie (gap 0.40–0.80s) → blokada/odblokowanie ekranów
  3x klaśnięcie (gap 0.20–0.50s) → otwiera YouTube w przeglądarce
  hasło 'bollocks'               → odblokowanie ekranów

Usage:
    python3 clap_detect.py [--device N] [--debug]

Dependencies:
    pip install sounddevice numpy pynput --break-system-packages
    sudo dnf install python3-tkinter
"""

import argparse
import queue
import re
import subprocess
import threading
import time
import tkinter as tk
import webbrowser

import numpy as np
import sounddevice as sd

# ── Config ────────────────────────────────────────────────────────────────────

CHUNK_FRAMES = 1024
FRAME_SIZE   = 256

LOW_HZ_MIN  =   80
LOW_HZ_MAX  = 1500
HIGH_HZ_MIN = 1500
HIGH_HZ_MAX = 8000

RATIO_MIN      = 15.0   # pełne klaśnięcie
RATIO_MAX      = 69.0
NEAR_RATIO_MIN =  8.0   # "prawie klaśnięcie" — słabsze uderzenie
NEAR_RATIO_MAX = 29.0
MIN_RMS        = 0.10

# Podwójne klaśnięcie — blokada ekranu
DOUBLE_MIN_GAP = 0.40
DOUBLE_MAX_GAP = 0.80

# Sekwencja YouTube: klap + klap + prawie (każdy gap 0.20–0.60s)
YOUTUBE_SEQ_GAP_MIN = 0.20
YOUTUBE_SEQ_GAP_MAX = 0.60

YOUTUBE_COOLDOWN = 15.0
PASSWORD = "bollocks"
YOUTUBE_URL = "https://www.youtube.com/watch?v=0mfJn604GT4"

CANDIDATE_RATES = [44100, 48000, 22050, 16000, 11025, 8000]

# ── Shared state ──────────────────────────────────────────────────────────────

cmd_queue: "queue.Queue[str]" = queue.Queue()
blocked              = False
last_youtube_trigger = 0.0
typed_buffer         = ""
sample_rate          = 16000
debug_mode           = False

# Historia klaśnięć: lista krotek (timestamp, kind) gdzie kind = 'clap' | 'near'
clap_history: list[tuple[float, str]] = []

# ── Peak-frame FFT ────────────────────────────────────────────────────────────

def frame_ratio(frame, rate):
    rms = float(np.sqrt(np.mean(frame ** 2)))
    if rms < MIN_RMS / 3:
        return rms, 0.0
    window = np.hanning(len(frame))
    mag    = np.abs(np.fft.rfft(frame * window))
    freqs  = np.fft.rfftfreq(len(frame), d=1.0 / rate)
    def energy(f0, f1):
        return float(np.sum(mag[(freqs >= f0) & (freqs < f1)] ** 2))
    e_lo  = energy(LOW_HZ_MIN,  LOW_HZ_MAX)
    e_hi  = energy(HIGH_HZ_MIN, min(HIGH_HZ_MAX, rate / 2))
    return rms, e_hi / (e_lo + 1e-10)


def best_frame(chunk, rate, frame_size):
    hop = frame_size // 2
    best_rms, best_ratio = 0.0, 0.0
    for start in range(0, len(chunk) - frame_size + 1, hop):
        rms, ratio = frame_ratio(chunk[start:start + frame_size], rate)
        if ratio > best_ratio:
            best_rms, best_ratio = rms, ratio
    return best_rms, best_ratio


# ── Gesture detection ─────────────────────────────────────────────────────────

def detect_gesture(now, kind):
    """
    Dodaje zdarzenie do historii i sprawdza gesty:
      'youtube' — klap, klap, prawie  (każdy gap YOUTUBE_SEQ_GAP_MIN–MAX)
      'double'  — klap, klap          (gap DOUBLE_MIN_GAP–MAX)
    Zwraca wykryty gest lub None.
    """
    global clap_history

    clap_history.append((now, kind))
    # Usuń stare wpisy (> 2s)
    clap_history = [(t, k) for (t, k) in clap_history if now - t < 2.0]

    n = len(clap_history)

    # Sekwencja YouTube: ostatnie 3 w odpowiednich gapach, wzorzec:
    #   clap + clap + clap  LUB  clap + near + clap
    if n >= 3:
        (t1, k1), (t2, k2), (t3, k3) = clap_history[-3], clap_history[-2], clap_history[-1]
        g1 = t2 - t1
        g2 = t3 - t2
        gaps_ok = (YOUTUBE_SEQ_GAP_MIN < g1 < YOUTUBE_SEQ_GAP_MAX and
                   YOUTUBE_SEQ_GAP_MIN < g2 < YOUTUBE_SEQ_GAP_MAX)
        pattern_ok = ((k1 == "clap" and k2 == "clap" and k3 == "clap") or
                      (k1 == "clap" and k2 == "near" and k3 == "clap"))
        if gaps_ok and pattern_ok:
            clap_history.clear()
            return "youtube"

    # Podwójne klaśnięcie: ostatnie 2 = clap, clap
    if n >= 2:
        (t1, k1), (t2, k2) = clap_history[-2], clap_history[-1]
        gap = t2 - t1
        if k1 == "clap" and k2 == "clap" and DOUBLE_MIN_GAP < gap < DOUBLE_MAX_GAP:
            clap_history.clear()
            return "double"

    return None


# ── Audio callback ────────────────────────────────────────────────────────────

def audio_callback(indata, frames, time_info, status):
    global last_youtube_trigger, blocked, sample_rate

    now   = time.monotonic()
    chunk = indata[:, 0]
    rms, ratio = best_frame(chunk, sample_rate, FRAME_SIZE)

    is_clap = (RATIO_MIN <= ratio <= RATIO_MAX) and (rms >= MIN_RMS)
    is_near = (NEAR_RATIO_MIN <= ratio < RATIO_MIN) and (rms >= MIN_RMS)

    if debug_mode and rms >= MIN_RMS / 3:
        if is_clap:
            kind_str = "KLAP"
        elif is_near:
            kind_str = "prawie"
        else:
            kind_str = ""
        mark = f" ← {kind_str}" if kind_str else ""
        print(f"  RMS={rms:.3f}  HF/LF={ratio:6.1f}{mark}")

    # Podczas youtube cooldown — nic nie przechodzi
    if now - last_youtube_trigger < YOUTUBE_COOLDOWN:
        if is_clap or is_near:
            remaining = YOUTUBE_COOLDOWN - (now - last_youtube_trigger)
            if debug_mode:
                print(f"  [blokada yt cooldown, jeszcze {remaining:.1f}s]")
        return

    if not (is_clap or is_near):
        return

    kind    = "clap" if is_clap else "near"
    gesture = detect_gesture(now, kind)

    if gesture == "youtube":
        last_youtube_trigger = now   # ustaw PRZED otwarciem
        print(f"  [>>] klap+klap+prawie → YouTube!")
        cmd_queue.put("youtube")

    elif gesture == "double":
        print(f"  [>>] Podwójne klaśnięcie → {'unblock' if blocked else 'block'}")
        cmd_queue.put("unblock" if blocked else "block")


# ── Keyboard listener ─────────────────────────────────────────────────────────

def keyboard_thread_func():
    global typed_buffer

    try:
        from pynput import keyboard

        def on_press(key):
            global typed_buffer
            try:
                ch = key.char
            except AttributeError:
                typed_buffer = ""
                return
            if ch is None:
                typed_buffer = ""
                return
            typed_buffer += ch
            if len(typed_buffer) > len(PASSWORD) + 2:
                typed_buffer = typed_buffer[-len(PASSWORD):]
            if typed_buffer.endswith(PASSWORD):
                typed_buffer = ""
                if blocked:
                    print("[kbd] Hasło poprawne — odblokowuję")
                    cmd_queue.put("unblock")

        with keyboard.Listener(on_press=on_press) as listener:
            listener.join()

    except Exception as e:
        print(f"[kbd] Listener klawiatury niedostępny: {e}")


# ── Screen geometry ───────────────────────────────────────────────────────────

def get_screen_geometries():
    try:
        out = subprocess.run(
            ["xrandr", "--current"], capture_output=True, text=True, timeout=3
        ).stdout
        pattern = re.compile(r"(\d+)x(\d+)\+(\d+)\+(\d+)")
        screens = []
        for line in out.splitlines():
            if " connected" in line:
                m = pattern.search(line)
                if m:
                    w, h, x, y = map(int, m.groups())
                    screens.append((x, y, w, h))
        return screens
    except Exception:
        return []


# ── Tk overlay manager ────────────────────────────────────────────────────────

class OverlayManager:
    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.overlays: list[tk.Toplevel] = []
        self._schedule_poll()

    def _schedule_poll(self):
        self.root.after(80, self._poll)

    def _poll(self):
        global blocked
        try:
            while True:
                cmd = cmd_queue.get_nowait()
                if cmd == "block" and not blocked:
                    blocked = True
                    self._show()
                elif cmd == "unblock" and blocked:
                    blocked = False
                    self._hide()
                elif cmd == "youtube":
                    webbrowser.open(YOUTUBE_URL)
                    print(f"  [yt] Otwarto: {YOUTUBE_URL}")
        except queue.Empty:
            pass
        self._schedule_poll()

    def _show(self):
        screens = get_screen_geometries()
        if not screens:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            screens = [(0, 0, sw, sh)]
        for (x, y, w, h) in screens:
            win = tk.Toplevel(self.root)
            win.configure(bg="black")
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            win.geometry(f"{w}x{h}+{x}+{y}")
            win.lift()
            win.protocol("WM_DELETE_WINDOW", lambda: None)
            self.overlays.append(win)
        print(f"  [overlay] Nałożono {len(screens)} ekran(ów)")

    def _hide(self):
        for win in self.overlays:
            try:
                win.destroy()
            except Exception:
                pass
        self.overlays.clear()
        print("  [overlay] Zdjęto blokadę")

    def run(self):
        self.root.mainloop()


# ── Helpers ───────────────────────────────────────────────────────────────────

def detect_sample_rate(device_index):
    for rate in CANDIDATE_RATES:
        try:
            sd.check_input_settings(device=device_index, channels=1, samplerate=rate)
            return rate
        except Exception:
            continue
    raise RuntimeError(f"Brak obsługiwanej częstotliwości dla urządzenia {device_index}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global RATIO_MIN, RATIO_MAX, MIN_RMS, sample_rate, debug_mode

    parser = argparse.ArgumentParser(description="Clap detector — blokada ekranu + YouTube")
    parser.add_argument("--device",    type=int,   default=None)
    parser.add_argument("--ratio-min", type=float, default=RATIO_MIN)
    parser.add_argument("--ratio-max", type=float, default=RATIO_MAX)
    parser.add_argument("--min-rms",   type=float, default=MIN_RMS)
    parser.add_argument("--debug",     action="store_true")
    args = parser.parse_args()

    RATIO_MIN  = args.ratio_min
    RATIO_MAX  = args.ratio_max
    MIN_RMS    = args.min_rms
    debug_mode = args.debug

    print("Dostępne urządzenia wejściowe:")
    print(sd.query_devices())
    print()

    idx  = args.device if args.device is not None else sd.default.device[0]
    info = sd.query_devices(idx, "input")
    print(f"Używam: [{info['index']}] {info['name']}")

    sample_rate = detect_sample_rate(idx)
    print(f"Sample rate: {sample_rate} Hz")
    print(f"Klaśnięcie = HF/LF [{RATIO_MIN}–{RATIO_MAX}] AND RMS >= {MIN_RMS}")
    print(f"Prawie     = HF/LF [{NEAR_RATIO_MIN}–{NEAR_RATIO_MAX}] AND RMS >= {MIN_RMS}")
    print(f"  2x klap  (gap {DOUBLE_MIN_GAP}–{DOUBLE_MAX_GAP}s)      → blokada ekranu")
    print(f"  klap+klap+prawie (gap {YOUTUBE_SEQ_GAP_MIN}–{YOUTUBE_SEQ_GAP_MAX}s) → YouTube (cooldown {YOUTUBE_COOLDOWN}s)")
    print(f"  hasło '{PASSWORD}'                    → odblokowanie")
    print()

    threading.Thread(target=keyboard_thread_func, daemon=True).start()

    with sd.InputStream(
        device=args.device,
        channels=1,
        samplerate=sample_rate,
        blocksize=CHUNK_FRAMES,
        dtype="float32",
        callback=audio_callback,
    ):
        try:
            manager = OverlayManager()
            manager.run()
        except KeyboardInterrupt:
            print("\nZatrzymano.")


if __name__ == "__main__":
    main()
