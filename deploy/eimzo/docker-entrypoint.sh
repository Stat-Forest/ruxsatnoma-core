#!/bin/sh
# `config.properties` ships with `${EIMZO_VPN_HOST}`/`${EIMZO_VPN_PORT}`/
# `${EIMZO_VPN_KEY_PASSWORD}` placeholders (plan 05.2 task 8) -- e-imzo-server
# itself is not known to expand them, so this entrypoint renders the file from
# THIS container's own environment (set by the compose service, from .env)
# before ever starting the jar. Idempotent: safe to run on every restart.
set -eu

envsubst <config.properties >config.properties.rendered
mv config.properties.rendered config.properties

exec java -Dfile.encoding=UTF-8 -jar e-imzo-server.jar config.properties
