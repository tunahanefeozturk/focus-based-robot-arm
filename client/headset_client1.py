"""
headset_client.py — Emotiv EPOC X → UDP Command Sender

Connects to the Emotiv EPOC X headset via the Cortex API, subscribes to the
motion ('mot') and facial-expression ('fac') data streams, and translates
head movements + blink events into JSON commands sent over UDP.

Protocol
--------
    Target : Raspberry Pi  (configurable IP, default port 5005)
    Payload: {"command": "<sag|sol|yukari|asagi|dur>", "mode": <1|2>}

State Machine
-------------
    mode starts at 1.  Every detected *blink* toggles mode between 1 and 2.

Motion Mapping  (position-based, relative to calibrated baseline)
-----------------------------------------------------------------
    Uses Euler angles (yaw, pitch) derived from the quaternion stream.
    On startup, the first CALIBRATION_SAMPLES are averaged to establish
    the "looking straight ahead" baseline.

    |relative_yaw|   > THRESHOLD_YAW   → "sag" / "sol"
    |relative_pitch| > THRESHOLD_PITCH  → "yukari" / "asagi"
    both inside DEADZONE                → "dur"  (stop)

Tuning
------
    Run the script and observe the printed relative yaw/pitch values
    while moving your head.  Adjust DEADZONE and THRESHOLD_* until
    the sensitivity feels right for your setup.
"""

import os
import sys
import json
import math
import socket
import time
import threading

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
# MOTION SENSITIVITY  (tune these while watching the console output)
# ---------------------------------------------------------------------------
# Values are in DEGREES of head tilt relative to the calibrated baseline.
#
#   DEADZONE   — head movements smaller than this are ignored entirely.
#                Prevents drift / noise from triggering commands.
#
#   THRESHOLD  — head must tilt at least this far to trigger a command.
#                Must be >= DEADZONE.  Larger = less sensitive.
#
# Suggested starting workflow:
#   1. Run the script, keep your head still during the 20-sample calibration.
#   2. Slowly tilt your head and watch the "rel_yaw / rel_pitch" printout.
#   3. Note the values at which a comfortable "intentional tilt" starts.
#   4. Set DEADZONE to ~half that value, THRESHOLD to the full value.
# ---------------------------------------------------------------------------
DEADZONE        = 3       # degrees (integer) — ignore jitter below this
THRESHOLD_YAW   = 5       # degrees (integer) — trigger sag / sol
THRESHOLD_PITCH  = 5       # degrees (integer) — trigger yukari / asagi

# Number of initial samples used to compute the baseline (look straight ahead)
CALIBRATION_SAMPLES = 20

# Blink debounce — ignore repeated blinks within this many seconds
BLINK_DEBOUNCE_SEC = 0.6

# Minimum interval between consecutive UDP sends (seconds)
# Matches the 20 Hz rate limit enforced by MOT_SLEEP_S
SEND_INTERVAL = 0.05

# Hard rate limit on the mot callback — sleep this long after each sample
# to cap the effective processing rate at 20 Hz max.
MOT_SLEEP_S = 0.05

# How often to print the debug line (every N-th mot sample)
DEBUG_PRINT_INTERVAL = 10

# Motion-data column layout from Cortex mot stream
# ['COUNTER_MEMS', 'INTERPOLATED_MEMS',
#  'Q0', 'Q1', 'Q2', 'Q3',          ← quaternion
#  'ACCX', 'ACCY', 'ACCZ',          ← accelerometer
#  'MAGX', 'MAGY', 'MAGZ']          ← magnetometer
IDX_Q0, IDX_Q1, IDX_Q2, IDX_Q3 = 2, 3, 4, 5


# =========================================================================
# HEADSET CLIENT
# =========================================================================

class HeadsetClient:
    """
    Connects to the Emotiv EPOC X headset, reads motion + facial-expression
    streams, and sends directional commands over UDP.
    """

    def __init__(self, app_client_id: str, app_client_secret: str, **kwargs):
        # --- Cortex handle --------------------------------------------------
        self.c = Cortex(app_client_id, app_client_secret, debug_mode=False, **kwargs)

        # --- Bind Cortex events (same pattern as demo2.py) ------------------
        self.c.bind(create_session_done=self.on_create_session_done)
        self.c.bind(new_data_labels=self.on_new_data_labels)
        self.c.bind(new_mot_data=self.on_new_mot_data)
        self.c.bind(new_fe_data=self.on_new_fe_data)
        self.c.bind(inform_error=self.on_inform_error)

        # --- State machine --------------------------------------------------
        self.mode = 1                       # toggled by blinks (1 ↔ 2)
        self._last_blink_time = 0.0         # debounce guard
        self._lock = threading.Lock()

        # --- UDP socket (non-blocking, fire-and-forget) ---------------------
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.target_addr = (RASPBERRY_PI_IP, UDP_PORT)
        self._last_send_time = 0.0
        self._last_command = None           # avoid spamming identical cmds

        # --- Calibration (dynamic baseline) ---------------------------------
        self._calibration_yaw_samples = []
        self._calibration_pitch_samples = []
        self._baseline_yaw = 0.0            # set after calibration
        self._baseline_pitch = 0.0
        self._is_calibrated = False

        # --- Sample counter for debug prints --------------------------------
        self._mot_count = 0

        # --- Data labels (informational) ------------------------------------
        self.mot_labels = []

    # =====================================================================
    # PUBLIC API
    # =====================================================================

    def start(self, headset_id: str = ''):
        """Open the Cortex connection (blocks on the websocket thread)."""
        print('\n' + '=' * 60)
        print('  EMOTIV EPOC X  ->  UDP COMMAND CLIENT')
        print('=' * 60)
        print(f'   Target IP    : {self.target_addr[0]}:{self.target_addr[1]}')
        print(f'   Deadzone     : {DEADZONE} deg')
        print(f'   Yaw thresh   : {THRESHOLD_YAW} deg')
        print(f'   Pitch thresh : {THRESHOLD_PITCH} deg')
        print(f'   Calibration  : first {CALIBRATION_SAMPLES} samples')
        print(f'   Blink dbnce  : {BLINK_DEBOUNCE_SEC}s')
        print('=' * 60)

        if headset_id:
            self.c.set_wanted_headset(headset_id)

        print('Cortex baglantisi aciliyor...')
        self.c.open()                       # blocks until ws closes

    # =====================================================================
    # CORTEX EVENT HANDLERS
    # =====================================================================

    def on_create_session_done(self, *args, **kwargs):
        """Session ready -> subscribe to mot + fac streams."""
        print('Session olusturuldu -- mot & fac stream abone olunuyor...')
        self.c.sub_request(['mot', 'fac'])

    def on_new_data_labels(self, *args, **kwargs):
        """Receive column labels for subscribed streams."""
        data = kwargs.get('data')
        stream_name = data['streamName']
        stream_labels = data['labels']
        print(f'{stream_name} labels: {stream_labels}')
        if stream_name == 'mot':
            self.mot_labels = stream_labels

    # --------------------------------------------------------------------- #
    #  FACIAL EXPRESSION (blink detection)                                   #
    # --------------------------------------------------------------------- #

    def on_new_fe_data(self, *args, **kwargs):
        """
        Facial-expression callback.

        Cortex fac payload (from cortex.py handle_stream_data):
            eyeAct : str   - e.g. "blink", "winkL", "winkR", "lookL", ...
            uAct   : str   - upper-face action
            uPow   : float - upper-face power
            lAct   : str   - lower-face action
            lPow   : float - lower-face power
            time   : float
        """
        data = kwargs.get('data')
        eye_action = data.get('eyeAct', '')

        if eye_action == 'blink':
            now = time.time()
            if now - self._last_blink_time < BLINK_DEBOUNCE_SEC:
                return                      # debounce
            self._last_blink_time = now

            with self._lock:
                self.mode = 2 if self.mode == 1 else 1

            print(f'BLINK algilandi -- mode -> {self.mode}')
            # Immediately inform the Pi about the mode switch
            self._send_udp('dur')

    # --------------------------------------------------------------------- #
    #  MOTION DATA  (position-based, relative to calibrated baseline)        #
    # --------------------------------------------------------------------- #

    def on_new_mot_data(self, *args, **kwargs):
        """
        Motion callback.

        Instead of computing angular *velocity* from quaternion deltas,
        we convert each quaternion sample to Euler angles (yaw, pitch)
        and compare against the calibrated baseline.  This means a
        *sustained tilt* continuously generates commands — no need to
        keep rotating.
        """
        data = kwargs.get('data')
        mot = data.get('mot', [])

        if len(mot) < 6:
            return                          # incomplete packet

        q = (mot[IDX_Q0], mot[IDX_Q1], mot[IDX_Q2], mot[IDX_Q3])
        yaw, pitch = self._quaternion_to_euler_yp(q)

        self._mot_count += 1

        # ---------- CALIBRATION PHASE ----------
        if not self._is_calibrated:
            self._calibration_yaw_samples.append(yaw)
            self._calibration_pitch_samples.append(pitch)

            remaining = CALIBRATION_SAMPLES - len(self._calibration_yaw_samples)
            if remaining > 0:
                if self._mot_count <= 3 or remaining % 5 == 0:
                    print(f'[CALIBRATION] Ornek toplaniyor... '
                          f'{len(self._calibration_yaw_samples)}/{CALIBRATION_SAMPLES}  '
                          f'(yaw={yaw:+.2f}, pitch={pitch:+.2f})')
                return

            # We have enough samples — compute baseline
            self._baseline_yaw = sum(self._calibration_yaw_samples) / len(self._calibration_yaw_samples)
            self._baseline_pitch = sum(self._calibration_pitch_samples) / len(self._calibration_pitch_samples)
            self._is_calibrated = True
            print('=' * 60)
            print(f'[CALIBRATION] TAMAMLANDI!')
            print(f'   Baseline yaw   = {self._baseline_yaw:+.4f} deg')
            print(f'   Baseline pitch  = {self._baseline_pitch:+.4f} deg')
            print(f'   Deadzone        = {DEADZONE} deg')
            print(f'   Threshold yaw   = {THRESHOLD_YAW} deg')
            print(f'   Threshold pitch = {THRESHOLD_PITCH} deg')
            print('=' * 60)
            return

        # ---------- NORMAL OPERATION ----------
        # 1. QUANTIZATION: round to nearest integer to kill sub-degree noise
        rel_yaw   = round(yaw   - self._baseline_yaw)
        rel_pitch = round(pitch - self._baseline_pitch)

        # 2. Map using the quantized integers
        command = self._map_command(rel_yaw, rel_pitch)

        # --- Debug: print the stabilized integer values for tuning ---
        if self._mot_count % DEBUG_PRINT_INTERVAL == 0:
            marker = '*' if command != 'dur' else ' '
            print(
                f'{marker} #{self._mot_count:5d}  '
                f'yaw={rel_yaw:+4d} deg  '
                f'pitch={rel_pitch:+4d} deg  '
                f'-> {command:6s}  '
                f'mode={self.mode}'
            )

        self._maybe_send(command)

        # 3. RATE LIMIT: hard-sleep to cap at 20 Hz max output rate
        time.sleep(MOT_SLEEP_S)

    # =====================================================================
    # INTERNAL HELPERS
    # =====================================================================

    @staticmethod
    def _quaternion_to_euler_yp(q):
        """
        Convert a unit quaternion (w, x, y, z) to yaw and pitch angles
        in degrees.

        Yaw   = rotation about the vertical (Z) axis   (left / right)
        Pitch = rotation about the lateral  (X) axis   (up / down)

        Uses the standard aerospace / Tait-Bryan ZYX convention.
        """
        w, x, y, z = q

        # Yaw (Z-axis rotation)
        siny_cosp = 2.0 * (w * z + x * y)
        cosy_cosp = 1.0 - 2.0 * (y * y + z * z)
        yaw = math.atan2(siny_cosp, cosy_cosp)

        # Pitch (X-axis rotation) — clamped to avoid NaN at gimbal lock
        sinp = 2.0 * (w * x - z * y)
        sinp = max(-1.0, min(1.0, sinp))
        pitch = math.asin(sinp)

        return math.degrees(yaw), math.degrees(pitch)

    @staticmethod
    def _map_command(rel_yaw: int, rel_pitch: int) -> str:
        """
        Determine the dominant directional command from quantized
        (integer) relative Euler angles.

        All comparisons use strict '>' against integer thresholds,
        so values exactly at the boundary are treated as neutral.

        Logic:
            1. If both |axes| <= DEADZONE → "dur" (stop).
            2. Pick the dominant axis (largest absolute value).
            3. If that axis > THRESHOLD → return the command.
            4. Otherwise → "dur".
        """
        abs_yaw   = abs(rel_yaw)
        abs_pitch = abs(rel_pitch)

        # Both inside deadzone → stop immediately
        if abs_yaw <= DEADZONE and abs_pitch <= DEADZONE:
            return 'dur'

        # Pick the dominant axis, strict '>' against threshold
        if abs_yaw >= abs_pitch:
            if abs_yaw > THRESHOLD_YAW:
                return 'sag' if rel_yaw > 0 else 'sol'
        else:
            if abs_pitch > THRESHOLD_PITCH:
                return 'yukari' if rel_pitch > 0 else 'asagi'

        # In between deadzone and threshold — no command
        return 'dur'

    # --------------------------------------------------------------------- #
    #  UDP SEND                                                              #
    # --------------------------------------------------------------------- #

    def _send_udp(self, command: str):
        """Build JSON payload and send via UDP."""
        with self._lock:
            mode = self.mode

        payload = json.dumps({'command': command, 'mode': mode})
        try:
            self.sock.sendto(payload.encode('utf-8'), self.target_addr)
        except OSError as exc:
            print(f'UDP gonderme hatasi: {exc}')

    def _maybe_send(self, command: str):
        """Rate-limited send — avoids flooding the network."""
        now = time.time()
        if now - self._last_send_time < SEND_INTERVAL:
            return
        # Skip repeated identical "dur" commands (one "dur" is enough to stop)
        if command == 'dur' and self._last_command == 'dur':
            return

        self._send_udp(command)
        self._last_send_time = now
        self._last_command = command

        if command != 'dur':
            print(f'UDP -> {self.target_addr}  |  cmd={command}  mode={self.mode}')

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
    global RASPBERRY_PI_IP, UDP_PORT, THRESHOLD_YAW, THRESHOLD_PITCH, DEADZONE
    import argparse

    parser = argparse.ArgumentParser(
        description='Emotiv EPOC X -> UDP Command Client',
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
        '--deadzone', type=float, default=DEADZONE,
        help=f'Deadzone in degrees (default: {DEADZONE})',
    )
    parser.add_argument(
        '--yaw-threshold', type=float, default=THRESHOLD_YAW,
        help=f'Yaw threshold in degrees (default: {THRESHOLD_YAW})',
    )
    parser.add_argument(
        '--pitch-threshold', type=float, default=THRESHOLD_PITCH,
        help=f'Pitch threshold in degrees (default: {THRESHOLD_PITCH})',
    )
    parser.add_argument(
        '--debug', action='store_true',
        help='Enable Cortex debug logging',
    )
    args = parser.parse_args()

    # Apply CLI overrides to module-level config
    RASPBERRY_PI_IP  = args.ip
    UDP_PORT         = args.port
    DEADZONE         = args.deadzone
    THRESHOLD_YAW    = args.yaw_threshold
    THRESHOLD_PITCH  = args.pitch_threshold

    client = HeadsetClient(APP_CLIENT_ID, APP_CLIENT_SECRET)
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
