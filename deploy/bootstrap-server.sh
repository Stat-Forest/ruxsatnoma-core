#!/usr/bin/env bash
# deploy/bootstrap-server.sh
# One-time preparation of a fresh dev server. Idempotent: safe to re-run.
#
# Run it ON THE SERVER, as the deploy user, after Docker is installed:
#   bash deploy/bootstrap-server.sh
#
# It creates the directory layout, the shared network and a .env rendered from
# deploy/.env.deploy.template with fresh generated secrets. It never overwrites an
# existing .env — the database and MinIO passwords are baked into their volumes on first
# start, so regenerating them would lock the data away.
#
# No sudo anywhere in this script: this server's sudo requires an interactive password,
# which would hang an unattended run forever. Membership in the docker group already
# grants everything below, and the deploy user already owns its own home directory.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$SCRIPT_DIR/.env.deploy.template"

API_DIR="$HOME/ruxsatnoma-api"
PROXY_DIR="$HOME/ruxsatnoma-proxy"
ADMIN_DIR="$HOME/ruxsatnoma-admin"
LANDING_DIR="$HOME/ruxsatnoma-landing"

echo "==> checking envsubst"
if ! command -v envsubst >/dev/null 2>&1; then
	echo "error: envsubst not found — install it with: apt-get install -y gettext-base" >&2
	exit 1
fi

echo "==> directories"
for d in "$API_DIR" "$PROXY_DIR" "$ADMIN_DIR" "$LANDING_DIR"; do
	mkdir -p "$d"
done

echo "==> shared edge network"
if ! docker network inspect ruxsatnoma-edge >/dev/null 2>&1; then
	docker network create --subnet 172.28.0.0/24 ruxsatnoma-edge
	echo "    created"
else
	echo "    already present"
fi

echo "==> .env"
if [ -f "$API_DIR/.env" ]; then
	echo "    already present — left untouched"
else
	if [ ! -f "$TEMPLATE" ]; then
		echo "error: template not found: $TEMPLATE" >&2
		exit 1
	fi
	umask 077
	SECRET_KEY=$(openssl rand -hex 32)
	POSTGRES_PASSWORD=$(openssl rand -hex 24)
	S3_SECRET_KEY=$(openssl rand -hex 24)
	export SECRET_KEY POSTGRES_PASSWORD S3_SECRET_KEY
	# Single quotes are deliberate: this is envsubst's variable-list argument, so the
	# literal `$VAR` tokens must reach envsubst unexpanded — it does its own substitution
	# against the exported environment, restricted to just these three names.
	# shellcheck disable=SC2016
	envsubst '$SECRET_KEY $POSTGRES_PASSWORD $S3_SECRET_KEY' \
		< "$TEMPLATE" > "$API_DIR/.env"
	echo "    generated with fresh secrets"
fi

echo "==> done. Next: copy deploy/proxy/* to $PROXY_DIR and docker-compose.deploy.yml to $API_DIR, then start each stack."
