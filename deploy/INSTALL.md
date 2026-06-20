# Deploying the EcoFlow parallel charge controller

Runs `controller.py` as a systemd daemon (user `ecoflow`) at
`/opt/ecoflow-rotator`, with an nginx reverse proxy exposing the portal at
`http://ecoflow-controller.local`.

All steps below require root (run in a real terminal with sudo).

## 1. Deploy code to /opt/ecoflow-rotator

```bash
# from the repo checkout (e.g. /home/stoneh/ef-ble-reverse)
sudo install -o ecoflow -g ecoflow -m644 -t /opt/ecoflow-rotator \
    controller.py policy.py connect.py \
    yj751_sys_pb2_v4.py pd303_pb2_v4.py utc_sys_pb2_v4.py
sudo mkdir -p /opt/ecoflow-rotator/web
sudo install -o ecoflow -g ecoflow -m644 web/index.html /opt/ecoflow-rotator/web/

# login_key.bin (same file connect.py uses) must sit in the WorkingDirectory
sudo install -o ecoflow -g ecoflow -m600 login_key.bin /opt/ecoflow-rotator/

# install python deps into the existing deploy venv
sudo -u ecoflow /opt/ecoflow-rotator/.venv/bin/pip install -r requirements.txt
```

## 2. Config

```bash
sudo cp deploy/rotator.env.example /etc/ecoflow/rotator.env
sudoedit /etc/ecoflow/rotator.env          # fill in USER_ID + the two BLE addrs
sudo chown root:ecoflow /etc/ecoflow/rotator.env
sudo chmod 640 /etc/ecoflow/rotator.env
```

## 3. Bluetooth access for the `ecoflow` user

The daemon talks to BlueZ over the DBus system bus. A headless system user has
no polkit session, so if the service logs `org.bluez ... AccessDenied` or
`NotAuthorized`, install the DBus policy + polkit rule:

```bash
# DBus: let ecoflow call org.bluez
sudo tee /etc/dbus-1/system.d/ecoflow-bluez.conf >/dev/null <<'EOF'
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

# polkit: allow ecoflow the bluez actions without an active session
sudo tee /etc/polkit-1/rules.d/51-ecoflow-bluez.rules >/dev/null <<'EOF'
polkit.addRule(function(action, subject) {
  if (action.id.indexOf("org.bluez.") === 0 && subject.user === "ecoflow") {
    return polkit.Result.YES;
  }
});
EOF

sudo systemctl reload dbus      # or reboot if reload is unavailable
```

## 4. systemd service

```bash
sudo cp deploy/ecoflow-rotator.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ecoflow-rotator.service
journalctl -u ecoflow-rotator -f      # watch it scan, connect, and CTRL: lines
```

This is a **new** unit alongside `ecoflow-keeper.service` (the MQTT keep-alive);
the keeper is left untouched.

## 5. nginx + hostname

```bash
sudo pacman -S nginx
sudo cp deploy/nginx-ecoflow-controller.conf /etc/nginx/conf.d/ecoflow-controller.conf

# ensure /etc/nginx/nginx.conf has, inside the http { } block:
#     include /etc/nginx/conf.d/*.conf;
sudo grep -q 'conf.d/\*.conf' /etc/nginx/nginx.conf || \
    echo 'EDIT /etc/nginx/nginx.conf: add "include /etc/nginx/conf.d/*.conf;" in http{}'

echo '127.0.0.1   ecoflow-controller.local' | sudo tee -a /etc/hosts

sudo nginx -t && sudo systemctl enable --now nginx
```

Open <http://ecoflow-controller.local>.

## Notes / troubleshooting

- **First run is read-then-act**: the controller forces both units to the
  policy's desired state on startup (idempotent), then reacts to heartbeats.
- **`set_self` vs `set_para`**: the OFF path sends `PrStateSet{set_self=0}`. If a
  unit refuses to detach on real hardware, the firmware may also want
  `set_para=0` — change `_apply` in `controller.py` accordingly.
- **MemoryDenyWriteExecute** is intentionally omitted from the unit (it can break
  Python C-extensions). Re-add only after verifying the service still starts.
- **Portal is bound to localhost**; only reachable through nginx. Add HTTP basic
  auth in the nginx server block if you want a login.
