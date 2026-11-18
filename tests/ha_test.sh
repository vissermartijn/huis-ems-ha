#!/bin/sh
# Start een schone wegwerp-Home Assistant met nepsensoren en draait de rooktest.
set -e
cd "$(dirname "$0")/.."
WORK="${TMPDIR:-${LOCALAPPDATA:-/tmp}/Temp}/huis-ems-ha-test"
docker rm -f ha-ems-test >/dev/null 2>&1 || true   # eerst de container, die houdt de map vast
rm -rf "$WORK"; mkdir -p "$WORK"
export EMS_TEST_WORK="$WORK"
# Vaste, geteste versie; een andere kies je met HA_VERSION=2026.12.0 sh tests/ha_test.sh
HA_VERSION="${HA_VERSION:-2026.11.1}"
cp tests/ha_test_configuration.yaml "$WORK/configuration.yaml"
cp homeassistant/dashboard.yaml "$WORK/ems_dashboard.yaml"
cp homeassistant/grafieken.yaml "$WORK/ems_grafieken.yaml"
# De losse kaart in een gewoon dashboard, zoals iemand hem in een bestaand dashboard plakt.
{ printf 'views:\n  - title: Kaart\n    path: kaart\n    cards:\n'; sed 's/^/      /; s/^      type: vertical-stack/      - type: vertical-stack/; s/^      cards:/        cards:/' homeassistant/grafieken-kaart.yaml; } > "$WORK/ems_kaart.yaml"
# Grafieken bekijken: APEX_JS=/pad/naar/apexcharts-card.js sh tests/ha_test.sh --serve
mkdir -p "$WORK/www"
if [ -n "$APEX_JS" ]; then cp "$APEX_JS" "$WORK/www/apexcharts-card.js"; else : > "$WORK/www/apexcharts-card.js"; fi
if command -v cygpath >/dev/null; then W="$(cygpath -w "$WORK")"; C="$(cygpath -w "$PWD/custom_components")"; else W="$WORK"; C="$PWD/custom_components"; fi
MSYS_NO_PATHCONV=1 docker run -d --name ha-ems-test -p 8124:8123 --add-host host.docker.internal:host-gateway -e TZ=Europe/Amsterdam \
  -v "$W:/config" -v "$C:/config/custom_components" "ghcr.io/home-assistant/home-assistant:$HA_VERSION" >/dev/null
set +e
python tests/ha_smoke.py http://localhost:8124 "$@"
STATUS=$?
set -e
echo "--- meldingen van de integratie ---"
docker logs ha-ems-test 2>&1 | grep -iE "huis_ems|Traceback" | grep -v "custom integration" | tail -20 || true
if docker logs ha-ems-test 2>&1 | grep -q "Traceback"; then echo "FOUT: Home Assistant meldt een Traceback"; STATUS=1; fi
exit $STATUS
