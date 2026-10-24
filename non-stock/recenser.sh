#!/usr/bin/env bash
# Point d'entrée : recensement non-stock (hors image Linux de base) + snapshot intégral.
# Aucune exclusion de contenu. Lancer via sudo pour lire tout le FS.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
exec sudo -E python3 "$ROOT/recenser.py" "$@"
