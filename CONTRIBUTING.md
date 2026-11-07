# Contributing to PdaNet Linux

Thanks for taking the time to improve PdaNet Linux. This is an unofficial,
community-maintained client. It speaks the PdaNet protocol to a phone running
the original, licensed PdaNet+ app; it does not bypass registration, licensing,
or carrier restrictions.

By contributing you agree that your changes are licensed under the same terms
as the project: **GPL-3.0-or-later**.

## Before you start

- Open an issue before starting large changes, especially in the protocol,
  routing, root helper, or Bluetooth code.
- Keep pull requests focused. One logical change per PR is easier to review
  and to test on real hardware.
- Never commit PdaNet installers, APKs, kernel extensions, or other
  proprietary binaries. Do not commit packet captures, logs, or screenshots
  that contain phone numbers, IMEI/IMSI, account names, or public IPs.
- Do not add code that bypasses PdaNet licensing or trial limitations.
- Treat privilege separation as a security boundary: the GUI and CLI run as
  the desktop user, and only the small root helper may touch TUN, routes, DNS
  and `/etc/hosts`.

## Development setup

Requirements are listed in the [README](README.md#requirements). In short:
Linux with TUN support, Python 3.10+, `adb`, `iproute2`, and PyGObject with
GTK 4 and libadwaita for the GUI.

```bash
git clone https://github.com/uznbt/pdanet-linux.git
cd pdanet-linux

python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e .

# Optional: install the root helper and desktop integration
# Add --passwordless to install the helper-only NOPASSWD sudoers rule.
sudo ./tools/install.sh
```

Run the app without installing the helper when working on UI code:

```bash
pdanet-linux gui
pdanet-linux doctor
```

The protocol simulator lets you exercise the session and pump without a
phone:

```bash
python3 -m pdanet_linux.mock_server --port 18740
```

## Tests

The suite uses the standard library's `unittest`; no test runner is required.

```bash
python3 -m unittest discover -s tests -v
```

Guidelines:

- Add or update tests for protocol parsing, routing, configuration, and
  connection state changes.
- Unit tests must not require a phone, root, a real network, or a display.
- Live checks are welcome in a PR description, not as automated tests.
- For routing or firewall changes, include the relevant output before and
  after connecting:

```bash
ip rule show
ip -6 rule show
ip route show table 51820
ip -6 route show table 51820
ip route get 1.1.1.1
```

## Code style

- Target Python 3.10+ and the standard library; avoid new runtime
  dependencies unless they are discussed in an issue first.
- Follow the existing style: four-space indentation, roughly 88 columns, and
  type hints where they clarify an interface.
- Never use `shell=True`. Pass argument lists to `subprocess`.
- Validate everything that crosses the helper socket; the helper must not
  trust the client's JSON blindly.
- Keep user-facing strings in English. Replies in Indonesian in issues are
  fine.
- GUI changes must remain usable in narrow and tiled windows. Check a width
  of roughly 360 px as well as a full desktop window.
- Preserve the existing behaviour where the tunnel never blocks on optional
  features such as cellular signal polling.

## Commits and pull requests

Use a short prefix that matches the area you touched:

```text
usb: retry adb forward after reconnect
bt: log SDP channel probe failures
wifi: handle nmcli timeout during scan
netconfig: keep ULA routes on main
gui: make dashboard usable at 360px
tests: cover IPv6 guard cleanup
docs: document table 51820 conflict
```

A good PR description answers:

1. **What** changed and **why**.
2. **How** it was tested (unit tests, mock server, or a real phone).
3. Which transport was used (USB, Wi-Fi, Bluetooth) and on which
   distribution, desktop, and window manager.
4. Any user-visible behaviour that needs a README update.

Please update the README when you change commands, requirements, routing
behaviour, or limitations.

## Reporting bugs

Include as much of this as you can:

- Distribution, kernel version, desktop environment and window manager.
- Python version, GTK/libadwaita version, and `pdanet-linux --version`.
- Transport used and whether the phone-side PdaNet app was licensed and
  running.
- Relevant log lines. Enable verbose logging if the CLI supports it, or copy
  the activity log from the GUI.
- `adb devices -l`, `bluetoothctl info <MAC>`, `ip rule show`, and
  `ip route show table 51820` where relevant.

Redact phone numbers, IMEI/IMSI, Wi-Fi passwords, and public IP addresses.

## Security issues

Do not open a public issue for privilege escalation, helper authentication
bypasses, command injection, or DNS/hosts tampering. Contact the maintainer
privately through the address on their GitHub profile and allow time for a
fix before disclosure.

## Hardware verification

Live testing is the most valuable contribution for this project. Reports for
these areas are especially welcome:

- USB reconnect after unplug/replug and after the phone reboots.
- Wi-Fi mode with the phone as hotspot, including DNS and MTU behaviour.
- Bluetooth RFCOMM pairing on BlueZ versions that differ from the tested one.
- IPv6 leak behaviour on networks that advertise a global prefix.
- Tray behaviour under KDE Plasma, GNOME with AppIndicator, waybar and
  QuickShell.
