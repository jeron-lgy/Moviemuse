#!/usr/bin/env bash
#
# Read-only summary for MovieMuse monitoring JSONL samples.

set -u

readonly DEFAULT_DATA_DIR="/mnt/user/appdata/moviemuse/monitoring-data"
PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
if [[ "${MOVIEMUSE_MONITOR_TEST_MODE:-0}" == "1" &&
    "${MOVIEMUSE_MONITOR_TEST_BIN_DIR:-}" == /* &&
    "${MOVIEMUSE_MONITOR_TEST_BIN_DIR:-}" != *$'\n'* &&
    "${MOVIEMUSE_MONITOR_TEST_BIN_DIR:-}" != *$'\r'* ]]; then
    PATH="${MOVIEMUSE_MONITOR_TEST_BIN_DIR}:$PATH"
fi
export PATH

DATA_DIR="$DEFAULT_DATA_DIR"
HOURS=24
OUTPUT_JSON=0
READ_STDIN=0

usage() {
    cat <<'EOF'
Usage:
  summarize-moviemuse-health.sh [--hours N] [--json]
  summarize-moviemuse-health.sh --data-dir ABSOLUTE_PATH [--hours N] [--json]
  summarize-moviemuse-health.sh --stdin [--hours N] [--json]

The summarizer is read-only. By default it reads:
  /mnt/user/appdata/moviemuse/monitoring-data/samples/
EOF
}

while (($# > 0)); do
    case "$1" in
        --hours)
            (($# >= 2)) || {
                usage >&2
                exit 2
            }
            HOURS="$2"
            shift 2
            ;;
        --data-dir)
            (($# >= 2)) || {
                usage >&2
                exit 2
            }
            DATA_DIR="${2%/}"
            shift 2
            ;;
        --json)
            OUTPUT_JSON=1
            shift
            ;;
        --stdin)
            READ_STDIN=1
            shift
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            printf 'Unknown argument: %s\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

missing_commands=()
for required_command in cat date find jq sort; do
    command -v "$required_command" >/dev/null 2>&1 || missing_commands+=("$required_command")
done
if ((${#missing_commands[@]} > 0)); then
    printf 'Missing required commands:' >&2
    printf ' %s' "${missing_commands[@]}" >&2
    printf '\nNo summary was produced.\n' >&2
    exit 2
fi

[[ "$HOURS" =~ ^[1-9][0-9]*$ ]] || {
    printf -- '--hours must be a positive integer.\n' >&2
    exit 2
}
((HOURS <= 24 * 31)) || {
    printf -- '--hours cannot exceed 744.\n' >&2
    exit 2
}

CUTOFF_EPOCH=$(($(date +%s) - HOURS * 3600))

read -r -d '' REDUCE_FILTER <<'JQ' || true
def maximum($left; $right):
    if ($right | type) != "number" then $left
    elif ($left | type) != "number" or $right > $left then $right
    else $left
    end;
def minimum($left; $right):
    if ($right | type) != "number" then $left
    elif ($left | type) != "number" or $right < $left then $right
    else $left
    end;
def add_alerts($counts; $alerts):
    reduce ($alerts // [])[] as $alert (
        $counts;
        .[$alert.code] = ((.[$alert.code] // 0) + 1)
    );
def add_severities($counts; $alerts):
    reduce ($alerts // [])[] as $alert (
        $counts;
        .[$alert.severity] = ((.[$alert.severity] // 0) + 1)
    );
reduce inputs as $line (
    {
        requested_hours:$hours,
        cutoff_epoch:$cutoff,
        valid_samples:0,
        invalid_lines:0,
        first_epoch:null,
        last_epoch:null,
        first_timestamp:null,
        last_timestamp:null,
        duration_ms:{max:null,total:0},
        moviemuse:{
            first_memory_current_bytes:null,
            last_memory_current_bytes:null,
            max_memory_current_bytes:null,
            max_anon_bytes:null,
            max_python_rss_bytes:null,
            identity_changes:0,
            memcg_oom_events:0,
            health_failures:0,
            max_health_failure_streak:0,
            local_model_hint_samples:0
        },
        flaresolverr:{
            max_memory_current_bytes:null,
            max_chromium_count:null,
            max_session_count:null,
            session_transitions:0,
            orphan_alert_samples:0,
            memcg_oom_events:0
        },
        database:{
            first_main_bytes:null,
            last_main_bytes:null,
            max_wal_bytes:null,
            latest_event_count:null,
            max_worker_offline_last_hour:null,
            max_worker_offline_payload_last_hour_bytes:null,
            max_waiting_worker_count:null,
            quick_check_failures:0
        },
        compute_worker:{offline_samples:0,online_samples:0,unknown_samples:0},
        alert_counts:{},
        severity_counts:{}
    };
    ($line | fromjson?) as $sample |
    if $sample == null then
        .invalid_lines += 1
    elif ($sample.epoch | type) != "number" or $sample.epoch < $cutoff then
        .
    else
        .valid_samples += 1 |
        .first_epoch = minimum(.first_epoch; $sample.epoch) |
        .last_epoch = maximum(.last_epoch; $sample.epoch) |
        if .first_epoch == $sample.epoch then
            .first_timestamp = $sample.timestamp |
            .moviemuse.first_memory_current_bytes = $sample.moviemuse.cgroup.memory_current_bytes |
            .database.first_main_bytes = $sample.database.files.main.bytes
        else . end |
        if .last_epoch == $sample.epoch then
            .last_timestamp = $sample.timestamp |
            .moviemuse.last_memory_current_bytes = $sample.moviemuse.cgroup.memory_current_bytes |
            .database.last_main_bytes = $sample.database.files.main.bytes |
            if ($sample.database.event_count | type) == "number" then
                .database.latest_event_count = $sample.database.event_count
            else . end
        else . end |
        .duration_ms.max = maximum(.duration_ms.max; $sample.sample.duration_ms) |
        .duration_ms.total += ($sample.sample.duration_ms // 0) |
        .moviemuse.max_memory_current_bytes =
            maximum(.moviemuse.max_memory_current_bytes; $sample.moviemuse.cgroup.memory_current_bytes) |
        .moviemuse.max_anon_bytes =
            maximum(.moviemuse.max_anon_bytes; $sample.moviemuse.cgroup.stat.anon_bytes) |
        .moviemuse.max_python_rss_bytes =
            maximum(.moviemuse.max_python_rss_bytes; $sample.moviemuse.processes.python_rss_bytes) |
        .moviemuse.health_failures += (if $sample.moviemuse.health.ok == false then 1 else 0 end) |
        .moviemuse.max_health_failure_streak =
            maximum(.moviemuse.max_health_failure_streak; $sample.monitor_state.health_failure_streak) |
        .moviemuse.local_model_hint_samples +=
            (if $sample.moviemuse.processes.heavy_model_hint == true then 1 else 0 end) |
        .flaresolverr.max_memory_current_bytes =
            maximum(.flaresolverr.max_memory_current_bytes;
                $sample.flaresolverr.container.cgroup.memory_current_bytes) |
        .flaresolverr.max_chromium_count =
            maximum(.flaresolverr.max_chromium_count;
                $sample.flaresolverr.container.processes.chromium_count) |
        .flaresolverr.max_session_count =
            maximum(.flaresolverr.max_session_count; $sample.flaresolverr.sessions.session_count) |
        .database.max_wal_bytes =
            maximum(.database.max_wal_bytes; $sample.database.files.wal.bytes) |
        .database.max_worker_offline_last_hour =
            maximum(.database.max_worker_offline_last_hour; $sample.database.worker_offline_last_hour) |
        .database.max_worker_offline_payload_last_hour_bytes =
            maximum(.database.max_worker_offline_payload_last_hour_bytes;
                $sample.database.worker_offline_payload_max_last_hour_bytes) |
        .database.max_waiting_worker_count =
            maximum(.database.max_waiting_worker_count; $sample.database.waiting_worker_count) |
        .database.quick_check_failures +=
            (if $sample.database.quick_check.attempted == true
                and $sample.database.quick_check.status != "ok" then 1 else 0 end) |
        .compute_worker.online_samples +=
            (if $sample.compute_worker.online == true then 1 else 0 end) |
        .compute_worker.offline_samples +=
            (if $sample.compute_worker.online == false then 1 else 0 end) |
        .compute_worker.unknown_samples +=
            (if $sample.compute_worker.online == null then 1 else 0 end) |
        .moviemuse.identity_changes +=
            ([$sample.events[]? | select(.kind == "moviemuse_identity_change")] | length) |
        .moviemuse.memcg_oom_events +=
            ([$sample.events[]? | select(.kind == "moviemuse_memcg_oom_kill")] | length) |
        .flaresolverr.session_transitions +=
            ([$sample.events[]? | select(.kind == "flare_sessions_changed")] | length) |
        .flaresolverr.orphan_alert_samples +=
            ([$sample.alerts[]? | select(.code == "flare_orphan_chromium_suspected")] | length) |
        .flaresolverr.memcg_oom_events +=
            ([$sample.alerts[]? | select(.code == "flaresolverr_memcg_oom_increment")] | length) |
        .alert_counts = add_alerts(.alert_counts; $sample.alerts) |
        .severity_counts = add_severities(.severity_counts; $sample.alerts)
    end
) |
.duration_ms.average = (
    if .valid_samples > 0 then ((.duration_ms.total / .valid_samples) * 10 | round) / 10
    else null end
) |
.moviemuse.memory_change_bytes = (
    if (.moviemuse.first_memory_current_bytes | type) == "number"
        and (.moviemuse.last_memory_current_bytes | type) == "number" then
        .moviemuse.last_memory_current_bytes - .moviemuse.first_memory_current_bytes
    else null end
) |
.database.main_change_bytes = (
    if (.database.first_main_bytes | type) == "number"
        and (.database.last_main_bytes | type) == "number" then
        .database.last_main_bytes - .database.first_main_bytes
    else null end
) |
del(.duration_ms.total)
JQ

if [[ "$READ_STDIN" == "1" ]]; then
    SUMMARY="$(jq -R -n --argjson cutoff "$CUTOFF_EPOCH" --argjson hours "$HOURS" "$REDUCE_FILTER")"
else
    [[ "$DATA_DIR" == /* && "$DATA_DIR" != "/" && "$DATA_DIR" != *$'\n'* && "$DATA_DIR" != *$'\r'* ]] || {
        printf 'Data directory must be a safe absolute path.\n' >&2
        exit 2
    }
    SAMPLES_DIR="$DATA_DIR/samples"
    [[ -d "$SAMPLES_DIR" && ! -L "$SAMPLES_DIR" ]] || {
        printf 'Sample directory is missing or is a symlink: %s\n' "$SAMPLES_DIR" >&2
        exit 1
    }
    mapfile -d '' SAMPLE_FILES < <(
        find "$SAMPLES_DIR" -maxdepth 1 -type f -name 'health-????-??-??.jsonl' -print0 |
            sort -z
    )
    ((${#SAMPLE_FILES[@]} > 0)) || {
        printf 'No monitoring samples found in %s\n' "$SAMPLES_DIR" >&2
        exit 1
    }
    SUMMARY="$(
        jq -R -n --argjson cutoff "$CUTOFF_EPOCH" --argjson hours "$HOURS" \
            "$REDUCE_FILTER" "${SAMPLE_FILES[@]}"
    )"
fi

if [[ "$OUTPUT_JSON" == "1" ]]; then
    printf '%s\n' "$SUMMARY"
    exit 0
fi

jq -r '
    def mib($value):
        if ($value | type) != "number" then "unknown"
        else (((($value / 1048576) * 10 | round) / 10) | tostring) + " MiB"
        end;
    def signed_mib($value):
        if ($value | type) != "number" then "unknown"
        else
            (if $value > 0 then "+" else "" end) +
            (((($value / 1048576) * 10 | round) / 10) | tostring) + " MiB"
        end;
    def counts($value):
        if ($value | length) == 0 then "none"
        else ($value | to_entries | sort_by(.key) | map("\(.key)=\(.value)") | join(", "))
        end;
    [
        "MovieMuse monitoring summary (\(.requested_hours)h)",
        "Window: \(.first_timestamp // "no samples") -> \(.last_timestamp // "no samples")",
        "Samples: valid=\(.valid_samples), invalid=\(.invalid_lines), collector avg=\(.duration_ms.average // "unknown")ms, max=\(.duration_ms.max // "unknown")ms",
        "",
        "MovieMuse:",
        "  memory first/last/max: \(mib(.moviemuse.first_memory_current_bytes)) / \(mib(.moviemuse.last_memory_current_bytes)) / \(mib(.moviemuse.max_memory_current_bytes))",
        "  memory change: \(signed_mib(.moviemuse.memory_change_bytes)); max anon: \(mib(.moviemuse.max_anon_bytes)); max Python RSS: \(mib(.moviemuse.max_python_rss_bytes))",
        "  health failures=\(.moviemuse.health_failures), max failure streak=\(.moviemuse.max_health_failure_streak), identity changes=\(.moviemuse.identity_changes), memcg OOM events=\(.moviemuse.memcg_oom_events)",
        "",
        "FlareSolverr:",
        "  max memory=\(mib(.flaresolverr.max_memory_current_bytes)), max sessions=\(.flaresolverr.max_session_count // "unknown"), max Chromium processes=\(.flaresolverr.max_chromium_count // "unknown")",
        "  session transitions=\(.flaresolverr.session_transitions), orphan alert samples=\(.flaresolverr.orphan_alert_samples), memcg OOM alert samples=\(.flaresolverr.memcg_oom_events)",
        "",
        "SQLite / worker:",
        "  database first/last/change: \(mib(.database.first_main_bytes)) / \(mib(.database.last_main_bytes)) / \(signed_mib(.database.main_change_bytes))",
        "  max WAL=\(mib(.database.max_wal_bytes)), latest events=\(.database.latest_event_count // "unknown"), max worker_offline/hour=\(.database.max_worker_offline_last_hour // "unknown"), max new payload=\(.database.max_worker_offline_payload_last_hour_bytes // "unknown") bytes",
        "  max waiting_worker=\(.database.max_waiting_worker_count // "unknown"), quick_check failures=\(.database.quick_check_failures)",
        "  worker online/offline/unknown samples=\(.compute_worker.online_samples)/\(.compute_worker.offline_samples)/\(.compute_worker.unknown_samples)",
        "",
        "Alerts by severity: \(counts(.severity_counts))",
        "Alerts by code: \(counts(.alert_counts))"
    ] | .[]
' <<<"$SUMMARY"
