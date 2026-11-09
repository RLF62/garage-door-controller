#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
if [[ "$project_dir" != /home/pi/garage ]] || ! id pi >/dev/null 2>&1; then
    printf '%s\n' 'The dashboard unit expects /home/pi/garage and the pi account.' >&2
    exit 1
fi

unit=/etc/systemd/system/garage.service
if sudo test -e "$unit" && ! sudo cmp -s "$project_dir/deploy/garage.service" "$unit"; then
    printf '%s\n' 'An existing garage.service differs. Review it before replacing it.' >&2
    exit 1
fi

sudo apt-get install -y python3-flask python3-gpiozero python3-rpi.gpio python3-serial iputils-ping iw
sudo install -m 0644 "$project_dir/deploy/garage.service" "$unit"
sudo systemctl daemon-reload
printf '%s\n' 'Dashboard unit installed. Complete hardware setup before starting garage.service.'
