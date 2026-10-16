#!/bin/sh
set -eu
LABEL=local.clash-party.auto-health
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$PLIST"
printf '%s\n' "Auto Health LaunchAgent removed. Remove the generated override from Clash Party and reload the profile to disable the rules."
