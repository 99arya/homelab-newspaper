#!/bin/sh
# One-step setup: checks requirements, then asks a few questions (Enter accepts the default).
cd "$(dirname "$0")"
command -v docker >/dev/null || { echo "Docker is required: https://docs.docker.com/engine/install/"; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required (sudo apt install python3)"; exit 1; }
docker info >/dev/null 2>&1 || { echo "Can't talk to Docker. Add yourself to the docker group: sudo usermod -aG docker \$USER (then log out and in)"; exit 1; }
exec python3 configure.py "$@"
