#!/usr/bin/env bash
# Obtain/renew the TLS certificate for the public proxy (ACME via lego HTTP-01) and hot-reload nginx.
#
# Same scheme as the other sites on this host: host port 80 is opened only for the seconds of a renewal,
# under a host-wide lock shared by every project. Settings come from .env (never committed):
#   CERT_IDENTIFIER   domain the certificate is for (monitor.<domain>)
#   CERT_PROFILE      ACME profile ("none" for ZeroSSL; "shortlived" only for Let's Encrypt IP certificates)
#   ACME_SERVER       default: Let's Encrypt production. This host uses ZeroSSL for *.example.org because
#                     example.org is not on the Public Suffix List (Let's Encrypt rate-limits the shared parent)
#   ACME_EMAIL        contact address for the ACME account
#   ACME_EAB_KID / ACME_EAB_HMAC   external account binding (required by ZeroSSL)
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f .env ]] && set -a && . ./.env && set +a
ID="${CERT_IDENTIFIER:?set CERT_IDENTIFIER in .env}"
PROFILE="${CERT_PROFILE:-none}"
SERVER="${ACME_SERVER:-https://acme-v02.api.letsencrypt.org/directory}"
CERTS="deploy/certs"
TLS="${TLS_DIR:-deploy/tls}"
mkdir -p "$CERTS" "$TLS"

ARGS=(--accept-tos --server "$SERVER" --domains "$ID" --http --path /certs)
[[ -n "$PROFILE" && "$PROFILE" != "none" ]] && ARGS+=(--profile "$PROFILE")
[[ -n "${ACME_EMAIL:-}" ]] && ARGS+=(--email "$ACME_EMAIL")
[[ -n "${ACME_EAB_KID:-}" ]] && ARGS+=(--eab --eab.kid "$ACME_EAB_KID" --eab.hmac "$ACME_EAB_HMAC")

exec 9>/run/lock/acme-http01.lock
flock -w 600 9
trap 'docker rm -f sm-lego >/dev/null 2>&1 || true' EXIT
# lego `run` obtains the certificate, or renews it when due.
timeout 240 docker run --rm --name sm-lego -p 80:80 -v "$PWD/$CERTS:/certs" goacme/lego:latest run \
  "${ARGS[@]}" ${LEGO_EXTRA:-}
flock -u 9

install -o 101 -g 101 -m 0444 "$CERTS/certificates/$ID.crt" "$TLS/fullchain.pem"   # 101 = nginx user
install -o 101 -g 101 -m 0400 "$CERTS/certificates/$ID.key" "$TLS/privkey.pem"
chmod 755 "$TLS"
if docker compose ps --status running --services 2>/dev/null | grep -qx web; then
  docker compose exec -T web nginx -s reload
fi
echo "[$(date -u +%FT%TZ)] certificate valid until: $(openssl x509 -in "$TLS/fullchain.pem" -noout -enddate)"
