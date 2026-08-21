#!/usr/bin/env bash

set -Eeuo pipefail
umask 022

PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PATH

DEST_DIR="${DEST_DIR:-/mnt/user/backup/u_pan/flash-backups}"
SOURCE_DIR="${SOURCE_DIR:-/}"
DOCROOT="${DOCROOT:-/usr/local/emhttp}"
FLASH_BACKUP_COMMAND="${FLASH_BACKUP_COMMAND:-/usr/local/emhttp/webGui/scripts/flash_backup}"
LOCK_FILE="${LOCK_FILE:-/var/lock/u_backup.lock}"
MEMINFO_FILE="${MEMINFO_FILE:-/proc/meminfo}"
RETENTION_DAYS="${RETENTION_DAYS:-15}"
SOURCE_RESERVE_BYTES="${SOURCE_RESERVE_BYTES:-536870912}"
DEST_RESERVE_BYTES="${DEST_RESERVE_BYTES:-536870912}"
MIN_SOURCE_FREE_BYTES="${MIN_SOURCE_FREE_BYTES:-}"
ALLOW_TEST_PATHS="${ALLOW_TEST_PATHS:-0}"

log() {
  printf '%s [%s] %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$1" "$2"
}

die() {
  log ERROR "$1" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "Required command is unavailable: $1"
}

for required in awk basename chmod cp cut date df dirname find flock install \
  mv readlink rm sha256sum stat sync tr unzip; do
  require_command "$required"
done

[[ "$ALLOW_TEST_PATHS" == "0" || "$ALLOW_TEST_PATHS" == "1" ]] \
  || die "ALLOW_TEST_PATHS must be 0 or 1"
[[ "$RETENTION_DAYS" =~ ^[0-9]+$ ]] || die "RETENTION_DAYS must be a non-negative integer"
[[ "$SOURCE_RESERVE_BYTES" =~ ^[0-9]+$ ]] || die "SOURCE_RESERVE_BYTES must be a non-negative integer"
[[ "$DEST_RESERVE_BYTES" =~ ^[0-9]+$ ]] || die "DEST_RESERVE_BYTES must be a non-negative integer"
if [[ -n "$MIN_SOURCE_FREE_BYTES" ]]; then
  [[ "$MIN_SOURCE_FREE_BYTES" =~ ^[0-9]+$ ]] \
    || die "MIN_SOURCE_FREE_BYTES must be empty or a non-negative integer"
fi

if [[ "$ALLOW_TEST_PATHS" == "0" ]]; then
  [[ "$DEST_DIR" == "/mnt/user/backup/u_pan/flash-backups" ]] \
    || die "Production DEST_DIR must be /mnt/user/backup/u_pan/flash-backups"
  [[ "$SOURCE_DIR" == "/" ]] || die "Production SOURCE_DIR must be /"
  [[ "$DOCROOT" == "/usr/local/emhttp" ]] \
    || die "Production DOCROOT must be /usr/local/emhttp"
  [[ "$FLASH_BACKUP_COMMAND" == "/usr/local/emhttp/webGui/scripts/flash_backup" ]] \
    || die "Production FLASH_BACKUP_COMMAND is fixed"
  [[ "$LOCK_FILE" == "/var/lock/u_backup.lock" ]] \
    || die "Production LOCK_FILE must be /var/lock/u_backup.lock"
  [[ "$MEMINFO_FILE" == "/proc/meminfo" ]] \
    || die "Production MEMINFO_FILE must be /proc/meminfo"
fi

[[ -d "$SOURCE_DIR" ]] || die "Source directory does not exist: $SOURCE_DIR"
[[ -d "$DOCROOT" ]] || die "Unraid document root does not exist: $DOCROOT"
[[ -x "$FLASH_BACKUP_COMMAND" ]] || die "flash_backup is not executable: $FLASH_BACKUP_COMMAND"
[[ -r "$MEMINFO_FILE" ]] || die "Memory information is not readable: $MEMINFO_FILE"

install -d -m 0755 "$DEST_DIR"
SOURCE_DIR="$(readlink -f -- "$SOURCE_DIR")"
DEST_DIR="$(readlink -f -- "$DEST_DIR")"
DOCROOT="$(readlink -f -- "$DOCROOT")"

if [[ "$ALLOW_TEST_PATHS" == "0" ]]; then
  [[ "$SOURCE_DIR" == "/" ]] || die "Resolved SOURCE_DIR escaped /"
  [[ "$DEST_DIR" == "/mnt/user/backup/u_pan/flash-backups" ]] \
    || die "Resolved DEST_DIR escaped the approved backup directory"
  [[ "$DOCROOT" == "/usr/local/emhttp" ]] || die "Resolved DOCROOT changed unexpectedly"
fi

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  die "Another u_backup process is already running"
fi

mapfile -d '' -t preexisting_archives < <(
  find "$SOURCE_DIR" -maxdepth 1 -type f -name '*-boot-backup-*.zip' -print0
)
if (( ${#preexisting_archives[@]} > 0 )); then
  for archive in "${preexisting_archives[@]}"; do
    log ERROR "Pre-existing source archive requires manual review: $archive"
  done
  die "Refusing to create another backup while a previous source archive remains"
fi

source_available="$(df -PB1 "$SOURCE_DIR" | awk 'END {print $4}')"
[[ "$source_available" =~ ^[0-9]+$ ]] || die "Could not determine source filesystem free space"

if [[ -n "$MIN_SOURCE_FREE_BYTES" ]]; then
  source_required="$MIN_SOURCE_FREE_BYTES"
else
  boot_used="$(df -PB1 /boot | awk 'END {print $3}')"
  [[ "$boot_used" =~ ^[0-9]+$ ]] || die "Could not determine /boot usage"
  source_required=$(( boot_used * 2 + SOURCE_RESERVE_BYTES ))
fi

if (( source_available < source_required )); then
  die "Insufficient rootfs space: available=$source_available required=$source_required"
fi

host_available_kb="$(awk '/^MemAvailable:/ {print $2; exit}' "$MEMINFO_FILE")"
[[ "$host_available_kb" =~ ^[0-9]+$ ]] || die "Could not determine host MemAvailable"
host_available_bytes=$(( host_available_kb * 1024 ))
if (( host_available_bytes < source_required )); then
  die "Insufficient host memory: available=$host_available_bytes required=$source_required"
fi

log INFO "Preflight passed: rootfs_available=$source_available host_available=$host_available_bytes required=$source_required"
log INFO "Generating Unraid boot backup"
backup_output="$("$FLASH_BACKUP_COMMAND")" || die "Unraid flash_backup command failed"
backup_name="$(printf '%s' "$backup_output" | tr -d '\r\n')"

[[ "$backup_name" =~ ^[A-Za-z0-9._-]+-boot-backup-[0-9]{8}-[0-9]{4}\.zip$ ]] \
  || die "flash_backup returned an unexpected filename"

source_link="$DOCROOT/$backup_name"
[[ -L "$source_link" ]] || die "Expected flash_backup symlink is missing: $source_link"
raw_link_target="$(readlink -- "$source_link")"
source_archive="$(readlink -f -- "$source_link")"

[[ -f "$source_archive" && ! -L "$source_archive" ]] \
  || die "Generated backup target is not a regular file"
[[ "$(dirname -- "$source_archive")" == "$SOURCE_DIR" ]] \
  || die "Generated backup escaped the approved source directory: $source_archive"
[[ "$(basename -- "$source_archive")" == "$backup_name" ]] \
  || die "Generated backup filename and symlink do not match"

unzip -tq "$source_archive" >/dev/null \
  || die "Generated source backup failed ZIP integrity validation"

source_size="$(stat -c %s "$source_archive")"
[[ "$source_size" =~ ^[1-9][0-9]*$ ]] || die "Generated source backup is empty"
dest_available="$(df -PB1 "$DEST_DIR" | awk 'END {print $4}')"
[[ "$dest_available" =~ ^[0-9]+$ ]] || die "Could not determine destination free space"
dest_required=$(( source_size + DEST_RESERVE_BYTES ))
if (( dest_available < dest_required )); then
  die "Insufficient destination space: available=$dest_available required=$dest_required"
fi

final_archive="$DEST_DIR/$backup_name"
partial_archive="$DEST_DIR/.${backup_name}.partial.$$"
partial_checksum="$DEST_DIR/.${backup_name}.sha256.partial.$$"

cleanup_temporary_files() {
  status=$?
  if [[ -n "${partial_archive:-}" && -f "$partial_archive" && ! -L "$partial_archive" ]]; then
    rm -- "$partial_archive"
  fi
  if [[ -n "${partial_checksum:-}" && -f "$partial_checksum" && ! -L "$partial_checksum" ]]; then
    rm -- "$partial_checksum"
  fi
  if (( status != 0 )); then
    log ERROR "Backup did not complete; generated source was preserved for manual review"
  fi
  exit "$status"
}
trap cleanup_temporary_files EXIT

source_sha="$(sha256sum "$source_archive" | cut -c1-64)"
if [[ -e "$final_archive" ]]; then
  [[ -f "$final_archive" && ! -L "$final_archive" ]] \
    || die "Destination collision is not a regular file: $final_archive"
  final_sha="$(sha256sum "$final_archive" | cut -c1-64)"
  [[ "$source_sha" == "$final_sha" ]] \
    || die "Destination filename collision has different content: $final_archive"
  unzip -tq "$final_archive" >/dev/null \
    || die "Existing destination archive failed ZIP integrity validation"
  log INFO "Identical validated destination archive already exists"
else
  cp --preserve=timestamps -- "$source_archive" "$partial_archive"
  partial_sha="$(sha256sum "$partial_archive" | cut -c1-64)"
  [[ "$source_sha" == "$partial_sha" ]] \
    || die "Source and destination SHA-256 values differ"
  unzip -tq "$partial_archive" >/dev/null \
    || die "Destination copy failed ZIP integrity validation"
  chmod 0644 "$partial_archive"
  mv -- "$partial_archive" "$final_archive"
fi

(
  cd "$DEST_DIR"
  sha256sum "$backup_name" > "$(basename -- "$partial_checksum")"
)
mv -- "$partial_checksum" "$final_archive.sha256"
sync -f "$final_archive"
(
  cd "$DEST_DIR"
  sha256sum -c "$backup_name.sha256" >/dev/null
) || die "Published checksum verification failed"

[[ -L "$source_link" ]] || die "flash_backup symlink changed before cleanup"
[[ "$(readlink -- "$source_link")" == "$raw_link_target" ]] \
  || die "flash_backup symlink target changed before cleanup"
rm -- "$source_archive"
rm -- "$source_link"

while IFS= read -r -d '' old_archive; do
  [[ "$old_archive" != "$final_archive" ]] || continue
  old_resolved="$(readlink -f -- "$old_archive")"
  [[ "$(dirname -- "$old_resolved")" == "$DEST_DIR" ]] \
    || die "Retention candidate escaped destination directory: $old_archive"
  [[ "$(basename -- "$old_resolved")" =~ ^[A-Za-z0-9._-]+-boot-backup-[0-9]{8}-[0-9]{4}\.zip$ ]] \
    || die "Retention candidate has an unexpected filename: $old_archive"
  rm -- "$old_archive"
  if [[ -f "$old_archive.sha256" && ! -L "$old_archive.sha256" ]]; then
    rm -- "$old_archive.sha256"
  fi
  log INFO "Removed expired backup: $old_archive"
done < <(
  find "$DEST_DIR" -maxdepth 1 -type f -name '*-boot-backup-*.zip' \
    -mtime "+$RETENTION_DAYS" -print0
)

sync -f "$DEST_DIR"
trap - EXIT
log INFO "Boot backup completed: path=$final_archive size_bytes=$source_size sha256=$source_sha"
