# e-imzo-server v2.1.1 (plan 05.2, task 8; fix round 1).
#
# The vendor ships its own Dockerfile inside the distribution zip
# (e-imzo-server-v2.1.1.zip), alongside the jar and lib/ -- it is the
# authority this file is built from, not a guess. Base image, the config/
# layout and the CMD's system properties below are taken from it directly.
# Deliberate differences from the vendor's own Dockerfile, each commented at
# the point it applies:
#   1. `gettext` (envsubst) and `wget` are installed -- the vendor's own file
#      needs neither. `envsubst` expands THIS project's own
#      `${EIMZO_VPN_HOST}`-style placeholders in config.properties from the
#      container's real environment (docker-entrypoint.sh); the vendor's own
#      config.properties has no placeholders at all. `wget` is what the
#      compose healthcheck runs against `/ping`.
#   2. The jar and lib/ are copied from `dist/` (this project's own staging
#      directory, gitignored, assembled by whoever builds the image) rather
#      than from the zip root the vendor's Dockerfile assumes it is built
#      inside of.
#   3. `keys/` is NOT copied into the image (the vendor's own Dockerfile does
#      copy it) -- the compose service bind-mounts it read-only instead, so a
#      key can be rotated or a test key swapped for a production one without
#      rebuilding.
FROM amazoncorretto:8-alpine3.22-jre

RUN apk add --no-cache gettext wget

WORKDIR /opt/e-imzo-server

# Both properties files live under config/, exactly like the vendor's own
# Dockerfile -- the CMD below points at them by that same relative path.
COPY config.properties ./config/config.properties
COPY logging.properties ./config/logging.properties

# `e-imzo-server.jar` is a THIN jar: its manifest's `Class-Path` names every
# entry in `lib/` individually (confirmed: `unzip -p e-imzo-server.jar
# META-INF/MANIFEST.MF`), and `Application.main()`'s very first instruction
# constructs a `picocli.CommandLine` -- a class that exists only in `lib/`.
# Without this directory the container dies at class loading with
# `NoClassDefFoundError`, before config, VPN or /ping are ever reached
# (fix round 1, critical finding 1). Both the jar and lib/ are NIC's own
# proprietary artifacts, distributed together with the VPN key (never a
# public download -- see README.md) and gitignored (`dist/`) the same way
# `keys/` is. Assemble them at `deploy/eimzo/dist/{e-imzo-server.jar,lib/}`
# before building -- the build fails loudly on the COPY below without them,
# which is the point: an operator who forgot the distribution sees a build
# error, not a container that starts and immediately dies.
COPY dist/e-imzo-server.jar ./e-imzo-server.jar
COPY dist/lib ./lib

COPY docker-entrypoint.sh ./docker-entrypoint.sh
RUN chmod +x ./docker-entrypoint.sh

# keys/ is mounted read-only by the compose service (docker-compose.yml,
# docker-compose.deploy.yml) -- nothing to COPY here, and nothing to build
# without it. Measured, not assumed (README.md): with no VPN key at that
# mount point, the jar does NOT start cleanly and does NOT answer /ping at
# all -- it never binds port 8080, because a constructor on the startup path
# opens keys/client.key eagerly and throws FileNotFoundException before the
# HTTP listener is ever wired up.

EXPOSE 8080 8081

ENTRYPOINT ["./docker-entrypoint.sh"]
