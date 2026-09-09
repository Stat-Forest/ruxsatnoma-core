#!/bin/sh
# config.properties ships with ${EIMZO_VPN_HOST}/${EIMZO_VPN_PORT}/
# ${EIMZO_VPN_KEY_PASSWORD} placeholders (plan 05.2 task 8) -- e-imzo-server
# itself is not known to expand them, so this entrypoint renders the file from
# THIS container's own environment (set by the compose service, from .env)
# before ever starting the jar. Idempotent: envsubst leaves already-resolved
# text alone, so re-running it against last run's rendered file on a plain
# `docker restart` (not a rebuild -- the writable layer survives that) is a
# no-op rather than a corruption.
set -eu

envsubst <config/config.properties >config/config.properties.rendered
mv config/config.properties.rendered config/config.properties

# Fix round 1, critical finding 2: the config path MUST be passed as the
# system property `properties.filename`, never as a bare positional argument.
# `Application` is a picocli command with subcommands and no positional
# fields, so a positional string is parsed as an unknown subcommand and the
# process aborts before config, VPN or /ping are ever reached -- the previous
# entrypoint's `-jar e-imzo-server.jar config.properties` never actually
# started. This mirrors the vendor's own Dockerfile CMD exactly (including
# `-Djava.util.logging.config.file` and the `$JAVA_OPTS` passthrough for
# anyone who needs to tune heap size later).
exec java -Dfile.encoding=UTF-8 \
  -Djava.util.logging.config.file=config/logging.properties \
  -Dproperties.filename=config/config.properties \
  ${JAVA_OPTS:-} \
  -jar e-imzo-server.jar
