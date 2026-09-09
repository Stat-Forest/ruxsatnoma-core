# e-imzo-server v2.1.1 (plan 05.2, task 8).
#
# Base image per the vendor's own v2.1.1 distribution (`e-imzo-server-v2.1.1.zip`,
# released 2026-05-25, unpacked during this stage's research): amazoncorretto:8-alpine3.19-jre.
#
# `e-imzo-server.jar` is NOT fetched by this Dockerfile. It is NIC's own proprietary
# artifact, distributed together with the VPN key and truststores (never a public
# download -- see README.md) and gitignored the same way `keys/` is. Place it at
# `deploy/eimzo/e-imzo-server.jar` before building -- the build fails loudly on the
# `COPY` below without it, which is the point: an operator who forgot the jar sees a
# build error, not a container that starts and does nothing.
FROM amazoncorretto:8-alpine3.19-jre

# `gettext` (envsubst) renders config.properties's `${EIMZO_VPN_HOST}`/etc.
# placeholders from the container's own environment at startup
# (docker-entrypoint.sh); `wget` is what the compose healthcheck runs against
# `/ping`. Neither ships in the alpine-jre base.
RUN apk add --no-cache gettext wget

WORKDIR /opt/e-imzo-server

COPY config.properties ./config.properties
COPY docker-entrypoint.sh ./docker-entrypoint.sh
RUN chmod +x ./docker-entrypoint.sh

COPY e-imzo-server.jar ./e-imzo-server.jar

# `keys/` is mounted read-only by the compose service (docker-compose.yml,
# docker-compose.deploy.yml) -- nothing to COPY here, and nothing to build
# without it: the jar starts and answers /ping with "certificate status could
# not be verified" until the VPN key exists at that mount point (README.md).

EXPOSE 8080 8081

ENTRYPOINT ["./docker-entrypoint.sh"]
