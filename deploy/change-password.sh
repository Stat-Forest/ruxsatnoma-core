#!/usr/bin/env bash
# deploy/change-password.sh
# Change a staff account's password against a running API, from the command line.
#
# WHY THIS EXISTS. Every account the bootstrap CLI creates carries
# `must_change_password`, which makes the API refuse every route but `GET /auth/me` with
# ERR-AUTH-007 until the password is changed. The adminka screen that would do this (C5) is
# stage 6.1's and does not exist yet, so a freshly bootstrapped operator cannot get past the
# blocking notice through the UI. This script drives the same three endpoints the screen will.
#
# Delete it once the C5 screen ships — it is a stopgap, not a permanent tool.
#
# It never takes a password on the command line (that would put it in the shell history and
# in the process list); everything sensitive is read interactively without echo.
#
# Usage:
#   bash deploy/change-password.sh [https://dev-api.ruxsatnoma-urmon.uz]
set -euo pipefail

BASE="${1:-https://dev-api.ruxsatnoma-urmon.uz}"
JAR=$(mktemp)
# -k because the Kerio gateway currently presents its own expired certificate; drop it once
# the real certificates are installed there.
CURL=(curl -sk --max-time 20 -c "$JAR" -b "$JAR")
cleanup() { rm -f "$JAR"; }
trap cleanup EXIT

command -v python3 >/dev/null || { echo "python3 is required (JSON parsing)" >&2; exit 1; }

json_get() { python3 -c 'import json,sys; print(json.load(sys.stdin).get(sys.argv[1],""))' "$1"; }

read -rp "login: " LOGIN
read -rsp "current password: " OLD_PASSWORD; echo
read -rsp "new password: " NEW_PASSWORD; echo
read -rsp "repeat new password: " NEW_PASSWORD_AGAIN; echo
[ "$NEW_PASSWORD" = "$NEW_PASSWORD_AGAIN" ] || { echo "the two new passwords differ" >&2; exit 1; }

echo "==> login"
login_body=$(python3 -c 'import json,sys; print(json.dumps({"login": sys.argv[1], "password": sys.argv[2]}))' "$LOGIN" "$OLD_PASSWORD")
login_out=$("${CURL[@]}" -X POST "$BASE/api/v1/auth/login" \
	-H 'Content-Type: application/json' -d "$login_body")
MFA_TOKEN=$(printf '%s' "$login_out" | json_get mfa_token)
if [ -z "$MFA_TOKEN" ]; then
	echo "login failed: $login_out" >&2
	exit 1
fi

# The TOTP code comes from the authenticator app enrolled with the otpauth:// URI that
# `python -m app.bootstrap` printed. It is 6 digits and changes every 30 seconds — if the
# next step reports an invalid code, you were simply too slow; re-run.
read -rp "6-digit code from the authenticator app: " CODE

echo "==> verifying the second factor"
mfa_body=$(python3 -c 'import json,sys; print(json.dumps({"mfa_token": sys.argv[1], "code": sys.argv[2]}))' "$MFA_TOKEN" "$CODE")
mfa_out=$("${CURL[@]}" -X POST "$BASE/api/v1/auth/mfa/verify" \
	-H 'Content-Type: application/json' -d "$mfa_body")
CSRF=$(printf '%s' "$mfa_out" | json_get csrf_token)
if [ -z "$CSRF" ]; then
	echo "second factor failed: $mfa_out" >&2
	exit 1
fi

echo "==> changing the password"
change_body=$(python3 -c 'import json,sys; print(json.dumps({"old_password": sys.argv[1], "new_password": sys.argv[2]}))' "$OLD_PASSWORD" "$NEW_PASSWORD")
status=$("${CURL[@]}" -o /tmp/pwchange.$$ -w '%{http_code}' -X POST "$BASE/api/v1/auth/password/change" \
	-H 'Content-Type: application/json' -H "X-CSRF-Token: $CSRF" -d "$change_body")
body=$(cat "/tmp/pwchange.$$" 2>/dev/null || true)
rm -f "/tmp/pwchange.$$"

if [ "$status" = "204" ]; then
	echo "done — the password is changed and must_change_password is cleared."
	echo "Every other session of this account was revoked, which is intended."
else
	echo "failed (HTTP $status): $body" >&2
	exit 1
fi
