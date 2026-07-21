# -*- coding: utf-8 -*-
"""
udp_servo_server.py — Raspberry Pi 5 UDP Servo Controller (Focus-Cycle Mode)

Listens for JSON payloads on UDP port 5005 and drives a 4-DOF robotic arm
through a predefined pick-and-place movement cycle, gated by the user's
mental focus level.

Protocol
--------
    Payload : {"cycle_active": true}   → start / continue the movement cycle
              {"cycle_active": false}  → pause (hold current position)

Movement Cycle  (8 steps, repeating)
------------------------------------
    Step 0: Shoulder → 180  (rotate fully right)
    Step 1: Elbow → 180, Wrist → 180  (reach forward/up)
    Step 2: Gripper → 0  (close claw — grab)
    Step 3: Wrist → 90, Elbow → 90  (retract to center)
    Step 4: Shoulder → 0  (rotate fully left)
    Step 5: Elbow → 180, Wrist → 180  (reach forward/up)
    Step 6: Gripper → 180  (open claw — release)
    Step 7: All → 90, Gripper → 180  (return to home)
    ... then back to Step 0

NON-BLOCKING DESIGN (Critical Requirement)
-------------------------------------------
    The main loop runs at TICK_HZ (50 Hz).  Between cycle steps the server
    waits STEP_DWELL_S seconds, but it does NOT use time.sleep() for this
    dwell.  Instead, it records the timestamp when a step was committed and
    checks elapsed time on every tick.  This means the UDP socket is polled
    every 20 ms regardless of the dwell timer, so a "cycle_active: false"
    command is processed within one tick — no blocking.

    When cycle_active becomes false the state machine freezes: the current
    step index and all servo angles are preserved.  When cycle_active
    becomes true again, execution resumes from exactly where it paused.

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

TICK_HZ  = 50                 # main-loop frequency (Hz)
TICK_S   = 1.0 / TICK_HZ      # ≈ 20 ms

# Time to dwell (wait) at each cycle step before advancing (seconds).
# Gives the physical servo time to reach its target angle.
STEP_DWELL_S = 1.5

# Maksimum servo hızı (derece/saniye). Düşük değer = daha yavaş, daha güvenli hareket.
# 60 derece/saniye → 180° dönüş yaklaşık 3 saniye sürer.
MAX_SPEED_DEG_PER_S = 90.0

# After this many seconds without cycle_active=true the servos detach
IDLE_TIMEOUT_S = 3.0

# After this many seconds without cycle_active=true the servos detach
IDLE_TIMEOUT_S = 3.0

# İstemciden bu süre boyunca mesaj gelmezse güvenlik için hareket durdurulur (saniye)
CLIENT_TIMEOUT_S = 0.5



# =========================================================================
# HARDWARE INIT  (verbatim from demo_robot.py)
# =========================================================================

warnings.simplefilter('ignore')
factory = LGPIOFactory()

# Servos — exact pin & pulse-width config from demo_robot.py (Pygame version)
shoulder = AngularServo(23, min_angle=0, max_angle=180, min_pulse_width=0.5/1000, max_pulse_width=2.5/1000, pin_factory=factory)
elbow    = AngularServo(22, min_angle=0, max_angle=180, min_pulse_width=0.5/1000, max_pulse_width=2.5/1000, pin_factory=factory)
wrist    = AngularServo(17, min_angle=0, max_angle=180, min_pulse_width=0.001, max_pulse_width=0.002, pin_factory=factory)
gripper  = AngularServo(27, min_angle=0, max_angle=180, min_pulse_width=0.0018, max_pulse_width=0.0025, pin_factory=factory)


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
# MOVEMENT CYCLE DEFINITION
# =========================================================================
# Each step is a dict of {servo_name: target_angle}.
# Only the servos listed in a step are moved; the others hold their position.
# Angles are in degrees [0, 180].  Home position: all 90, gripper 180.

CYCLE_STEPS = [
    # Step 0: Shoulder rotates fully to one side
    {'shoulder': 0},

    # Step 1: Elbow up, Wrist forward (reach out)

    {'elbow':0},
    {'wrist': 180},
    

    # Step 2: Gripper close (grab object)
    {'gripper': 180},

    # Step 3: Retract — Wrist center, Elbow center
    {'wrist': 90, 'elbow': 90},

    # Step 4: Shoulder rotates fully to the other side
    {'shoulder': 180},

    # Step 5: Elbow up, Wrist forward (reach out on the other side)
    {'elbow': 0,'wrist': 180},

    # Step 6: Gripper open (release object)
    {'gripper': 0},

    # Step 7: Return to home position
    {'shoulder': 90, 'elbow': 90, 'wrist': 90, 'gripper': 0},
]

NUM_STEPS = len(CYCLE_STEPS)

# =========================================================================
# STATE
# =========================================================================

# Hedef açılar — adım çalıştırıldığında buraya yazılır.
target_angles = {name: 90.0 for name in SERVOS}
target_angles['gripper'] = 180.0              # gripper starts fully open

# Mevcut (interpolasyonlu) açılar — her tick'te hedefe doğru kademeli ilerler.
angles = dict(target_angles)

# Previous angles — the last value written to the physical servo.
prev_angles = dict(angles)

servos_attached = False                        # power-save flag
last_active_time = 0.0                         # timestamp of last movement

# State machine for the cycle
cycle_active       = False                     # are we running the cycle?
current_step_index = 0                         # which step we're on
step_committed     = False                     # has the current step been written?
step_commit_time   = 0.0                       # when the current step was committed


# =========================================================================
# HELPERS
# =========================================================================

def clamp(value: float, lo: float = 0.0, hi: float = 180.0) -> float:
    """Strictly clamp angle to [0, 180] to prevent out-of-bounds PWM."""
    return max(lo, min(hi, value))


def update_leds_cycle(active: bool):
    """Indicate cycle state on LEDs.  Both on = active, both off = paused."""
    if not leds_available:
        return
    if active:
        led_mod_a.on()
        led_mod_b.on()
    else:
        led_mod_a.off()
        led_mod_b.off()


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
        current = angles[name]
        if abs(current - prev_angles[name]) >= 0.5:
            servo.angle = current
            prev_angles[name] = current
            servos_attached = True


def smooth_move_tick(dt: float) -> bool:
    """
    Her tick'te mevcut açıları hedef açılara doğru kademeli olarak hareket ettirir.
    Maksimum hız MAX_SPEED_DEG_PER_S ile sınırlandırılır.

    Returns True if all servos have reached their targets.
    """
    max_step = MAX_SPEED_DEG_PER_S * dt
    all_reached = True

    for name in SERVOS:
        current = angles[name]
        target = target_angles[name]
        diff = target - current

        if abs(diff) < 0.5:
            angles[name] = target
        else:
            # Hedefe doğru en fazla max_step kadar ilerle
            move = min(abs(diff), max_step)
            if diff > 0:
                angles[name] = current + move
            else:
                angles[name] = current - move
            all_reached = False

    commit_changed_servos()
    return all_reached


def initialize_servos():
    """Set all servos to home position on startup."""
    print('Servolar baslatiliyor...')
    home = {'shoulder': 90.0, 'elbow': 90.0, 'wrist': 90.0, 'gripper': 180.0}
    for name, servo in SERVOS.items():
        target = home[name]
        angles[name] = target
        prev_angles[name] = target
        servo.angle = target
        time.sleep(0.3)
        servo.detach()
    print('Baslatma tamamlandi!  (Home: Sh=90 El=90 Wr=90 Gr=180)\n')


def execute_step(step_index: int):
    """
    Apply the target angles defined in CYCLE_STEPS[step_index]
    to the global `target_angles` dict.  Does NOT touch hardware directly —
    smooth_move_tick() will gradually move servos towards these targets.
    """
    step = CYCLE_STEPS[step_index]
    for servo_name, target_angle_val in step.items():
        target_angles[servo_name] = clamp(target_angle_val)


# =========================================================================
# UDP SOCKET HELPERS
# =========================================================================

def drain_socket(sock: socket.socket) -> bool | None:
    """
    Read ALL pending datagrams and return only the latest valid
    cycle_active value.

    Returns True, False, or None (if the buffer was empty / invalid).
    """
    latest = None

    while True:
        ready, _, _ = select.select([sock], [], [], 0)  # non-blocking poll
        if not ready:
            break
        try:
            data, addr = sock.recvfrom(1024)
            payload = json.loads(data.decode('utf-8'))
            ca = payload.get('cycle_active')
            if isinstance(ca, bool):
                latest = ca
        except (json.JSONDecodeError, UnicodeDecodeError, KeyError):
            continue

    return latest


# =========================================================================
# MAIN LOOP  (non-blocking state machine)
# =========================================================================

def main():
    global cycle_active, current_step_index
    global step_committed, step_commit_time
    global servos_attached, last_active_time

    print('=' * 60)
    print('ROBOT KOL UDP SUNUCUSU  (Focus-Cycle Mode)'.center(60))
    print('=' * 60)
    print(f'   Dinlenen port       : {UDP_PORT}')
    print(f'   Dongu hizi          : {TICK_HZ} Hz')
    print(f'   Adim bekleme suresi : {STEP_DWELL_S} s')
    print(f'   Bosta zaman asimi   : {IDLE_TIMEOUT_S} s')
    print(f'   Adim sayisi         : {NUM_STEPS}')
    print('=' * 60)
    print()
    print('Hareket Dongusu:')
    for i, step in enumerate(CYCLE_STEPS):
        parts = [f'{k}→{v}' for k, v in step.items()]
        print(f'   Adim {i}: {", ".join(parts)}')
    print()

    initialize_servos()
    update_leds_cycle(False)

    # --- UDP socket -------------------------------------------------------
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((UDP_HOST, UDP_PORT))
    sock.setblocking(False)
    print(f'UDP dinleniyor  {UDP_HOST}:{UDP_PORT} ...\n')

    prev_cycle_active = False

    last_msg_time = time.time()

    try:
        while True:
            tick_start = time.time()

            # ──────────────────────────────────────────────────────────────
            # 1. DRAIN UDP — get the latest cycle_active command
            # ──────────────────────────────────────────────────────────────
            incoming = drain_socket(sock)

            if incoming is not None:
                cycle_active = incoming
                last_msg_time=time.time()

                if cycle_active and (time.time() - last_msg_time > CLIENT_TIMEOUT_S):
                    print(f'\n>>> GÜVENLİK UYARISI: İstemciden {CLIENT_TIMEOUT_S} saniyedir sinyal yok!')
                    print('>>> İletişim koptuğu için hareket zorunlu olarak durduruldu.\n')
                    cycle_active = False

                # Detect state transitions for logging
                if cycle_active != prev_cycle_active:
                    if cycle_active:
                        print(
                            f'>>> ODAK AKTIF — döngü devam ediyor  '
                            f'(adim {current_step_index}/{NUM_STEPS})'
                        )
                        update_leds_cycle(True)
                    else:
                        print(
                            f'>>> ODAK KAYBI — döngü duraklatildi  '
                            f'(adim {current_step_index}/{NUM_STEPS})  '
                            f'| Sh={angles["shoulder"]:.0f} '
                            f'El={angles["elbow"]:.0f} '
                            f'Wr={angles["wrist"]:.0f} '
                            f'Gr={angles["gripper"]:.0f}'
                        )
                        update_leds_cycle(False)
                    prev_cycle_active = cycle_active

            # ──────────────────────────────────────────────────────────────
            # 2. STATE MACHINE — advance the cycle if active
            # ──────────────────────────────────────────────────────────────
            if cycle_active:
                now = time.time()

                if not step_committed:
                    # --- Hedef açıları ayarla (henüz donanıma yazmaz) ---
                    execute_step(current_step_index)
                    step_committed = True
                    step_commit_time = 0.0        # hedefe ulaşınca ayarlanacak
                    last_active_time = now
                    step_reached_target = False

                    step = CYCLE_STEPS[current_step_index]
                    parts = [f'{k}→{v}' for k, v in step.items()]
                    print(
                        f'  ADIM {current_step_index}/{NUM_STEPS}  '
                        f'{", ".join(parts):30s}  '
                        f'| Hedef: Sh={target_angles["shoulder"]:5.1f} '
                        f'El={target_angles["elbow"]:5.1f} '
                        f'Wr={target_angles["wrist"]:5.1f} '
                        f'Gr={target_angles["gripper"]:5.1f}'
                    )

                # --- Her tick'te kademeli hareket (smooth ramp) ---
                all_reached = smooth_move_tick(TICK_S)
                last_active_time = now

                # Hedefe ilk ulaşıldığında dwell zamanlayıcısını başlat
                if all_reached and step_commit_time == 0.0:
                    step_commit_time = now

                # Dwell süresi dolduysa bir sonraki adıma geç
                if all_reached and step_commit_time > 0.0:
                    elapsed_since_commit = now - step_commit_time
                    if elapsed_since_commit >= STEP_DWELL_S:
                        current_step_index = (current_step_index + 1) % NUM_STEPS
                        step_committed = False

            # ──────────────────────────────────────────────────────────────
            # 3. IDLE POWER-SAVE — detach servos if idle for too long
            # ──────────────────────────────────────────────────────────────
            if servos_attached and not cycle_active:
                if time.time() - last_active_time > IDLE_TIMEOUT_S:
                    detach_all()
                    print('Servolar bosta — detach edildi')

            # ──────────────────────────────────────────────────────────────
            # 4. TICK PACING — sleep only the remainder of the tick
            #    This is the ONLY sleep in the loop, and it's just the
            #    tick-pacing sleep (~20 ms max), NOT a step-dwell sleep.
            # ──────────────────────────────────────────────────────────────
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
