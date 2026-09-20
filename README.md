# Sony Camera IP Setup

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Unofficial RM-IP Setup alternative for macOS and Linux.

Discover Sony SRG/BRC PTZ cameras on the LAN and set their IP address, subnet mask, gateway, and name. This is a Python implementation of Sony's **Camera IP Setting Command** — the same UDP protocol used by the Windows-only [RM-IP Setup Tool](https://www.sony.com/electronics/support/software/00243728). It is not affiliated with Sony.

The command-line tool is installed as `sony-camera-ip-setup`.

## Why

Sony's official tool is Windows-only. The cameras themselves speak a UDP broadcast protocol on port **52380**, so a small Python CLI is enough.

## Install

Python 3.9 or newer is required. No third-party packages are needed.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

After that the `sony-camera-ip-setup` command is available.

You can also run the module without installing:

```bash
PYTHONPATH=src python3 -m sony_camera_ip_setup discover
```

## Usage

List cameras:

```bash
sony-camera-ip-setup discover
```

Change IP address, mask, gateway, and/or name:

```bash
sony-camera-ip-setup set --mac AA-BB-CC-DD-EE-FF --ip 192.168.42.52 --mask 255.255.255.0 --gateway 192.168.42.1 --name CAM1
```

If you only want to rename a camera, pass its current IP as `--ip` and add `--name`:

```bash
sony-camera-ip-setup set --mac AA-BB-CC-DD-EE-FF --ip 192.168.0.100 --name STUDIO1
```

Show the packet without sending it:

```bash
sony-camera-ip-setup set --mac AA-BB-CC-DD-EE-FF --ip 192.168.42.52 --name CAM1 --dry-run
```

Print raw TX/RX hex dumps with `-v`. Bind to a specific local address with `--bind 192.168.0.10`.

## Camera name rules

Sony limits the name to **8 characters**: letters, digits, and spaces.

## Important notes

- The camera must be in **LAN mode** (VISCA/LAN switch ON).
- Network settings can only be changed while the inquiry reply shows **WRITE:on**.
- WRITE turns off automatically about **20 minutes** after power-on. Power-cycle the camera and retry.
- Factory defaults are typically `192.168.0.100 / 255.255.255.0 / CAM1`.
- Discovery and IP changes use UDP broadcast to `255.255.255.255:52380`. The Mac or PC must share the same Ethernet segment (same switch or VLAN) as the camera. The IP subnet does not have to match: a host on `192.168.42.0/24` can still find a camera at the factory address `192.168.0.100`.
- A temporary address alias is only needed if broadcasts are filtered or you want unicast access (for example VISCA on port 52381) before the camera is moved onto your subnet:

```bash
sudo ifconfig en0 alias 192.168.0.10 netmask 255.255.255.0
```

## Protocol

Sony frames the setup messages as:

```
STX (0x02) + ASCII field + 0xFF + ... + ETX (0x03)
```

Inquiry:

```
ENQ:network
```

Inquiry reply includes `MAC`, `MODEL`, `SOFTVERSION`, `IPADR`, `MASK`, `GATEWAY`, `NAME`, and `WRITE`.

A change is accepted only when the `MAC` in the setting packet matches the camera. Success is `ACK:<mac>`, failure is `NAK:<mac>`.

Documented in Sony command lists such as the [SRG-300H technical manual](https://www.sony.com/electronics/support/res/manuals/AES6/fe573c4d3e5d01ec8d5172b500b32ac1/AES61001M.pdf) (IP Related Setting Command) and later SRG command lists (Camera IP Setting Command).

VISCA camera control (pan/tilt/zoom) uses a different port, **52381**, and is out of scope here.

## Tests

```bash
source .venv/bin/activate
PYTHONPATH=src python3 -m unittest discover -s tests
```

## Publishing to PyPI

Releases are published by GitHub Actions when you create a GitHub Release.
No PyPI token is stored in the repository. Authentication uses
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/).

One-time setup on [pypi.org](https://pypi.org/manage/account/publishing/):

1. Add a pending trusted publisher (the project does not have to exist yet).
2. PyPI project name: `sony-camera-ip-setup`
3. Owner: `saschaludwig`
4. Repository: `Sony-Camera-IP-Setup`
5. Workflow: `publish.yml`
6. Environment: `pypi`

In the GitHub repo, create an environment named `pypi` (Settings → Environments).
Optional but recommended: restrict it to the `main` branch.

To publish a new version:

1. Bump `version` in `pyproject.toml` and `__version__` in
   `src/sony_camera_ip_setup/__init__.py`.
2. Commit and push to `main`.
3. Create a GitHub Release (for example tag `v1.0.1`).
4. The **Publish** workflow runs tests, builds the package, and uploads it to PyPI.

## License

[MIT](LICENSE)
