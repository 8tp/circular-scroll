#!/usr/bin/env python3
"""Circular-scroll proxy daemon for Panasonic Let's Note (Synaptics TM3562-003).

Permanently grabs the real touchpad and exposes a virtual touchpad clone to
libinput. Forwards all events 1:1 EXCEPT during a single-finger stroke that
starts on the outer ring -- that stroke is hidden from libinput end to end
(no events forwarded), and its rotation is converted into REL_WHEEL events
on a separate virtual scroll-wheel device. Multi-finger gestures and
strokes that start in the inner area always pass through unchanged.

Because libinput never sees the hidden stroke, there is no grab/ungrab
race against the lift event -- the device's state stays consistent from
libinput's perspective throughout, so cursor response is immediate the
moment the next touch begins.
"""

import math
import sys
import evdev
from evdev import UInput, ecodes as e

TOUCHPAD_NAME = "Synaptics TM3562-003"
RING_FRACTION = 0.70     # finger must be at >=70% of pad radius to engage
DEGREES_PER_TICK = 12.0  # smaller = more sensitive
INVERT = False           # flip scroll direction


def find_touchpad():
    for path in evdev.list_devices():
        dev = evdev.InputDevice(path)
        if dev.name == TOUCHPAD_NAME:
            return dev
        dev.close()
    sys.exit(f"touchpad '{TOUCHPAD_NAME}' not found")


def axis_range(dev, code):
    info = dict(dev.capabilities()[e.EV_ABS])[code]
    return info.min, info.max


def main():
    real = find_touchpad()
    x_min, x_max = axis_range(real, e.ABS_MT_POSITION_X)
    y_min, y_max = axis_range(real, e.ABS_MT_POSITION_Y)
    cx, cy = (x_min + x_max) / 2, (y_min + y_max) / 2
    pad_radius = min((x_max - x_min) / 2, (y_max - y_min) / 2)
    ring_threshold_sq = (RING_FRACTION * pad_radius) ** 2

    # Clone capabilities for the virtual touchpad. UInput supplies EV_SYN
    # automatically; passing it explicitly raises an error.
    caps = real.capabilities()
    caps.pop(e.EV_SYN, None)
    pad = UInput(
        caps,
        name="Circular-Scroll Virtual Touchpad",
        vendor=real.info.vendor,
        product=real.info.product,
        version=real.info.version,
        bustype=real.info.bustype,
        input_props=real.input_props(),
    )
    wheel = UInput({e.EV_REL: [e.REL_WHEEL]}, name="Circular-Scroll Wheel")

    sign = -1 if INVERT else 1
    print(f"real:    {real.name} {real.path}", file=sys.stderr)
    print(f"pad:     {pad.device.path}", file=sys.stderr)
    print(f"X[{x_min}..{x_max}] Y[{y_min}..{y_max}] center=({cx:.0f},{cy:.0f}) "
          f"engage at r>={RING_FRACTION:.0%}", file=sys.stderr)

    STATE_IDLE, STATE_CURSOR, STATE_SCROLL = 0, 1, 2

    cur_slot = 0
    slot_tid = {}   # slot -> kernel tracking id (present iff slot is active)
    slot_pos = {}   # slot -> (x, y)
    active_count = 0

    state = STATE_IDLE
    hiding_slot = None
    last_angle = None
    accum = 0.0

    buf = []

    def forward(events):
        for evt in events:
            pad.write(evt.type, evt.code, evt.value)
        pad.syn()

    def emit_wheel():
        nonlocal accum
        while accum >= DEGREES_PER_TICK:
            wheel.write(e.EV_REL, e.REL_WHEEL, -1 * sign)
            wheel.syn()
            accum -= DEGREES_PER_TICK
        while accum <= -DEGREES_PER_TICK:
            wheel.write(e.EV_REL, e.REL_WHEEL, 1 * sign)
            wheel.syn()
            accum += DEGREES_PER_TICK

    real.grab()
    try:
        for ev in real.read_loop():
            if ev.type == e.EV_SYN and ev.code == e.SYN_REPORT:
                # ---- Decide what to do with this frame ----
                if state == STATE_IDLE:
                    if active_count == 0:
                        forward(buf)  # likely empty, but pass through anyway
                    elif active_count == 1:
                        slot = next(iter(slot_tid))
                        x, y = slot_pos.get(slot, (cx, cy))
                        dx, dy = x - cx, y - cy
                        if dx * dx + dy * dy >= ring_threshold_sq:
                            state = STATE_SCROLL
                            hiding_slot = slot
                            last_angle = math.degrees(math.atan2(dy, dx))
                            accum = 0.0
                            # discard buf -- libinput never sees this touch
                        else:
                            state = STATE_CURSOR
                            forward(buf)
                    else:
                        state = STATE_CURSOR
                        forward(buf)
                elif state == STATE_CURSOR:
                    forward(buf)
                    if active_count == 0:
                        state = STATE_IDLE
                elif state == STATE_SCROLL:
                    if active_count == 0:
                        # hidden finger lifted; the lift events stay hidden too
                        state = STATE_IDLE
                        hiding_slot = None
                        last_angle = None
                        accum = 0.0
                    elif active_count >= 2:
                        # second finger joined -- reveal the previously hidden
                        # slot so libinput has consistent state, then forward
                        # the current frame (which contains the new finger)
                        if hiding_slot in slot_tid:
                            tid = slot_tid[hiding_slot]
                            x, y = slot_pos.get(hiding_slot, (cx, cy))
                            pad.write(e.EV_ABS, e.ABS_MT_SLOT, hiding_slot)
                            pad.write(e.EV_ABS, e.ABS_MT_TRACKING_ID, tid)
                            pad.write(e.EV_ABS, e.ABS_MT_POSITION_X, int(x))
                            pad.write(e.EV_ABS, e.ABS_MT_POSITION_Y, int(y))
                        state = STATE_CURSOR
                        hiding_slot = None
                        last_angle = None
                        accum = 0.0
                        forward(buf)
                    else:
                        # still single finger, still hiding -- process rotation
                        if hiding_slot in slot_pos:
                            x, y = slot_pos[hiding_slot]
                            dx, dy = x - cx, y - cy
                            if dx * dx + dy * dy >= ring_threshold_sq:
                                angle = math.degrees(math.atan2(dy, dx))
                                if last_angle is not None:
                                    delta = angle - last_angle
                                    while delta > 180:
                                        delta -= 360
                                    while delta <= -180:
                                        delta += 360
                                    accum += delta
                                last_angle = angle
                                emit_wheel()
                            else:
                                last_angle = None  # off the rim; pause
                buf = []
                continue

            buf.append(ev)
            if ev.type == e.EV_ABS:
                if ev.code == e.ABS_MT_SLOT:
                    cur_slot = ev.value
                elif ev.code == e.ABS_MT_TRACKING_ID:
                    if ev.value == -1:
                        if cur_slot in slot_tid:
                            del slot_tid[cur_slot]
                            slot_pos.pop(cur_slot, None)
                            active_count = max(0, active_count - 1)
                    else:
                        if cur_slot not in slot_tid:
                            active_count += 1
                        slot_tid[cur_slot] = ev.value
                elif ev.code == e.ABS_MT_POSITION_X:
                    x, y = slot_pos.get(cur_slot, (ev.value, cy))
                    slot_pos[cur_slot] = (ev.value, y)
                elif ev.code == e.ABS_MT_POSITION_Y:
                    x, y = slot_pos.get(cur_slot, (cx, ev.value))
                    slot_pos[cur_slot] = (x, ev.value)
    finally:
        try:
            real.ungrab()
        except OSError:
            pass
        pad.close()
        wheel.close()


if __name__ == "__main__":
    main()
