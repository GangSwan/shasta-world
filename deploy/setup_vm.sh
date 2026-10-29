#!/usr/bin/env bash
# Sets up shasta-world on an Ubuntu Server VM that's already on Tailscale. Run once as root;
# running it again is safe (it never replaces the config, the tokens or the world's data).
#
#   curl -fsSL https://raw.githubusercontent.com/GangSwan/shasta-world/main/deploy/setup_vm.sh -o setup_vm.sh
#   sudo bash setup_vm.sh
#
# What it makes:
#   shasta                 a system user with no login; runs the server and the auto-deploy
#   /opt/shasta-world      a checkout of main (owned by shasta)
#   /etc/shasta-world      config.json with one fresh token per creature (root:shasta, 640)
#   /var/lib/shasta-world  the world's database and its 14 daily backups
#   shasta-world.service   the server, on the Tailscale address only, restarted if it dies
#   shasta-world-update.timer   every 5 minutes: pull main, run the tests on it, restart only if they pass
set -euo pipefail

REPO=https://github.com/GangSwan/shasta-world
APP=/opt/shasta-world
DATA=/var/lib/shasta-world
CONF=/etc/shasta-world
PORT=8642

[ "$(id -u)" = 0 ] || { echo "Run me as root: sudo bash $0"; exit 1; }
TS_IP=$(tailscale ip -4 2>/dev/null | head -1 || true)
[ -n "$TS_IP" ] || { echo "Tailscale isn't up on this machine (tailscale ip -4 is empty). Run: sudo tailscale up"; exit 1; }

echo "== packages"
apt-get install -y -q git python3 >/dev/null

echo "== user and folders"
id shasta >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin shasta
install -d -o shasta -g shasta -m 750 "$DATA" "$DATA/backups"
install -d -o root -g shasta -m 750 "$CONF"

echo "== code"
if [ ! -d "$APP/.git" ]; then
  git clone -q "$REPO" "$APP"
fi
chown -R shasta:shasta "$APP"

echo "== config"
if [ ! -f "$CONF/config.json" ]; then
  python3 - "$CONF/config.json" "$TS_IP" "$PORT" "$DATA" <<'EOF'
import json, secrets, sys
path, host, port, data = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
cfg = {"host": host, "port": port, "db": f"{data}/world.db", "backups": f"{data}/backups",
       "creatures": {"bolt": {"name": "Bolt", "home": "nest", "token": secrets.token_urlsafe(32)},
                     "spike": {"name": "Spike", "home": "fir", "token": secrets.token_urlsafe(32)}}}
with open(path, "w") as f:
    f.write(json.dumps(cfg, indent=2) + "\n")
EOF
  echo "   made $CONF/config.json with new tokens for bolt and spike"
else
  echo "   $CONF/config.json already exists; keeping it and its tokens"
fi
chown root:shasta "$CONF/config.json"
chmod 640 "$CONF/config.json"

echo "== services"
install -m 644 "$APP/deploy/shasta-world.service" "$APP/deploy/shasta-world-update.service" \
               "$APP/deploy/shasta-world-update.timer" /etc/systemd/system/
# The one thing the auto-deploy may do as root: restart the server.
echo 'shasta ALL=(root) NOPASSWD: /usr/bin/systemctl restart shasta-world' > /etc/sudoers.d/shasta-world
chmod 440 /etc/sudoers.d/shasta-world
visudo -cqf /etc/sudoers.d/shasta-world
systemctl daemon-reload
systemctl enable -q --now shasta-world.service shasta-world-update.timer
systemctl restart shasta-world.service

echo "== checking"
for _ in $(seq 1 20); do
  code=$(python3 -c "import urllib.request,urllib.error
try: urllib.request.urlopen('http://$TS_IP:$PORT/v1/town', timeout=2)
except urllib.error.HTTPError as e: print(e.code)
except Exception: print(0)" || true)
  [ "$code" = 401 ] && break
  sleep 1
done
if [ "$code" = 401 ]; then
  echo "   shasta-world is up on http://$TS_IP:$PORT (it answers 401 without a token, as it should)"
else
  echo "   shasta-world didn't answer. Look at: journalctl -u shasta-world -n 50"
  exit 1
fi

cat <<EOF

Done. Next:
  - The creature tokens are in $CONF/config.json:   sudo cat $CONF/config.json
    Spike's goes in Spike's config. Bolt's goes to Caelan privately, never in a repo or a public chat.
  - Logs:          journalctl -u shasta-world -f
  - Auto-deploy:   journalctl -u shasta-world-update -n 30
  - Backups:       $DATA/backups (one a day, 14 kept). Also put this VM in a Proxmox backup schedule.
EOF
