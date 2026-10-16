#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# setup.sh - thin wrapper (works on Linux, macOS, Termux). For Windows use setup.bat.
# Installs nothing by itself except auto-downloaded jars in ./tools (no sudo).
# Pass -y to auto-accept downloads:  ./setup.sh -y
exec python3 "$(dirname "$0")/remove_ads.py" --setup "$@"
