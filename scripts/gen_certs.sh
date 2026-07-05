#!/usr/bin/env bash
set -euo pipefail
mkdir -p certs
openssl req -x509 -newkey rsa:4096 -keyout certs/sim.key -out certs/sim.crt \
  -days 3650 -nodes \
  -subj "/CN=localhost" \
  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
echo "Certificates generated in certs/"
