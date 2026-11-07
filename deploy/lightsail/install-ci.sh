#!/bin/sh
# Install only on the already provisioned, dedicated Yeoun Lightsail instance.
set -eu
test "$(id -u)" = 0
source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
root=/opt/yeoun
test -f /etc/yeoun/api.env
test -f "$root/web-dist/index.html"
docker inspect yeoun-demo-api-1 >/dev/null
install -d -m 755 "$root/www" "$root/www/releases" /usr/local/lib/yeoun-deploy
install -d -m 700 /var/lib/yeoun-deploy
if [ ! -e "$root/www/current" ]; then
  install -d -m 755 "$root/www/releases/bootstrap"
  cp -a "$root/web-dist/." "$root/www/releases/bootstrap/"
  cp -a "$root/web-dist/assets" "$root/www/assets"
  ln -s releases/bootstrap "$root/www/current"
fi
if [ ! -f /etc/yeoun/runtime.env ]; then
  image=$(docker inspect yeoun-demo-api-1 --format '{{.Config.Image}}')
  (umask 077; printf 'YEOUN_API_IMAGE=%s\n' "$image" > /etc/yeoun/runtime.env)
fi
if [ ! -f "$root/www/deployment-status.json" ]; then
  printf '{}\n' > "$root/www/deployment-status.json"
  chmod 644 "$root/www/deployment-status.json"
fi
backup="$root/ci-config-before-$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 700 "$backup"
cp "$root/deploy/lightsail/compose.yaml" "$root/deploy/lightsail/Caddyfile" "$backup/"
install -m 644 "$source_dir/compose.yaml" "$root/deploy/lightsail/compose.yaml"
install -m 644 "$source_dir/Caddyfile" "$root/deploy/lightsail/Caddyfile"
install -m 644 "$source_dir/pull_release.py" /usr/local/lib/yeoun-deploy/pull_release.py
install -m 644 "$source_dir/yeoun-deploy.service" /etc/systemd/system/yeoun-deploy.service
install -m 644 "$source_dir/yeoun-deploy.timer" /etc/systemd/system/yeoun-deploy.timer
docker compose --env-file /etc/yeoun/runtime.env -f "$root/deploy/lightsail/compose.yaml" --project-directory "$root" config -q
docker run --rm --env-file /etc/yeoun/site.env \
  -v "$root/deploy/lightsail/Caddyfile:/etc/caddy/Caddyfile:ro" \
  caddy:2-alpine caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker compose --env-file /etc/yeoun/runtime.env -f "$root/deploy/lightsail/compose.yaml" --project-directory "$root" up -d --no-deps --no-build caddy
systemctl daemon-reload
systemctl enable --now yeoun-deploy.timer
echo 'Yeoun release polling enabled. Existing API and PostgreSQL containers were preserved.'
