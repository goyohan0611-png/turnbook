#!/bin/sh
# Turnbook, installed in one step on macOS and Linux: no Python needed beforehand.
#
#   curl -LsSf https://raw.githubusercontent.com/goyohan0611-png/turnbook/main/scripts/install.sh | sh
#
# It installs uv (Astral's installer, to your home folder, no sudo), which brings its own
# Python 3.12; installs Turnbook with it; then connects your AI apps (turnbook-install).
# Arguments go to turnbook-install (... | sh -s -- --name "Your Name"). TURNBOOK_VERSION pins one.
set -eu

if ! command -v uv >/dev/null 2>&1; then
    echo "Turnbook: installing uv (it brings Python)..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    PATH="$HOME/.local/bin:$PATH"
    export PATH
fi

PIN="${TURNBOOK_VERSION:-${GRAPH_MIND_VERSION:-}}"     # GRAPH_MIND_VERSION: the old name
SPEC="turnbook${PIN:+==$PIN}"
# the name before 0.9.44: its commands would clash with these; the memory itself is kept
uv tool list 2>/dev/null | grep -q "^graph-mind-memory " && uv tool uninstall graph-mind-memory >/dev/null
echo "Turnbook: installing $SPEC..."
uv tool install --python 3.12 --upgrade "$SPEC"
# piped into sh, stdin is this script: ask the terminal instead, so the installer can ask first
if [ -t 1 ] && { : </dev/tty; } 2>/dev/null; then
    "$(uv tool dir --bin)/turnbook-install" "$@" </dev/tty
else
    "$(uv tool dir --bin)/turnbook-install" "$@"
fi
