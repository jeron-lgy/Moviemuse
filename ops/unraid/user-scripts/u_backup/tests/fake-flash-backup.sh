#!/usr/bin/env bash

set -Eeuo pipefail

: "${SOURCE_DIR:?SOURCE_DIR is required}"
: "${DOCROOT:?DOCROOT is required}"

backup_name="${FAKE_BACKUP_NAME:-test-v1.0-boot-backup-20260723-1400.zip}"
fixture="$SOURCE_DIR/fixture.txt"

if [[ -n "${FAKE_MARKER:-}" ]]; then
  : > "$FAKE_MARKER"
fi

printf 'u_backup fixture\n' > "$fixture"
(
  cd "$SOURCE_DIR"
  zip -q "$backup_name" "$(basename -- "$fixture")"
)
rm -- "$fixture"
ln -s "$SOURCE_DIR/$backup_name" "$DOCROOT/$backup_name"
printf '%s' "$backup_name"
