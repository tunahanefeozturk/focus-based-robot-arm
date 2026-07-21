# -*- coding: utf-8 -*-
"""
headset_client.py — Emotiv EPOC X → Focus-Based UDP State Sender

Connects to the Emotiv EPOC X headset via the Cortex API, subscribes to the
Performance Metrics ('met') data stream, and reads the user's Focus
(Engagement) level.

Protocol
--------
    Target : Raspberry Pi  (configurable IP, default port 5005)
    Payload: {"cycle_active": true}   when Focus > FOCUS_THRESHOLD
             {"cycle_active": false}  when Focus <= FOCUS_THRESHOLD

Architecture
------------
    The client is intentionally simple.  It does NOT decide which servos
    to move or how far — that logic lives entirely on the server
    (udp_servo_server.py).  This script's only job is to relay whether
    the user is mentally "focused" enough to drive the robotic arm.

Performance Metrics (met) Label Layout
--------------------------------------
    Index  Label
    -----  ---------------
      0    eng.isActive
      1    eng             (Engagement score)
      2    exc.isActive
      3    exc             (Excitement score)
      4    lex             (Long-term Excitement)
      5    str.isActive
      6    str             (Stress score)
      7    rel.isActive
      8    rel             (Relaxation score)
      9    int.isActive
     10    int             (Interest score)
     11    foc.isActive
     12    foc             (Focus score)        ← WE USE THIS
"""

import os
import sys
import json
import socket
import time
import threading
import tkinter as tk

# ---------------------------------------------------------------------------
# Cortex SDK import (same strategy as demo2.py)
# ---------------------------------------------------------------------------
_cortex_path = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'cortex-example-master', 'python',
)
if os.path.exists(_cortex_path):
    sys.path.insert(0, _cortex_path)

from cortex import Cortex

# =========================================================================
# CONFIGURATION
# =========================================================================

# Cortex API credentials (extracted from demo2.py / record2.py)
APP_CLIENT_ID     = 'ElQmzt08lrwnQOF6DBicPHZzm0fbb6CBC8TB2euW'
APP_CLIENT_SECRET = (
    'GFfrahhXrsphhD8ask7t4fI6Ck1f3aIMmSGdtnixjQYLOBE46mwAphOz7NVdRpWk'
    '39YSIBVoFlV2DFsfpA7YHdTMfDct1D2VeyCM4cNgyETJUW7cXy856S56MLWH7Wre'
)

# UDP target — change RASPBERRY_PI_IP to your Pi's address
RASPBERRY_PI_IP = '192.168.152.14'
UDP_PORT        = 5005

# ---------------------------------------------------------------------------
# FOCUS THRESHOLD
# ---------------------------------------------------------------------------
# The Cortex 'met' stream returns Focus (foc) as a float in [0.0, 1.0].
# Values above this threshold trigger cycle_active = true.
FOCUS_THRESHOLD = 0.80

# ---------------------------------------------------------------------------
# RATE LIMITING
# ---------------------------------------------------------------------------
# Minimum interval between consecutive UDP sends (seconds).
# 0.05 s → max 20 Hz,  0.10 s → max 10 Hz.
# This keeps the send rate between 10–20 packets/second.
SEND_INTERVAL = 0.07   # ~14 Hz — comfortable middle ground

# How often to print a debug line (every N-th met sample)
DEBUG_PRINT_INTERVAL = 5

# ---------------------------------------------------------------------------
# met stream column indices (from Cortex documentation & sub_data.py)
# ---------------------------------------------------------------------------
# ['eng.isActive', 'eng', 'exc.isActive', 'exc', 'lex',
#  'str.isActive', 'str', 'rel.isActive', 'rel',
#  'int.isActive', 'int', 'foc.isActive', 'foc']
IDX_FOC_IS_ACTIVE = 11
IDX_FOC           = 12


# =========================================================================
# CROSSHAIR OVERLAY  (record2.py'deki "+" sembolünün karşılığı)
# =========================================================================

class CrosshairOverlay:
    """
    Ekranın ortasında her zaman görünen, saydam, tıklanabilir olmayan
    bir "+" sembolü penceresi oluşturur.
    """

    def __init__(self):
        self._thread = None
        self._root = None

    def start(self):
        """Overlay'i ayrı bir thread'de başlat."""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        root = tk.Tk()
        self._root = root

        # --- Pencere ayarları ---
        root.title('Crosshair')
        root.attributes('-topmost', True)       # Her zaman üstte
        root.overrideredirect(True)             # Çerçevesiz pencere

        # Pencere boyutu ve konum — ekranın tam ortası
        size = 60
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        x = (screen_w - size) // 2
        y = (screen_h - size) // 2
        root.geometry(f'{size}x{size}+{x}+{y}')

        # --- Saydam (transparan) arka plan ---
        transparent_color = '#010101'           # Saydamlık anahtarı rengi
        root.configure(bg=transparent_color)
        root.attributes('-transparentcolor', transparent_color)

        # --- "+" sembolünü çiz ---
        label = tk.Label(
            root,
            text='+',
            font=('Arial', 36, 'bold'),
            fg='white',
            bg=transparent_color,
        )
        label.place(relx=0.5, rely=0.5, anchor='center')

        root.mainloop()

    def stop(self):
        """Overlay penceresini kapat."""
        if self._root:
            try:
                self._root.after(0, self._root.destroy)
            except Exception:
                pass


# =========================================================================
# HEADSET CLIENT
# =========================================================================

class FocusClient:
    """
    Connects to the Emotiv EPOC X headset, reads the Focus performance
    metric, and sends {"cycle_active": true/false} over UDP.
    """

    def __init__(self, app_client_id: str, app_client_secret: str, **kwargs):
        # --- Cortex handle --------------------------------------------------
        self.c = Cortex(app_client_id, app_client_secret, debug_mode=False, **kwargs)

        # --- Bind Cortex events ---------------------------------------------
        self.c.bind(create_session_done=self.on_create_session_done)
        self.c.bind(new_data_labels=self.on_new_data_labels)
        self.c.bind(new_met_data=self.on_new_met_data)
        self.c.bind(inform_error=self.on_inform_error)

        # --- UDP socket (non-blocking, fire-and-forget) ---------------------
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.target_addr = (RASPBERRY_PI_IP, UDP_PORT)

        # --- Rate-limit state -----------------------------------------------
        self._last_send_time = 0.0
        self._last_state_sent = None   # None / True / False

        # --- Sample counter for debug prints --------------------------------
        self._met_count = 0

        # --- Thread safety --------------------------------------------------
        self._lock = threading.Lock()

    # =====================================================================
    # PUBLIC API
    # =====================================================================

    def start(self, headset_id: str = ''):
        """Open the Cortex connection (blocks on the websocket thread)."""
        print('\n' + '=' * 60)
        print('  EMOTIV EPOC X  →  FOCUS-BASED UDP CLIENT')
        print('=' * 60)
        print(f'   Target IP       : {self.target_addr[0]}:{self.target_addr[1]}')
        print(f'   Focus threshold : {FOCUS_THRESHOLD:.0%}')
        print(f'   Send interval   : {SEND_INTERVAL*1000:.0f} ms  ({1/SEND_INTERVAL:.0f} Hz max)')
        print('=' * 60)

        # --- Ekran ortasında "+" sembolünü göster ---
        self._crosshair = CrosshairOverlay()
        self._crosshair.start()
        print('  ✚  Odaklanma çapraz işareti açıldı  ✚')

        if headset_id:
            self.c.set_wanted_headset(headset_id)

        print('Cortex baglantisi aciliyor...')
        self.c.open()                       # blocks until ws closes

    # =====================================================================
    # CORTEX EVENT HANDLERS
    # =====================================================================

    def on_create_session_done(self, *args, **kwargs):
        """Session ready → subscribe to the met (Performance Metrics) stream."""
        print('Session olusturuldu -- met stream abone olunuyor...')
        self.c.sub_request(['met'])

    def on_new_data_labels(self, *args, **kwargs):
        """Receive column labels for subscribed streams (informational)."""
        data = kwargs.get('data')
        stream_name = data['streamName']
        stream_labels = data['labels']
        print(f'{stream_name} labels: {stream_labels}')

    # --------------------------------------------------------------------- #
    #  PERFORMANCE METRICS (Focus reading)                                    #
    # --------------------------------------------------------------------- #

    def on_new_met_data(self, *args, **kwargs):
        """
        Performance Metrics callback.

        Reads the Focus (foc) value from the met stream and sends the
        appropriate cycle_active state over UDP.

        met payload structure (from cortex.py handle_stream_data):
            data['met'] → list of floats/bools matching the met labels
            data['time'] → float timestamp
        """
        data = kwargs.get('data')
        met = data.get('met', [])

        if len(met) < IDX_FOC + 1:
            return                             # incomplete packet

        foc_is_active = met[IDX_FOC_IS_ACTIVE]
        foc_value     = met[IDX_FOC]

        self._met_count += 1

        # Determine desired state
        if not foc_is_active:
            # Focus metric is not active (headset may still be warming up)
            cycle_active = False
        else:
            cycle_active = foc_value > FOCUS_THRESHOLD

        # --- Debug print (throttled) ---
        if self._met_count % DEBUG_PRINT_INTERVAL == 0:
            status = 'AKTIF ✓' if cycle_active else 'PASIF ✗'
            bar_len = int(foc_value * 20) if isinstance(foc_value, (int, float)) else 0
            bar = '█' * bar_len + '░' * (20 - bar_len)
            print(
                f'  #{self._met_count:5d}  '
                f'Focus={foc_value:.3f}  '
                f'[{bar}]  '
                f'Esik={FOCUS_THRESHOLD:.2f}  '
                f'→ {status}'
            )

        # --- Rate-limited UDP send ---
        self._maybe_send(cycle_active)

    # =====================================================================
    # INTERNAL HELPERS
    # =====================================================================

    def _send_udp(self, cycle_active: bool):
        """Build JSON payload and send via UDP."""
        payload = json.dumps({'cycle_active': cycle_active})
        try:
            self.sock.sendto(payload.encode('utf-8'), self.target_addr)
        except OSError as exc:
            print(f'UDP gonderme hatasi: {exc}')

    def _maybe_send(self, cycle_active: bool):
        """
        Rate-limited send.

        Always sends when the state changes (true↔false).
        When the state is unchanged, sends at most once per SEND_INTERVAL
        to keep the server's "last-heard" timer fresh.
        """
        now = time.time()
        elapsed = now - self._last_send_time

        state_changed = (cycle_active != self._last_state_sent)

        if not state_changed and elapsed < SEND_INTERVAL:
            return                             # throttled — skip this sample

        self._send_udp(cycle_active)
        self._last_send_time = now
        self._last_state_sent = cycle_active

        if state_changed:
            status = 'AKTIF ✓' if cycle_active else 'PASIF ✗'
            print(
                f'  >>> UDP → {self.target_addr}  |  '
                f'cycle_active={cycle_active}  ({status})'
            )

    # --------------------------------------------------------------------- #
    #  ERROR HANDLER                                                         #
    # --------------------------------------------------------------------- #

    def on_inform_error(self, *args, **kwargs):
        error_data = kwargs.get('error_data')
        print(f'Cortex hatasi: {error_data}')


# =========================================================================
# MAIN
# =========================================================================

def main():
    global RASPBERRY_PI_IP, UDP_PORT, FOCUS_THRESHOLD, SEND_INTERVAL
    import argparse

    parser = argparse.ArgumentParser(
        description='Emotiv EPOC X → Focus-Based UDP Client',
    )
    parser.add_argument(
        '--ip', default=RASPBERRY_PI_IP,
        help=f'Raspberry Pi IP address (default: {RASPBERRY_PI_IP})',
    )
    parser.add_argument(
        '--port', type=int, default=UDP_PORT,
        help=f'UDP port (default: {UDP_PORT})',
    )
    parser.add_argument(
        '--headset', default='',
        help='Emotiv headset ID (auto-detect if omitted)',
    )
    parser.add_argument(
        '--threshold', type=float, default=FOCUS_THRESHOLD,
        help=f'Focus threshold 0.0-1.0 (default: {FOCUS_THRESHOLD})',
    )
    parser.add_argument(
        '--rate', type=float, default=1.0 / SEND_INTERVAL,
        help=f'Max UDP send rate in Hz (default: {1.0/SEND_INTERVAL:.0f})',
    )
    parser.add_argument(
        '--debug', action='store_true',
        help='Enable Cortex debug logging',
    )
    args = parser.parse_args()

    # Apply CLI overrides to module-level config
    RASPBERRY_PI_IP  = args.ip
    UDP_PORT         = args.port
    FOCUS_THRESHOLD  = args.threshold
    SEND_INTERVAL    = 1.0 / max(1.0, args.rate)

    client = FocusClient(APP_CLIENT_ID, APP_CLIENT_SECRET)
    client.target_addr = (RASPBERRY_PI_IP, UDP_PORT)

    if args.debug:
        client.c.debug = True

    try:
        client.start(headset_id=args.headset)
    except KeyboardInterrupt:
        print('\nIstemci durduruldu.')
    except Exception as exc:
        print(f'\nBeklenmeyen hata: {exc}')
        import traceback
        traceback.print_exc()
    finally:
        client.sock.close()
        try:
            client.c.close()
        except Exception:
            pass


if __name__ == '__main__':
    main()
