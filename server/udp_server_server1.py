# -*- coding: utf-8 -*-
"""
udp_servo_server.py — Raspberry Pi 5 UDP Servo Controller

Listens for JSON payloads on UDP port 5005 and translates them into
incremental servo movements on a 4-DOF robotic arm.

Protocol
--------
    Payload : {"command": "<sag|sol|yukari|asagi|dur>", "mode": <1|2>}

Movement Mapping
----------------
    Mode 1 (Arm):
        sag    →  Shoulder + STEP  (base rotation right)
        sol    →  Shoulder – STEP  (base rotation left)
        yukari →  Elbow   + STEP   (arm up)
        asagi  →  Elbow   – STEP   (arm down)

    Mode 2 (Gripper + Wrist):
        sag    →  Gripper + STEP   (open claw)
        sol    →  Gripper – STEP   (close claw)
        yukari →  Wrist   + STEP   (arm forward)
        asagi  →  Wrist   – STEP   (arm backward)

Hardware
--------
    Servo pins and pulse-width configs are copied verbatim from demo_robot.py.
    Requires: gpiozero, lgpio   (pip install gpiozero lgpio)
"""

import json
import select
import socket
import time
import warnings
from collections import OrderedDict

from gpiozero import AngularServo, LED
from gpiozero.pins.lgpio import LGPIOFactory

# =========================================================================
# CONFIGURATION
# =========================================================================

UDP_HOST = '0.0.0.0'          # listen on all interfaces
UDP_PORT = 5005

STEP     = 2                  # degrees per tick (matches demo_robot.py)
TICK_HZ  = 50                 # main-loop frequency (Hz)
TICK_S   = 1.0 / TICK_HZ      # ≈ 20 ms

# After this many seconds of silence the servos detach (power-save)
IDLE_TIMEOUT_S = 1.0

# =========================================================================
# HARDWARE INIT  (verbatim from demo_robot.py)
# =========================================================================

warnings.simplefilter('ignore')
factory = LGPIOFactory()

# Servos — exact pin & pulse-width config from demo_robot.py (Pygame version)
shoulder = AngularServo(
    23,
    min_angle=0, max_angle=180,
    min_pulse_width=1 / 1000, max_pulse_width=2 / 1000,
    pin_factory=factory,
)
elbow = AngularServo(
    22,
    min_angle=0, max_angle=180,
    min_pulse_width=0.5 / 1000, max_pulse_width=2.5 / 1000,
    pin_factory=factory,
)
wrist = AngularServo(
    17,
    min_angle=0, max_angle=180,
    min_pulse_width=0.0005, max_pulse_width=0.002,
    pin_factory=factory,
)
gripper = AngularServo(
    27,
    min_angle=0, max_angle=180,
    min_pulse_width=0.0018, max_pulse_width=0.0025,
    pin_factory=factory,
)

SERVOS = OrderedDict([
    ('shoulder', shoulder),
    ('elbow',    elbow),
    ('wrist',    wrist),
    ('gripper',  gripper),
])

# LEDs
try:
    led_mod_a = LED(5, pin_factory=factory)
    led_mod_b = LED(6, pin_factory=factory)
    leds_available = True
except Exception as exc:
    print(f'LED HATASI: {exc}')
    leds_available = False

# =========================================================================
# STATE
# =========================================================================

# Angle state tracker — stores the committed angle for each servo.
# Hardware is only updated when a value here actually changes.
angles = {name: 90.0 for name in SERVOS}

# Previous angles — the last value written to the physical servo.
# Compared against `angles` to decide whether a PWM write is needed.
prev_angles = {name: 90.0 for name in SERVOS}

servos_attached = False                      # power-save flag
last_active_time = 0.0                       # timestamp of last real movement


# =========================================================================
# HELPERS
# =========================================================================

def clamp(value: float, lo: float = 0.0, hi: float = 180.0) -> float:
    """Strictly clamp angle to [0, 180] to prevent out-of-bounds PWM."""
    return max(lo, min(hi, value))


def update_leds(mode: int):
    """Mirror the LED logic from demo_robot.py."""
    if not leds_available:
        return
    if mode == 1:
        led_mod_a.on()
        led_mod_b.off()
    else:
        led_mod_a.off()
        led_mod_b.on()


def detach_all():
    """Release PWM on every servo (power-save / prevent jitter)."""
    global servos_attached
    for servo in SERVOS.values():
        servo.detach()
    servos_attached = False


def commit_changed_servos():
    """
    Write new angles ONLY to servos whose target angle has actually
    changed since the last commit.  This is the core anti-jitter measure:
    servos that are not moving receive no PWM updates and stay silent.
    """
    global servos_attached

    for name, servo in SERVOS.items():
        target = angles[name]
        if abs(target - prev_angles[name]) >= 0.5:
            servo.angle = target
            prev_angles[name] = target
            servos_attached = True           # at least one servo is active


def initialize_servos():
    """Centre all servos at 90° on startup (same as demo_robot.py)."""
    print('Servolar baslatiliyor...')
    for name, servo in SERVOS.items():
        angles[name] = 90.0
        prev_angles[name] = 90.0
        servo.angle = 90
        time.sleep(0.3)
        servo.detach()
    print('Baslatma tamamlandi!\n')


# Command → (servo_name, delta) lookup tables.
# Separated by mode for absolute clarity.
_MODE1_MAP = {
    'sag':    ('shoulder', +STEP),   # base rotation right
    'sol':    ('shoulder', -STEP),   # base rotation left
    'yukari': ('elbow',   +STEP),   # arm up
    'asagi':  ('elbow',   -STEP),   # arm down
}

_MODE2_MAP = {
    'sag':    ('gripper', +STEP),   # open claw
    'sol':    ('gripper', -STEP),   # close claw
    'yukari': ('wrist',  +STEP),    # arm forward
    'asagi':  ('wrist',  -STEP),    # arm backward
}


def apply_command(command: str, mode: int) -> bool:
    """
    Translate a single (command, mode) pair into an incremental angle
    delta on exactly ONE servo.  Returns True if any angle was modified.

    Mode 1 — Arm:
        sag    →  shoulder + STEP   (base rotation right)
        sol    →  shoulder – STEP   (base rotation left)
        yukari →  elbow   + STEP    (arm up)
        asagi  →  elbow   – STEP    (arm down)

    Mode 2 — Gripper + Wrist:
        sag    →  gripper + STEP    (open claw)
        sol    →  gripper – STEP    (close claw)
        yukari →  wrist   + STEP    (arm forward)
        asagi  →  wrist   – STEP    (arm backward)
    """
    if command == 'dur':
        return False

    # Select the lookup table for the active mode
    mapping = _MODE1_MAP if mode == 1 else _MODE2_MAP
    entry = mapping.get(command)

    if entry is None:
        return False

    servo_name, delta = entry
    old_angle = angles[servo_name]

    # Compute and strictly clamp to [0, 180]
    new_angle = max(0, min(180, old_angle + delta))

    # Only touch hardware if the angle actually changed
    if abs(new_angle - old_angle) < 0.01:
        return False                         # already at limit

    angles[servo_name] = new_angle
    return True


def drain_socket(sock: socket.socket):
    """
    Read ALL pending datagrams and return only the latest valid
    (command, mode) pair.  This prevents buffer bloat when the PC streams
    faster than the servo loop can consume.

    Returns (command, mode) or (None, None) if the buffer was empty or
    contained no valid payload.
    """
    latest_command = None
    latest_mode = None

    while True:
        ready, _, _ = select.select([sock], [], [], 0)  # non-blocking poll
        if not ready:
            break
        try:
            data, addr = sock.recvfrom(1024)
            payload = json.loads(data.decode('utf-8'))
            cmd  = payload.get('command', 'dur')
            mode = payload.get('mode', 1)
            latest_command = cmd
            latest_mode = mode
        except (json.JSONDecodeError, UnicodeDecodeError, KeyError):
            continue                        # skip malformed packets

    return latest_command, latest_mode


# =========================================================================
# MAIN LOOP
# =========================================================================

def main():
    global servos_attached, last_active_time

    print('=' * 60)
    print('ROBOT KOL UDP SUNUCUSU'.center(60))
    print('=' * 60)
    print(f'   Dinlenen port      : {UDP_PORT}')
    print(f'   Adim buyuklugu     : {STEP} derece')
    print(f'   Dongu hizi         : {TICK_HZ} Hz')
    print(f'   Bosta zaman asimi  : {IDLE_TIMEOUT_S} s')
    print('=' * 60)

    initialize_servos()
    update_leds(mode=1)

    # --- UDP socket -------------------------------------------------------
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((UDP_HOST, UDP_PORT))
    sock.setblocking(False)
    print(f'UDP dinleniyor  {UDP_HOST}:{UDP_PORT} ...\n')

    current_mode = 1

    try:
        while True:
            tick_start = time.time()

            # 1. Drain all pending packets — keep only the freshest one
            command, mode = drain_socket(sock)

            if command is not None and mode is not None:
                # Mode switch detection
                if mode != current_mode:
                    current_mode = mode
                    update_leds(current_mode)
                    print(f'Mod degisti -> MOD {current_mode}')

                # Compute new angle (does NOT touch hardware yet)
                moved = apply_command(command, current_mode)

                if moved:
                    last_active_time = time.time()

                    # Write ONLY the servo(s) whose angle actually changed
                    commit_changed_servos()

                    print(
                        f'cmd={command:6s}  mode={current_mode}  '
                        f'| Sh={angles["shoulder"]:5.1f} '
                        f'El={angles["elbow"]:5.1f} '
                        f'Wr={angles["wrist"]:5.1f} '
                        f'Gr={angles["gripper"]:5.1f}'
                    )

            # 2. Idle power-save — detach servos if no real command for a while
            if servos_attached and (time.time() - last_active_time > IDLE_TIMEOUT_S):
                detach_all()
                print('Servolar bosta - detach edildi')

            # 3. Sleep the remainder of the tick
            elapsed = time.time() - tick_start
            remaining = TICK_S - elapsed
            if remaining > 0:
                time.sleep(remaining)

    except KeyboardInterrupt:
        print('\n\nSunucu durduruldu.')
    finally:
        detach_all()
        if leds_available:
            led_mod_a.off()
            led_mod_b.off()
        sock.close()
        print('Servolar ve LEDler kapatildi. Program sonlandirildi.')


if __name__ == '__main__':
    main()
