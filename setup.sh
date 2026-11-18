#!/bin/bash
# Setup-Skript für die Claude-Code-Cloud-Umgebung
pip install --quiet pillow numpy imageio-ffmpeg || pip install --quiet --break-system-packages pillow numpy imageio-ffmpeg
