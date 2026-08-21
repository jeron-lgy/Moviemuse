#!/usr/bin/env bash

set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
subject="$script_dir/../u_backup.sh"
fake_source="$script_dir/fake-flash-backup.sh"
work="$(mktemp -d /tmp/u-backup-test.XXXXXX)"

cleanup() {
  case "$work" in
    /tmp/u-backup-test.*) rm -rf -- "$work" ;;
    *) printf 'Refusing to clean unexpected test path: %s\n' "$work" >&2 ;;
  esac
}
trap cleanup EXIT

source_dir="$work/source"
dest_dir="$work/destination"
docroot="$work/docroot"
lock_file="$work/u_backup.lock"
fake_command="$work/fake-flash-backup.sh"
marker="$work/fake-command-ran"
low_meminfo="$work/low-meminfo"
backup_name="test-v1.0-boot-backup-20260723-1400.zip"

mkdir -p "$source_dir" "$dest_dir" "$docroot"
cp -- "$fake_source" "$fake_command"
chmod 0755 "$fake_command"

printf 'expired fixture\n' > "$dest_dir/expired.txt"
(
  cd "$dest_dir"
  zip -q "test-v1.0-boot-backup-20260601-0000.zip" "expired.txt"
)
rm -- "$dest_dir/expired.txt"
touch -d '20 days ago' "$dest_dir/test-v1.0-boot-backup-20260601-0000.zip"

env \
  ALLOW_TEST_PATHS=1 \
  DEST_DIR="$dest_dir" \
  SOURCE_DIR="$source_dir" \
  DOCROOT="$docroot" \
  FLASH_BACKUP_COMMAND="$fake_command" \
  LOCK_FILE="$lock_file" \
  MIN_SOURCE_FREE_BYTES=1 \
  DEST_RESERVE_BYTES=1 \
  RETENTION_DAYS=15 \
  bash "$subject"

test -f "$dest_dir/$backup_name"
test -f "$dest_dir/$backup_name.sha256"
(
  cd "$dest_dir"
  sha256sum -c "$backup_name.sha256" >/dev/null
)
unzip -tq "$dest_dir/$backup_name" >/dev/null
test ! -e "$source_dir/$backup_name"
test ! -L "$docroot/$backup_name"
test ! -e "$dest_dir/test-v1.0-boot-backup-20260601-0000.zip"

printf 'pre-existing residue\n' > "$source_dir/test-v1.0-boot-backup-20260722-0000.zip"
if env \
  ALLOW_TEST_PATHS=1 \
  DEST_DIR="$dest_dir" \
  SOURCE_DIR="$source_dir" \
  DOCROOT="$docroot" \
  FLASH_BACKUP_COMMAND="$fake_command" \
  LOCK_FILE="$lock_file" \
  MIN_SOURCE_FREE_BYTES=1 \
  DEST_RESERVE_BYTES=1 \
  RETENTION_DAYS=15 \
  FAKE_MARKER="$marker" \
  FAKE_BACKUP_NAME="test-v1.0-boot-backup-20260723-1401.zip" \
  bash "$subject"; then
  printf 'Expected pre-existing source archive guard to fail\n' >&2
  exit 1
fi

test ! -e "$marker"
test -f "$source_dir/test-v1.0-boot-backup-20260722-0000.zip"
rm -- "$source_dir/test-v1.0-boot-backup-20260722-0000.zip"

printf 'MemAvailable: 0 kB\n' > "$low_meminfo"
if env \
  ALLOW_TEST_PATHS=1 \
  DEST_DIR="$dest_dir" \
  SOURCE_DIR="$source_dir" \
  DOCROOT="$docroot" \
  FLASH_BACKUP_COMMAND="$fake_command" \
  LOCK_FILE="$lock_file" \
  MEMINFO_FILE="$low_meminfo" \
  MIN_SOURCE_FREE_BYTES=1 \
  DEST_RESERVE_BYTES=1 \
  RETENTION_DAYS=15 \
  FAKE_MARKER="$marker" \
  FAKE_BACKUP_NAME="test-v1.0-boot-backup-20260723-1402.zip" \
  bash "$subject"; then
  printf 'Expected low host memory guard to fail\n' >&2
  exit 1
fi

test ! -e "$marker"

printf 'u_backup behavior tests passed\n'
