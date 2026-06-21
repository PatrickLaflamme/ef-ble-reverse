#!/usr/bin/env bash
# Privileged installer for the EcoFlow parallel charge controller.
# Run from a real terminal:  sudo bash deploy/install.sh
# Idempotent: safe to re-run. See deploy/INSTALL.md for the manual walkthrough.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST=/opt/ecoflow-rotator
ENVFILE=/etc/ecoflow/rotator.env
PLACEHOLDER="1234567890123456789"

if [[ $EUID -ne 0 ]]; then
  echo "ERROR: run as root:  sudo bash deploy/install.sh" >&2
  exit 1
fi
if ! id ecoflow &>/dev/null; then
  echo "ERROR: user 'ecoflow' does not exist (expected from the keeper deploy)." >&2
  exit 1
fi

echo "==> [1/6] Deploy code to $DEST"
install -d -o ecoflow -g ecoflow "$DEST" "$DEST/web"
install -o ecoflow -g ecoflow -m644 -t "$DEST" \
  "$REPO_DIR"/controller.py "$REPO_DIR"/policy.py "$REPO_DIR"/connect.py \
  "$REPO_DIR"/yj751_sys_pb2_v4.py "$REPO_DIR"/pd303_pb2_v4.py "$REPO_DIR"/utc_sys_pb2_v4.py
install -o ecoflow -g ecoflow -m644 "$REPO_DIR"/web/index.html "$DEST/web/"
# Stage requirements.txt into $DEST so the ecoflow user can read it (it cannot
# traverse into /home/stoneh to pip-install directly from the repo).
install -o ecoflow -g ecoflow -m644 "$REPO_DIR"/requirements.txt "$DEST/"
if [[ -f "$REPO_DIR/login_key.bin" ]]; then
  install -o ecoflow -g ecoflow -m600 "$REPO_DIR/login_key.bin" "$DEST/"
else
  echo "    WARN: $REPO_DIR/login_key.bin not found — copy it to $DEST/ before starting the service"
fi

echo "==> [2/6] Python deps into the deploy venv"
if [[ -x "$DEST/.venv/bin/pip" ]]; then
  sudo -u ecoflow "$DEST/.venv/bin/pip" install -q -r "$DEST/requirements.txt"
else
  echo "    WARN: $DEST/.venv missing; create it:  sudo -u ecoflow python -m venv $DEST/.venv"
fi

echo "==> [3/6] Config $ENVFILE"
install -d -o root -g root /etc/ecoflow
if [[ ! -f "$ENVFILE" ]]; then
  install -o root -g ecoflow -m640 "$REPO_DIR/deploy/rotator.env.example" "$ENVFILE"
  echo "    created $ENVFILE from example — EDIT IT (USER_ID + the two BLE addresses)"
fi

echo "==> [4/6] BlueZ access for headless ecoflow user (DBus + polkit)"
cat >/etc/dbus-1/system.d/ecoflow-bluez.conf <<'EOF'
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <policy user="ecoflow">
    <allow send_destination="org.bluez"/>
    <allow send_destination="org.bluez" send_interface="org.freedesktop.DBus.ObjectManager"/>
    <allow send_destination="org.bluez" send_interface="org.freedesktop.DBus.Properties"/>
  </policy>
</busconfig>
EOF
cat >/etc/polkit-1/rules.d/51-ecoflow-bluez.rules <<'EOF'
polkit.addRule(function(action, subject) {
  if (action.id.indexOf("org.bluez.") === 0 && subject.user === "ecoflow") {
    return polkit.Result.YES;
  }
});
EOF
systemctl reload dbus 2>/dev/null || echo "    NOTE: reload dbus failed; a reboot will apply the policy"

echo "==> [5/6] systemd unit ecoflow-rotator.service"
install -m644 "$REPO_DIR/deploy/ecoflow-rotator.service" /etc/systemd/system/
systemctl daemon-reload

echo "==> [6/6] nginx reverse proxy"
if ! command -v nginx &>/dev/null; then
  pacman -S --needed --noconfirm nginx
fi
install -d /etc/nginx/conf.d
install -m644 "$REPO_DIR/deploy/nginx-ecoflow-controller.conf" /etc/nginx/conf.d/ecoflow-controller.conf
if ! grep -qE 'include\s+/etc/nginx/conf\.d/\*\.conf;' /etc/nginx/nginx.conf; then
  sed -i '0,/^\s*http\s*{/s//http {\n    include \/etc\/nginx\/conf.d\/*.conf;/' /etc/nginx/nginx.conf
  echo "    added 'include /etc/nginx/conf.d/*.conf;' to nginx.conf http{}"
fi
grep -q 'ecoflow-controller.local' /etc/hosts || \
  echo '127.0.0.1   ecoflow-controller.local' >>/etc/hosts
nginx -t
systemctl enable --now nginx
systemctl reload nginx || systemctl restart nginx

echo
echo "==> nginx is up. Portal proxied at http://ecoflow-controller.local"
if grep -q "$PLACEHOLDER" "$ENVFILE" 2>/dev/null; then
  echo "==> NEXT: edit $ENVFILE (still has placeholder values), then:"
  echo "      sudo systemctl enable --now ecoflow-rotator.service"
  echo "      journalctl -u ecoflow-rotator -f"
else
  echo "==> Starting ecoflow-rotator.service"
  systemctl enable --now ecoflow-rotator.service
  echo "    watch:  journalctl -u ecoflow-rotator -f"
fi
