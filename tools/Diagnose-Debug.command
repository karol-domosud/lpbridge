#!/bin/zsh
cd -- "${0:A:h}/.." || exit 1
print 'First connect to Wi-Fi Debug and open lpbridge on Find your printer.'
read 'ready?Press Return when ready (this does not switch networks): '
/usr/bin/python3 tools/diagnose-network.py
read 'done?Press Return to close this window: '
