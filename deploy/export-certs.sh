#!/usr/bin/env bash
# deploy/export-certs.sh
# Export the certificates Caddy holds, for installation on the Kerio Control gateway.
#
# WHY THIS EXISTS. The gateway terminates TLS itself and the network team cannot provide a
# plain DNAT forward (2026-09-04), so the certificate a visitor sees is the gateway's, not
# Caddy's. Caddy still obtains and renews real Let's Encrypt certificates over the gateway's
# port-80 publication; this script hands them over in the two files Kerio expects.
#
# THE OPERATIONAL COST, STATED PLAINLY. Let's Encrypt certificates last 90 days and Caddy
# renews them at ~60. The renewal is automatic; INSTALLING the renewed file on the gateway is
# not. If nobody re-exports and re-installs, all three sites start showing a security warning
# on the day the installed copy expires. Run this after each renewal — `--check` tells you
# whether the exported copy is stale.
#
# Usage:
#   bash deploy/export-certs.sh            # export into ~/certs-for-gateway
#   bash deploy/export-certs.sh --check    # report expiry dates, export nothing
#   bash deploy/export-certs.sh /some/dir  # export elsewhere
set -euo pipefail

CONTAINER="${CADDY_CONTAINER:-ruxsatnoma-proxy-caddy-1}"
CERT_ROOT=/data/caddy/certificates
DOMAINS=(
	dev-api.ruxsatnoma-urmon.uz
	dev-admin.ruxsatnoma-urmon.uz
	dev.ruxsatnoma-urmon.uz
)

mode=dump
out_dir="$HOME/certs-for-gateway"
case "${1:-}" in
	--check) mode=check ;;
	"") ;;
	*) out_dir="$1" ;;
esac

if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
	echo "error: container '$CONTAINER' not found — is the proxy stack up?" >&2
	exit 1
fi

# Caddy stores each certificate under an issuer-specific directory, and the issuer differs
# between Let's Encrypt and ZeroSSL (it falls back). Search rather than assume a path.
find_cert() {
	docker exec "$CONTAINER" find "$CERT_ROOT" -name "$1.crt" 2>/dev/null | head -1
}

[ "$mode" = dump ] && { umask 077; mkdir -p "$out_dir"; }

missing=0
for domain in "${DOMAINS[@]}"; do
	crt_path=$(find_cert "$domain")
	if [ -z "$crt_path" ]; then
		echo "MISSING  $domain — no certificate yet."
		echo "         Caddy can only obtain one once the gateway publishes"
		echo "         http://$domain/.well-known/acme-challenge/ to this server."
		missing=$((missing + 1))
		continue
	fi
	key_path="${crt_path%.crt}.key"

	not_after=$(docker exec "$CONTAINER" cat "$crt_path" \
		| openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
	issuer=$(docker exec "$CONTAINER" cat "$crt_path" \
		| openssl x509 -noout -issuer 2>/dev/null | sed 's/.*CN *= *//')

	if [ "$mode" = check ]; then
		printf 'OK       %-32s expires %s (issuer %s)\n' "$domain" "$not_after" "$issuer"
		continue
	fi

	docker exec "$CONTAINER" cat "$crt_path" > "$out_dir/$domain.fullchain.pem"
	docker exec "$CONTAINER" cat "$key_path" > "$out_dir/$domain.privkey.pem"
	chmod 600 "$out_dir/$domain.fullchain.pem" "$out_dir/$domain.privkey.pem"
	printf 'EXPORTED %-32s expires %s\n' "$domain" "$not_after"
done

if [ "$mode" = dump ]; then
	echo
	echo "Files are in $out_dir (mode 600, owned by $(id -un))."
	echo "The private keys must never travel over chat or e-mail — the network team fetches"
	echo "them over SSH from this server, which they already have access to."
fi

[ "$missing" -gt 0 ] && exit 2
exit 0
