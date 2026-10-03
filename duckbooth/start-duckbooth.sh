#!/bin/sh

# Labwc exposes its Wayland socket before its first frame is ready. Waiting here
# prevents Qt from creating an unpainted fullscreen surface during boot.
sleep 12
exec systemctl --user start duckbooth.service
