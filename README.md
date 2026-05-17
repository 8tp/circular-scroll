# circular-scroll

ThinkPad/classic-Synaptics-style **circular scrolling** for modern Linux laptops, implemented as a userspace proxy daemon over `evdev` + `uinput`.

Trace your finger around the outer rim of the touchpad to scroll. A clockwise stroke scrolls down, counter-clockwise scrolls up (configurable). Strokes that start in the inner area, and any multi-finger gestures, pass through untouched.

Developed and tested on a **Panasonic Let's Note CF-SV9** (Synaptics TM3562-003 touchpad) under Fedora + GNOME / libinput. Should work on any Linux laptop with a Synaptics multitouch pad after one config change (`TOUCHPAD_NAME`).

## Why

libinput dropped support for circular scrolling years ago, and most modern touchpad drivers don't expose it at all. Edge-scrolling exists but is straight-line; circular scrolling is faster for long pages because you don't have to re-engage at the edge. This daemon brings the feature back without patching the kernel or libinput.

## How it works (briefly)

The daemon permanently `EVIOCGRAB`s your real touchpad and exposes a **virtual touchpad clone** to libinput (cloning capabilities, vendor/product IDs, input_props). It also exposes a separate **virtual scroll-wheel device**.

For each touch stroke, it decides per `SYN_REPORT`:

- **Single finger lands in the inner ~70%** → cursor stroke. Forward all events 1:1 to the virtual pad.
- **Single finger lands on the outer rim** → scroll stroke. *Hide the entire stroke from libinput* (no events forwarded) and convert finger rotation to `REL_WHEEL` ticks on the wheel device.
- **A second finger appears mid-scroll** → synthesize a "reveal" sequence (slot, tracking_id, position) so libinput has consistent slot state, then resume forwarding normally.
- **Multi-finger from the start** → pass through.

Because libinput never sees a hidden scroll stroke, there is no grab/ungrab race against the lift event — the device's apparent state stays consistent throughout. Earlier grab-only-during-scroll implementations of the same idea suffer from a 1–2 second post-scroll input delay; the proxy approach eliminates it.

## Requirements

- Linux with the `uinput` kernel module (`modprobe uinput`)
- Python 3.9+
- [`python-evdev`](https://github.com/gvalkov/python-evdev) (`pip install evdev`, or `dnf install python3-evdev` on Fedora)
- Your user in the `input` group (most distros put you there by default; check with `groups`)
- libinput-based desktop session (X11 or Wayland — both work, the virtual device looks like any other touchpad)

## Install

```sh
# 1. Clone into your home dir (the systemd unit expects ~/circular-scroll/)
git clone <repo-url> ~/circular-scroll
cd ~/circular-scroll

# 2. Find your touchpad's exact name and update circular-scroll.py
sudo libinput list-devices | grep -i touchpad
# Then edit TOUCHPAD_NAME at the top of circular-scroll.py to match

# 3. Install the udev rule for /dev/uinput access
sudo cp 99-uinput.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger
# Log out and back in (or reboot) for group permissions to apply.

# 4. Install and enable the user systemd unit
mkdir -p ~/.config/systemd/user
cp circular-scroll.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now circular-scroll.service

# 5. Verify
systemctl --user status circular-scroll.service
# In GNOME Settings → Mouse & Touchpad you'll see a second "touchpad"
# called "Circular-Scroll Virtual Touchpad". That's the one libinput uses.
```

To test: open a long web page and slowly trace a circle near the touchpad edge. The page should scroll.

## Configuration

All knobs live at the top of `circular-scroll.py`:

| Constant | Default | Effect |
|---|---|---|
| `TOUCHPAD_NAME` | `"Synaptics TM3562-003"` | Exact device name from `libinput list-devices` |
| `RING_FRACTION` | `0.70` | How far out (fraction of pad radius) a touch must start to engage scrolling. Lower = bigger scroll ring, smaller cursor area |
| `DEGREES_PER_TICK` | `12.0` | Rotation per scroll tick. Smaller = more sensitive |
| `INVERT` | `False` | Flip scroll direction |

Restart after editing: `systemctl --user restart circular-scroll.service`.

Touchpad cursor speed is unchanged — it's still controlled by your desktop's normal touchpad settings (e.g. `gsettings set org.gnome.desktop.peripherals.touchpad speed 0.3`) and applies to the virtual pad like any other touchpad.

## Troubleshooting

**Kill switch.** `systemctl --user stop circular-scroll.service` instantly releases the grab — your real touchpad goes back to behaving normally with no daemon in the path.

**Touchpad feels frozen / cursor stuck.** A crash while holding `EVIOCGRAB` can leave libinput temporarily confused. Recovery, in order of severity:

1. `systemctl --user restart circular-scroll.service`
2. Toggle touchpad off and on: `gsettings set org.gnome.desktop.peripherals.touchpad send-events 'disabled'` then `'enabled'`
3. Re-trigger the touchpad in udev: `sudo udevadm trigger /dev/input/event*`

The unit ships with `StartLimitBurst=3 StartLimitIntervalSec=60` so a recurring crash can't pummel libinput in a tight loop.

**Two touchpads in Settings.** Cosmetic only. The real one is permanently grabbed so it can't feed events to anything but this daemon; only the virtual pad reaches libinput. Settings changes (tap-to-click, natural scrolling, speed) should be applied to the virtual one.

**Permission denied opening `/dev/uinput`.** The udev rule didn't apply, or your user isn't in `input`. Check `ls -l /dev/uinput` (should be `crw-rw---- root input`) and `groups`.

**Touchpad name not found at startup.** Check the journal: `journalctl --user -u circular-scroll.service -n 50`. Update `TOUCHPAD_NAME` to match.

## License

MIT — see `LICENSE`.
