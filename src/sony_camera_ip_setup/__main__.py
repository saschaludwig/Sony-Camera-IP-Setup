# Copyright (c) 2026 Sascha Ludwig, astrastudio broadcast solutions
# SPDX-License-Identifier: MIT

"""Allow `python -m sony_camera_ip_setup`.

Delegates to :func:`sony_camera_ip_setup.main` so an editable install
and a direct module invocation share the same CLI.
"""

from sony_camera_ip_setup import main

raise SystemExit(main())
