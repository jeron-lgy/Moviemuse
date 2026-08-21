#!/usr/bin/env bash
#
# MovieMuse temporary Unraid host monitor.
#
# This script observes the Unraid host, libvirt VMs, Docker cgroups, MovieMuse,
# FlareSolverr and the subscription SQLite database. It never restarts workloads,
# destroys sessions or writes to the application data directory.

set -u
umask 077

readonly SCHEMA_VERSION="2"
readonly DEFAULT_DATA_DIR="/mnt/user/appdata/moviemuse/monitoring-data"
readonly DEFAULT_APP_DATA_DIR="/mnt/user/appdata/moviemuse/data"
readonly DEFAULT_MOVIEMUSE_CONTAINER="moviemuse"
readonly DEFAULT_FLARE_CONTAINER="flaresolverr"
readonly DEFAULT_HEALTH_URL="http://127.0.0.1:18188/health"
readonly DEFAULT_FLARE_URL="http://127.0.0.1:8191/v1"

PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
if [[ "${MOVIEMUSE_MONITOR_TEST_MODE:-0}" == "1" &&
    "${MOVIEMUSE_MONITOR_TEST_BIN_DIR:-}" == /* &&
    "${MOVIEMUSE_MONITOR_TEST_BIN_DIR:-}" != *$'\n'* &&
    "${MOVIEMUSE_MONITOR_TEST_BIN_DIR:-}" != *$'\r'* ]]; then
    PATH="${MOVIEMUSE_MONITOR_TEST_BIN_DIR}:$PATH"
fi
export PATH

MODE="collect"
case "${1:-}" in
    "")
        ;;
    --probe)
        MODE="probe"
        ;;
    --capabilities)
        MODE="capabilities"
        ;;
    --help|-h)
        cat <<'EOF'
Usage:
  collect-moviemuse-health.sh                 Collect one sample.
  collect-moviemuse-health.sh --probe         Print one read-only sample; write nothing.
  collect-moviemuse-health.sh --capabilities  Check required host commands.

Production writes are restricted to:
  /mnt/user/appdata/moviemuse/monitoring-data

See README.md in this directory for supported environment variables.
EOF
        exit 0
        ;;
    *)
        printf 'Unknown argument: %s\n' "$1" >&2
        exit 2
        ;;
esac

required_commands=(
    awk basename cat cksum curl date df docker find flock grep head hostname jq mkdir mv
    ps readlink rm sed sort sqlite3 stat tail tr wc
)

missing_commands=()
for required_command in "${required_commands[@]}"; do
    if ! command -v "$required_command" >/dev/null 2>&1; then
        missing_commands+=("$required_command")
    fi
done

if [[ "$MODE" == "capabilities" ]]; then
    for required_command in "${required_commands[@]}"; do
        if command -v "$required_command" >/dev/null 2>&1; then
            printf '%s=yes:%s\n' "$required_command" "$(command -v "$required_command")"
        else
            printf '%s=no\n' "$required_command"
        fi
    done
    if command -v python3 >/dev/null 2>&1; then
        printf 'python3=available-but-not-used:%s\n' "$(command -v python3)"
    else
        printf 'python3=not-required\n'
    fi
    if command -v virsh >/dev/null 2>&1; then
        printf 'virsh=optional-available:%s\n' "$(command -v virsh)"
    else
        printf 'virsh=optional-unavailable\n'
    fi
    [[ -r /proc/pressure/memory ]] && printf 'memory_psi=available\n' || printf 'memory_psi=unavailable\n'
    [[ -r /boot/logs/syslog ]] && printf 'persistent_syslog=available:/boot/logs/syslog\n' || printf 'persistent_syslog=unavailable\n'
    ((${#missing_commands[@]} == 0))
    exit $?
fi

if ((${#missing_commands[@]} > 0)); then
    printf 'Missing required commands:' >&2
    printf ' %s' "${missing_commands[@]}" >&2
    printf '\nNo sample was written.\n' >&2
    exit 2
fi

DATA_DIR="${MOVIEMUSE_MONITOR_DATA_DIR:-$DEFAULT_DATA_DIR}"
APP_DATA_DIR="${MOVIEMUSE_APP_DATA_DIR:-$DEFAULT_APP_DATA_DIR}"
MOVIEMUSE_CONTAINER="${MOVIEMUSE_CONTAINER_NAME:-$DEFAULT_MOVIEMUSE_CONTAINER}"
FLARE_CONTAINER="${MOVIEMUSE_FLARE_CONTAINER_NAME:-$DEFAULT_FLARE_CONTAINER}"
HEALTH_URL="${MOVIEMUSE_HEALTH_URL:-$DEFAULT_HEALTH_URL}"
FLARE_URL="${MOVIEMUSE_FLARE_URL:-$DEFAULT_FLARE_URL}"
TEST_MODE="${MOVIEMUSE_MONITOR_TEST_MODE:-0}"

fail() {
    printf 'MovieMuse monitor: %s\n' "$*" >&2
    exit 2
}

valid_absolute_path() {
    local value="$1"
    [[ "$value" == /* ]] || return 1
    [[ "$value" != "/" ]] || return 1
    [[ "$value" != *$'\n'* && "$value" != *$'\r'* ]] || return 1
}

valid_container_name() {
    [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]
}

valid_absolute_path "$APP_DATA_DIR" || fail "APP_DATA_DIR must be a safe absolute path"
valid_container_name "$MOVIEMUSE_CONTAINER" || fail "invalid MovieMuse container name"
valid_container_name "$FLARE_CONTAINER" || fail "invalid FlareSolverr container name"

DATA_DIR="${DATA_DIR%/}"
if [[ "$MODE" == "collect" ]]; then
    valid_absolute_path "$DATA_DIR" || fail "monitoring data directory must be a safe absolute path"
    if [[ "$TEST_MODE" != "1" && "$DATA_DIR" != "$DEFAULT_DATA_DIR" ]]; then
        fail "production writes are restricted to $DEFAULT_DATA_DIR"
    fi
fi

epoch_ms() {
    local value
    value="$(date +%s%3N 2>/dev/null || true)"
    if [[ "$value" =~ ^[0-9]{13}$ ]]; then
        printf '%s\n' "$value"
    else
        printf '%s000\n' "$(date +%s)"
    fi
}

metric_file_json() {
    local path="$1"
    if [[ -f "$path" ]]; then
        jq -nc --argjson bytes "$(stat -c '%s' "$path" 2>/dev/null || printf 'null')" \
            '{exists:true, bytes:$bytes}'
    else
        jq -nc '{exists:false, bytes:null}'
    fi
}

container_identity_json() {
    local container="$1"
    local identity
    identity="$(
        docker inspect "$container" 2>/dev/null |
            jq -c --arg requested_name "$container" '
                if length == 0 then
                    {name:$requested_name, exists:false, running:false, id:null, image:null,
                     image_id:null, started_at:null, restart_count:null, oom_killed:null,
                     health:null, pid:null}
                else
                    .[0] |
                    {
                        name:(.Name | ltrimstr("/")),
                        exists:true,
                        running:(.State.Running // false),
                        id:(.Id // null),
                        image:(.Config.Image // null),
                        image_id:(.Image // null),
                        started_at:(.State.StartedAt // null),
                        restart_count:(.RestartCount // null),
                        oom_killed:(.State.OOMKilled // null),
                        health:(.State.Health.Status // null),
                        pid:(.State.Pid // null)
                    }
                end
            ' 2>/dev/null
    )"
    if ! jq -e 'type == "object"' >/dev/null 2>&1 <<<"$identity"; then
        jq -nc --arg name "$container" \
            '{name:$name, exists:false, running:false, id:null, image:null, image_id:null,
              started_at:null, restart_count:null, oom_killed:null, health:null, pid:null}'
        return
    fi
    printf '%s\n' "$identity"
}

cgroup_json() {
    local pid="$1"
    if [[ ! "$pid" =~ ^[1-9][0-9]*$ || ! -r "/proc/$pid/cgroup" ]]; then
        jq -nc '
            {version:null, path_available:false, memory_current_bytes:null,
             memory_peak_bytes:null, memory_max_bytes:null, swap_current_bytes:null,
             swap_max_bytes:null, events:{low:null,high:null,max:null,oom:null,
             oom_kill:null,oom_group_kill:null}, stat:{anon_bytes:null,file_bytes:null,
             shmem_bytes:null,slab_bytes:null}}
        '
        return
    fi

    local relative_path cgroup_dir version
    relative_path="$(awk -F: '$1 == "0" {print $3; exit}' "/proc/$pid/cgroup" 2>/dev/null)"
    if [[ -n "$relative_path" && -d "/sys/fs/cgroup$relative_path" ]]; then
        version="v2"
        cgroup_dir="/sys/fs/cgroup$relative_path"
    else
        version="unknown"
        cgroup_dir=""
    fi

    local current="" peak="" maximum="" swap_current="" swap_maximum=""
    local event_low="" event_high="" event_max="" event_oom="" event_oom_kill="" event_oom_group=""
    local stat_anon="" stat_file="" stat_shmem="" stat_slab=""
    if [[ -n "$cgroup_dir" ]]; then
        [[ -r "$cgroup_dir/memory.current" ]] && current="$(<"$cgroup_dir/memory.current")"
        [[ -r "$cgroup_dir/memory.peak" ]] && peak="$(<"$cgroup_dir/memory.peak")"
        [[ -r "$cgroup_dir/memory.max" ]] && maximum="$(<"$cgroup_dir/memory.max")"
        [[ -r "$cgroup_dir/memory.swap.current" ]] && swap_current="$(<"$cgroup_dir/memory.swap.current")"
        [[ -r "$cgroup_dir/memory.swap.max" ]] && swap_maximum="$(<"$cgroup_dir/memory.swap.max")"
        if [[ -r "$cgroup_dir/memory.events" ]]; then
            event_low="$(awk '$1 == "low" {print $2}' "$cgroup_dir/memory.events")"
            event_high="$(awk '$1 == "high" {print $2}' "$cgroup_dir/memory.events")"
            event_max="$(awk '$1 == "max" {print $2}' "$cgroup_dir/memory.events")"
            event_oom="$(awk '$1 == "oom" {print $2}' "$cgroup_dir/memory.events")"
            event_oom_kill="$(awk '$1 == "oom_kill" {print $2}' "$cgroup_dir/memory.events")"
            event_oom_group="$(awk '$1 == "oom_group_kill" {print $2}' "$cgroup_dir/memory.events")"
        fi
        if [[ -r "$cgroup_dir/memory.stat" ]]; then
            stat_anon="$(awk '$1 == "anon" {print $2}' "$cgroup_dir/memory.stat")"
            stat_file="$(awk '$1 == "file" {print $2}' "$cgroup_dir/memory.stat")"
            stat_shmem="$(awk '$1 == "shmem" {print $2}' "$cgroup_dir/memory.stat")"
            stat_slab="$(awk '$1 == "slab" {print $2}' "$cgroup_dir/memory.stat")"
        fi
    fi

    jq -nc \
        --arg version "$version" \
        --arg current "$current" --arg peak "$peak" --arg maximum "$maximum" \
        --arg swap_current "$swap_current" --arg swap_maximum "$swap_maximum" \
        --arg event_low "$event_low" --arg event_high "$event_high" \
        --arg event_max "$event_max" --arg event_oom "$event_oom" \
        --arg event_oom_kill "$event_oom_kill" --arg event_oom_group "$event_oom_group" \
        --arg stat_anon "$stat_anon" --arg stat_file "$stat_file" \
        --arg stat_shmem "$stat_shmem" --arg stat_slab "$stat_slab" '
        def metric($value):
            if $value == "" then null
            elif $value == "max" then "max"
            elif ($value | test("^[0-9]+$")) then ($value | tonumber)
            else null
            end;
        {
            version:$version,
            path_available:($version == "v2"),
            memory_current_bytes:metric($current),
            memory_peak_bytes:metric($peak),
            memory_max_bytes:metric($maximum),
            swap_current_bytes:metric($swap_current),
            swap_max_bytes:metric($swap_maximum),
            events:{
                low:metric($event_low),
                high:metric($event_high),
                max:metric($event_max),
                oom:metric($event_oom),
                oom_kill:metric($event_oom_kill),
                oom_group_kill:metric($event_oom_group)
            },
            stat:{
                anon_bytes:metric($stat_anon),
                file_bytes:metric($stat_file),
                shmem_bytes:metric($stat_shmem),
                slab_bytes:metric($stat_slab)
            }
        }
    '
}

process_json() {
    local container="$1"
    local pids
    pids="$(
        docker top "$container" 2>/dev/null |
            awk '
                NR == 1 {
                    for (column = 1; column <= NF; column++) {
                        if (toupper($column) == "PID") {
                            pid_column = column
                        }
                    }
                    next
                }
                pid_column > 0 {print $pid_column}
            '
    )"
    if [[ -z "$pids" ]]; then
        jq -nc '
            {status:"unknown", process_count:null, thread_count:null, rss_bytes:null,
             pids:null, chromium_pids:null,
             python_count:null, python_rss_bytes:null, chromium_count:null,
             chromium_rss_bytes:null, javdb_chromium_count:null,
             javlibrary_chromium_count:null, unclassified_chromium_count:null,
             heavy_model_hint:null}
        '
        return
    fi

    local process_count=0 thread_count=0 rss_kib=0
    local python_count=0 python_rss_kib=0 chromium_count=0 chromium_rss_kib=0
    local javdb_chromium_count=0 javlibrary_chromium_count=0 unclassified_chromium_count=0
    local heavy_model_hint=false
    local pid comm lower_comm rss threads cmdline lower_cmdline is_python
    local pid_csv="" chromium_pid_csv=""

    while IFS= read -r pid; do
        [[ "$pid" =~ ^[1-9][0-9]*$ && -r "/proc/$pid/status" ]] || continue
        comm=""
        IFS= read -r comm <"/proc/$pid/comm" 2>/dev/null || true
        lower_comm="$(tr '[:upper:]' '[:lower:]' <<<"$comm")"
        rss="$(awk '/^VmRSS:/ {print $2; exit}' "/proc/$pid/status" 2>/dev/null)"
        threads="$(awk '/^Threads:/ {print $2; exit}' "/proc/$pid/status" 2>/dev/null)"
        [[ "$rss" =~ ^[0-9]+$ ]] || rss=0
        [[ "$threads" =~ ^[0-9]+$ ]] || threads=0
        ((process_count += 1))
        ((thread_count += threads))
        ((rss_kib += rss))
        pid_csv="${pid_csv:+$pid_csv,}$pid"
        cmdline="$(tr '\000' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true)"
        lower_cmdline="$(tr '[:upper:]' '[:lower:]' <<<"$cmdline")"

        is_python=false
        if [[ "$lower_comm" == *python* || "$lower_comm" == *uvicorn* ||
            "$lower_cmdline" == *python* || "$lower_cmdline" == *uvicorn* ]]; then
            is_python=true
        fi
        if [[ "$is_python" == "true" ]]; then
            ((python_count += 1))
            ((python_rss_kib += rss))
            if grep -Eiq '(large-v3|faster[-_]?whisper|ctranslate2|whisper-model)' \
                "/proc/$pid/cmdline" "/proc/$pid/maps" 2>/dev/null; then
                heavy_model_hint=true
            fi
        fi

        if [[ "$lower_comm" == *chromium* || "$lower_comm" == *chrome* || "$lower_comm" == *chromedriver* ]]; then
            ((chromium_count += 1))
            ((chromium_rss_kib += rss))
            chromium_pid_csv="${chromium_pid_csv:+$chromium_pid_csv,}$pid"
            if [[ "$lower_cmdline" == *javdb* ]]; then
                ((javdb_chromium_count += 1))
            elif [[ "$lower_cmdline" == *moviemuse-jl-* || "$lower_cmdline" == *javlibrary* ]]; then
                ((javlibrary_chromium_count += 1))
            else
                ((unclassified_chromium_count += 1))
            fi
        fi
    done <<<"$pids"

    jq -nc \
        --argjson process_count "$process_count" --argjson thread_count "$thread_count" \
        --argjson rss_bytes "$((rss_kib * 1024))" \
        --argjson python_count "$python_count" --argjson python_rss_bytes "$((python_rss_kib * 1024))" \
        --argjson chromium_count "$chromium_count" \
        --argjson chromium_rss_bytes "$((chromium_rss_kib * 1024))" \
        --argjson javdb_chromium_count "$javdb_chromium_count" \
        --argjson javlibrary_chromium_count "$javlibrary_chromium_count" \
        --argjson unclassified_chromium_count "$unclassified_chromium_count" \
        --argjson heavy_model_hint "$heavy_model_hint" \
        --arg pid_csv "$pid_csv" --arg chromium_pid_csv "$chromium_pid_csv" '
        {
            status:"ok",
            process_count:$process_count,
            thread_count:$thread_count,
            rss_bytes:$rss_bytes,
            pids:(if $pid_csv == "" then [] else ($pid_csv | split(",") | map(tonumber)) end),
            chromium_pids:(
                if $chromium_pid_csv == "" then []
                else ($chromium_pid_csv | split(",") | map(tonumber))
                end
            ),
            python_count:$python_count,
            python_rss_bytes:$python_rss_bytes,
            chromium_count:$chromium_count,
            chromium_rss_bytes:$chromium_rss_bytes,
            javdb_chromium_count:$javdb_chromium_count,
            javlibrary_chromium_count:$javlibrary_chromium_count,
            unclassified_chromium_count:$unclassified_chromium_count,
            heavy_model_hint:$heavy_model_hint
        }
    '
}

container_json() {
    local container="$1"
    local identity pid cgroup processes
    identity="$(container_identity_json "$container")"
    pid="$(jq -r '.pid // empty' <<<"$identity")"
    cgroup="$(cgroup_json "$pid")"
    if [[ "$(jq -r '.running' <<<"$identity")" == "true" ]]; then
        processes="$(process_json "$container")"
    else
        processes="$(
            jq -nc '
                {status:"unavailable", process_count:null, thread_count:null, rss_bytes:null,
                 pids:null, chromium_pids:null,
                 python_count:null, python_rss_bytes:null, chromium_count:null,
                 chromium_rss_bytes:null, javdb_chromium_count:null,
                 javlibrary_chromium_count:null, unclassified_chromium_count:null,
                 heavy_model_hint:null}
            '
        )"
    fi
    jq -nc --argjson identity "$identity" --argjson cgroup "$cgroup" \
        --argjson processes "$processes" '$identity + {cgroup:$cgroup, processes:$processes}'
}

curl_result_parts() {
    # Prints three lines: curl rc, HTTP metadata, response body.
    # The body starts on line three and may itself contain newlines.
    local raw="$1"
    local rc="$2"
    local marker="$3"
    local meta body
    meta="$(tail -n 1 <<<"$raw")"
    if [[ "$meta" == "$marker"* ]]; then
        body="$(sed '$d' <<<"$raw")"
        meta="${meta#"$marker"}"
    else
        body="$raw"
        meta="000|0"
    fi
    printf '%s\n%s\n%s' "$rc" "$meta" "$body"
}

health_json() {
    local marker="__MOVIEMUSE_HEALTH_${$}_${RANDOM}__"
    local raw rc parsed meta body http_code seconds elapsed_ms status subtitle_mode ok
    raw="$(curl -sS --connect-timeout 1 --max-time 1.5 --max-filesize 131072 \
        -w $'\n'"$marker"'%{http_code}|%{time_total}' "$HEALTH_URL" 2>/dev/null)"
    rc=$?
    parsed="$(curl_result_parts "$raw" "$rc" "$marker")"
    rc="$(sed -n '1p' <<<"$parsed")"
    meta="$(sed -n '2p' <<<"$parsed")"
    body="$(sed '1,2d' <<<"$parsed")"
    http_code="${meta%%|*}"
    seconds="${meta#*|}"
    [[ "$http_code" =~ ^[0-9]{3}$ ]] || http_code="0"
    elapsed_ms="$(awk -v seconds="$seconds" 'BEGIN {if (seconds ~ /^[0-9.]+$/) printf "%.0f", seconds * 1000; else print 0}')"
    status="$(jq -r 'if type == "object" then (.status // empty) else empty end' <<<"$body" 2>/dev/null)"
    subtitle_mode="$(jq -r 'if type == "object" then (.subtitle_mode // empty) else empty end' <<<"$body" 2>/dev/null)"
    ok=false
    if [[ "$rc" == "0" && "$http_code" == "200" && "$status" == "ok" ]]; then
        ok=true
    fi
    jq -nc --argjson ok "$ok" --argjson curl_exit "$rc" \
        --argjson http_code "$((10#$http_code))" --argjson elapsed_ms "$elapsed_ms" \
        --arg status "$status" --arg subtitle_mode "$subtitle_mode" '
        {
            ok:$ok,
            curl_exit:$curl_exit,
            http_code:(if $http_code == 0 then null else $http_code end),
            elapsed_ms:$elapsed_ms,
            status:(if $status == "" then null else $status end),
            subtitle_mode:(if $subtitle_mode == "" then null else $subtitle_mode end)
        }
    '
}

flare_sessions_json() {
    local marker="__MOVIEMUSE_FLARE_${$}_${RANDOM}__"
    local raw rc parsed meta body http_code seconds elapsed_ms response_status sessions
    raw="$(curl -sS --connect-timeout 1 --max-time 1.5 --max-filesize 262144 \
        -H 'Content-Type: application/json' -d '{"cmd":"sessions.list"}' \
        -w $'\n'"$marker"'%{http_code}|%{time_total}' "$FLARE_URL" 2>/dev/null)"
    rc=$?
    parsed="$(curl_result_parts "$raw" "$rc" "$marker")"
    rc="$(sed -n '1p' <<<"$parsed")"
    meta="$(sed -n '2p' <<<"$parsed")"
    body="$(sed '1,2d' <<<"$parsed")"
    http_code="${meta%%|*}"
    seconds="${meta#*|}"
    [[ "$http_code" =~ ^[0-9]{3}$ ]] || http_code="0"
    elapsed_ms="$(awk -v seconds="$seconds" 'BEGIN {if (seconds ~ /^[0-9.]+$/) printf "%.0f", seconds * 1000; else print 0}')"
    response_status="$(jq -r 'if type == "object" then (.status // empty) else empty end' <<<"$body" 2>/dev/null)"
    sessions="$(
        jq -c '
            if type == "object" and .status == "ok" and (.sessions | type) == "array" then
                [.sessions[] |
                    if type == "string" then .
                    elif type == "object" then (.id // .name // .session // tostring)
                    else tostring
                    end
                ] | sort
            else null
            end
        ' <<<"$body" 2>/dev/null
    )"
    [[ -n "$sessions" ]] || sessions="null"
    jq -nc --argjson curl_exit "$rc" --argjson http_code "$((10#$http_code))" \
        --argjson elapsed_ms "$elapsed_ms" --arg response_status "$response_status" \
        --argjson sessions "$sessions" '
        {
            api_ok:($curl_exit == 0 and $http_code == 200 and $response_status == "ok" and ($sessions | type) == "array"),
            curl_exit:$curl_exit,
            http_code:(if $http_code == 0 then null else $http_code end),
            elapsed_ms:$elapsed_ms,
            response_status:(if $response_status == "" then null else $response_status end),
            sessions:$sessions,
            session_count:(if ($sessions | type) == "array" then ($sessions | length) else null end),
            moviemuse_sessions:(
                if ($sessions | type) == "array" then
                    [$sessions[] | select(startswith("moviemuse-jl-"))]
                else null
                end
            ),
            moviemuse_session_count:(
                if ($sessions | type) == "array" then
                    ([$sessions[] | select(startswith("moviemuse-jl-"))] | length)
                else null
                end
            )
        }
    '
}

read_compute_connection() {
    local config_file="$APP_DATA_DIR/compute_settings.json"
    local backend_url="" backend_token="" model=""
    if [[ -r "$config_file" ]] && jq -e 'type == "object"' "$config_file" >/dev/null 2>&1; then
        backend_url="$(jq -r '.subtitle_backend_url // empty | select(type == "string")' "$config_file")"
        backend_token="$(jq -r '.subtitle_backend_token // empty | select(type == "string")' "$config_file")"
        model="$(jq -r '.whisper_model // empty | select(type == "string")' "$config_file")"
    fi
    if [[ -z "$backend_url" ]]; then
        backend_url="$(
            docker inspect "$MOVIEMUSE_CONTAINER" 2>/dev/null |
                jq -r '
                    (.[0].Config.Env // [] |
                    map(select(startswith("SUBTITLE_BACKEND_URL="))) | last // "") |
                    sub("^SUBTITLE_BACKEND_URL="; "")
                ' 2>/dev/null
        )"
    fi
    if [[ -z "$backend_token" ]]; then
        backend_token="$(
            docker inspect "$MOVIEMUSE_CONTAINER" 2>/dev/null |
                jq -r '
                    (.[0].Config.Env // [] |
                    map(select(startswith("SUBTITLE_BACKEND_TOKEN="))) | last // "") |
                    sub("^SUBTITLE_BACKEND_TOKEN="; "")
                ' 2>/dev/null
        )"
    fi
    if [[ -z "$model" ]]; then
        model="$(
            docker inspect "$MOVIEMUSE_CONTAINER" 2>/dev/null |
                jq -r '
                    (.[0].Config.Env // [] |
                    map(select(startswith("WHISPER_MODEL="))) | last // "") |
                    sub("^WHISPER_MODEL="; "")
                ' 2>/dev/null
        )"
    fi

    if [[ "$backend_url" != http://* && "$backend_url" != https://* ]]; then
        backend_url=""
    fi
    if [[ "$backend_url" == *"://"*'@'* ]]; then
        backend_url=""
    fi
    if [[ "$backend_url" == *$'\n'* || "$backend_url" == *$'\r'* ]]; then
        backend_url=""
    fi
    if [[ "$backend_token" == *$'\n'* || "$backend_token" == *$'\r'* ]]; then
        backend_token=""
    fi
    printf '%s\n%s\n%s' "$backend_url" "$backend_token" "$model"
}

worker_json() {
    local connection backend_url backend_token model model_class
    connection="$(read_compute_connection)"
    backend_url="$(sed -n '1p' <<<"$connection")"
    backend_token="$(sed -n '2p' <<<"$connection")"
    model="$(sed -n '3p' <<<"$connection")"
    backend_url="${backend_url%/}"
    model_class="other"
    if [[ -z "$model" ]]; then
        model_class="unknown"
    elif [[ "${model,,}" == *large-v3* || "${model,,}" == *whisper* ]]; then
        model_class="large-whisper"
    fi

    if [[ -z "$backend_url" ]]; then
        jq -nc --arg model_class "$model_class" '
            {configured:false, token_configured:false, online:null, curl_exit:null,
             http_code:null, elapsed_ms:null, status:null, mode:null,
             configured_model_class:$model_class}
        '
        return
    fi

    local marker="__MOVIEMUSE_WORKER_${$}_${RANDOM}__"
    local raw rc parsed meta body http_code seconds elapsed_ms status mode online
    if [[ -n "$backend_token" ]]; then
        local escaped_token
        escaped_token="${backend_token//\\/\\\\}"
        escaped_token="${escaped_token//\"/\\\"}"
        raw="$(
            printf 'header = "X-API-Key: %s"\n' "$escaped_token" |
                curl --config - -sS --connect-timeout 1 --max-time 2 --max-filesize 524288 \
                    -w $'\n'"$marker"'%{http_code}|%{time_total}' \
                    "$backend_url/api/subtitle/node/status" 2>/dev/null
        )"
        rc=$?
    else
        raw="$(
            curl -sS --connect-timeout 1 --max-time 2 --max-filesize 524288 \
                -w $'\n'"$marker"'%{http_code}|%{time_total}' \
                "$backend_url/api/subtitle/node/status" 2>/dev/null
        )"
        rc=$?
    fi
    parsed="$(curl_result_parts "$raw" "$rc" "$marker")"
    rc="$(sed -n '1p' <<<"$parsed")"
    meta="$(sed -n '2p' <<<"$parsed")"
    body="$(sed '1,2d' <<<"$parsed")"
    http_code="${meta%%|*}"
    seconds="${meta#*|}"
    [[ "$http_code" =~ ^[0-9]{3}$ ]] || http_code="0"
    elapsed_ms="$(awk -v seconds="$seconds" 'BEGIN {if (seconds ~ /^[0-9.]+$/) printf "%.0f", seconds * 1000; else print 0}')"
    status="$(jq -r 'if type == "object" then (.status // empty) else empty end' <<<"$body" 2>/dev/null)"
    mode="$(jq -r 'if type == "object" then (.mode // empty) else empty end' <<<"$body" 2>/dev/null)"
    online=false
    if [[ "$rc" == "0" && "$http_code" == "200" && ( "$status" == "ok" || "$status" == "online" ) ]]; then
        online=true
    fi
    jq -nc --argjson token_configured "$([[ -n "$backend_token" ]] && printf true || printf false)" \
        --argjson online "$online" --argjson curl_exit "$rc" \
        --argjson http_code "$((10#$http_code))" --argjson elapsed_ms "$elapsed_ms" \
        --arg status "$status" --arg mode "$mode" --arg model_class "$model_class" '
        {
            configured:true,
            token_configured:$token_configured,
            online:$online,
            curl_exit:$curl_exit,
            http_code:(if $http_code == 0 then null else $http_code end),
            elapsed_ms:$elapsed_ms,
            status:(if $status == "" then null else $status end),
            mode:(if $mode == "" then null else $mode end),
            configured_model_class:$model_class
        }
    '
}

sqlite_json() {
    local run_quick_check="$1"
    local database="$APP_DATA_DIR/subscriptions.sqlite3"
    local main_file wal_file shm_file
    main_file="$(metric_file_json "$database")"
    wal_file="$(metric_file_json "$database-wal")"
    shm_file="$(metric_file_json "$database-shm")"

    if [[ ! -r "$database" ]]; then
        jq -nc --argjson main "$main_file" --argjson wal "$wal_file" --argjson shm "$shm_file" '
            {
                files:{main:$main,wal:$wal,shm:$shm},
                aggregate_status:"missing", snapshot_mode:"immutable_main",
                wal_visibility:"not_applicable",
                event_count:null, latest_event_id:null, latest_event_epoch:null,
                worker_offline_total:null, worker_offline_last_hour:null,
                events_last_five_minutes:null, worker_offline_last_five_minutes:null,
                payload_average_bytes:null, payload_max_bytes:null,
                worker_offline_payload_average_last_hour_bytes:null,
                worker_offline_payload_max_last_hour_bytes:null,
                waiting_worker_count:null, javdb_source_enabled:null,
                quick_check:{attempted:false,status:null,result:null}
            }
        '
        return
    fi

    local sql result rc database_uri wal_visibility
    database_uri="file:$database?immutable=1"
    wal_visibility="main_only"
    if [[ "$(jq -r '.exists' <<<"$wal_file")" == "true" ]] &&
        (( $(jq -r '.bytes // 0' <<<"$wal_file") > 0 )); then
        wal_visibility="main_only_wal_present"
    fi
    sql="
        SELECT
            (SELECT count(*) FROM task_events),
            (SELECT COALESCE(max(id), 0) FROM task_events),
            (SELECT COALESCE(max(created_at), 0) FROM task_events),
            (SELECT count(*) FROM task_events WHERE stage = 'worker_offline'),
            (SELECT count(*) FROM task_events
                WHERE stage = 'worker_offline' AND created_at >= unixepoch('now') - 3600),
            (SELECT count(*) FROM task_events WHERE created_at >= unixepoch('now') - 300),
            (SELECT count(*) FROM task_events
                WHERE stage = 'worker_offline' AND created_at >= unixepoch('now') - 300),
            (SELECT COALESCE(round(avg(length(data_json))), 0) FROM task_events),
            (SELECT COALESCE(max(length(data_json)), 0) FROM task_events),
            (SELECT COALESCE(round(avg(length(data_json))), 0) FROM task_events
                WHERE stage = 'worker_offline' AND created_at >= unixepoch('now') - 3600),
            (SELECT COALESCE(max(length(data_json)), 0) FROM task_events
                WHERE stage = 'worker_offline' AND created_at >= unixepoch('now') - 3600),
            (SELECT count(*) FROM postprocess_tasks WHERE status = 'waiting_worker'),
            (SELECT COALESCE(
                (SELECT CASE lower(trim(value))
                    WHEN 'true' THEN 1 WHEN '1' THEN 1
                    WHEN 'false' THEN 0 WHEN '0' THEN 0 ELSE -1 END
                 FROM subscription_settings WHERE key = 'javdb_source_enabled' LIMIT 1),
                -1
            ))
    "
    result="$(
        sqlite3 -readonly -cmd '.timeout 1000' -cmd 'PRAGMA query_only=ON;' \
            -separator '|' "$database_uri" "$sql" 2>/dev/null
    )"
    rc=$?

    local aggregate
    if [[ "$rc" == "0" && "$result" =~ ^[0-9]+(\|[0-9]+){1}\|[0-9.]+(\|[-0-9.]+){10}$ ]]; then
        local event_count latest_event_id latest_event_epoch worker_offline_total
        local worker_offline_last_hour events_last_five worker_offline_last_five
        local payload_average payload_max worker_payload_average worker_payload_max
        local waiting_worker javdb_raw
        IFS='|' read -r event_count latest_event_id latest_event_epoch worker_offline_total \
            worker_offline_last_hour events_last_five worker_offline_last_five \
            payload_average payload_max worker_payload_average worker_payload_max \
            waiting_worker javdb_raw <<<"$result"
        aggregate="$(
            jq -nc \
                --argjson event_count "$event_count" --argjson latest_event_id "$latest_event_id" \
                --argjson latest_event_epoch "$latest_event_epoch" \
                --argjson worker_offline_total "$worker_offline_total" \
                --argjson worker_offline_last_hour "$worker_offline_last_hour" \
                --argjson events_last_five "$events_last_five" \
                --argjson worker_offline_last_five "$worker_offline_last_five" \
                --argjson payload_average "$payload_average" --argjson payload_max "$payload_max" \
                --argjson worker_payload_average "$worker_payload_average" \
                --argjson worker_payload_max "$worker_payload_max" \
                --argjson waiting_worker "$waiting_worker" --argjson javdb_raw "$javdb_raw" '
                {
                    aggregate_status:"ok",
                    event_count:$event_count,
                    latest_event_id:$latest_event_id,
                    latest_event_epoch:$latest_event_epoch,
                    worker_offline_total:$worker_offline_total,
                    worker_offline_last_hour:$worker_offline_last_hour,
                    events_last_five_minutes:$events_last_five,
                    worker_offline_last_five_minutes:$worker_offline_last_five,
                    payload_average_bytes:$payload_average,
                    payload_max_bytes:$payload_max,
                    worker_offline_payload_average_last_hour_bytes:$worker_payload_average,
                    worker_offline_payload_max_last_hour_bytes:$worker_payload_max,
                    waiting_worker_count:$waiting_worker,
                    javdb_source_enabled:(
                        if $javdb_raw == 1 then true
                        elif $javdb_raw == 0 then false
                        else null
                        end
                    )
                }
            '
        )"
    else
        aggregate="$(
            jq -nc --argjson sqlite_exit "$rc" '
                {
                    aggregate_status:"error",
                    sqlite_exit:$sqlite_exit,
                    event_count:null, latest_event_id:null, latest_event_epoch:null,
                    worker_offline_total:null, worker_offline_last_hour:null,
                    events_last_five_minutes:null, worker_offline_last_five_minutes:null,
                    payload_average_bytes:null, payload_max_bytes:null,
                    worker_offline_payload_average_last_hour_bytes:null,
                    worker_offline_payload_max_last_hour_bytes:null,
                    waiting_worker_count:null, javdb_source_enabled:null
                }
            '
        )"
    fi

    local quick_json
    if [[ "$run_quick_check" == "1" ]]; then
        local quick_result quick_rc quick_status
        quick_result="$(
            sqlite3 -readonly -cmd '.timeout 1000' -cmd 'PRAGMA query_only=ON;' \
                "$database_uri" 'PRAGMA quick_check(1);' 2>/dev/null
        )"
        quick_rc=$?
        quick_status="error"
        [[ "$quick_rc" == "0" && "$quick_result" == "ok" ]] && quick_status="ok"
        quick_json="$(
            jq -nc --arg status "$quick_status" --arg result "$quick_result" \
                --argjson sqlite_exit "$quick_rc" '
                {
                    attempted:true,
                    status:$status,
                    result:(if $result == "" then null else ($result | .[0:120]) end),
                    sqlite_exit:$sqlite_exit
                }
            '
        )"
    else
        quick_json="$(jq -nc '{attempted:false,status:null,result:null,sqlite_exit:null}')"
    fi

    jq -nc --argjson main "$main_file" --argjson wal "$wal_file" \
        --argjson shm "$shm_file" --argjson aggregate "$aggregate" \
        --argjson quick "$quick_json" --arg wal_visibility "$wal_visibility" '
        $aggregate + {
            snapshot_mode:"immutable_main",
            wal_visibility:$wal_visibility,
            files:{main:$main,wal:$wal,shm:$shm},
            quick_check:$quick
        }'
}

host_top_processes_json() {
    local rows
    rows="$(
        ps -eo pid=,rss=,comm= --sort=-rss 2>/dev/null |
            awk '
                NF >= 3 && count < 10 {
                    pid=$1; rss=$2; $1=""; $2="";
                    sub(/^[[:space:]]+/, "", $0);
                    gsub(/[\t\r\n]/, " ", $0);
                    printf "%s\t%s\t%s\n", pid, rss * 1024, $0;
                    count++
                }
            '
    )"
    jq -Rn --arg rows "$rows" '
        def number_or_null($value):
            if ($value | test("^[0-9]+$")) then ($value | tonumber) else null end;
        if $rows == "" then [] else
            [$rows | split("\n")[] | split("\t") |
                {pid:number_or_null(.[0]),rss_bytes:number_or_null(.[1]),process_type:.[2]}]
        end
    '
}

docker_inventory_json() {
    local since_epoch="$1"
    local rows="" id name cgroup_dir current peak maximum oom oom_kill
    while IFS=$'\t' read -r id name; do
        [[ "$id" =~ ^[a-f0-9]{64}$ && -n "$name" ]] || continue
        cgroup_dir="/sys/fs/cgroup/docker/$id"
        if [[ ! -d "$cgroup_dir" && -d "/sys/fs/cgroup/system.slice/docker-$id.scope" ]]; then
            cgroup_dir="/sys/fs/cgroup/system.slice/docker-$id.scope"
        fi
        current=""; peak=""; maximum=""; oom=""; oom_kill=""
        if [[ -d "$cgroup_dir" ]]; then
            [[ -r "$cgroup_dir/memory.current" ]] && current="$(<"$cgroup_dir/memory.current")"
            [[ -r "$cgroup_dir/memory.peak" ]] && peak="$(<"$cgroup_dir/memory.peak")"
            [[ -r "$cgroup_dir/memory.max" ]] && maximum="$(<"$cgroup_dir/memory.max")"
            if [[ -r "$cgroup_dir/memory.events" ]]; then
                oom="$(awk '$1 == "oom" {print $2}' "$cgroup_dir/memory.events")"
                oom_kill="$(awk '$1 == "oom_kill" {print $2}' "$cgroup_dir/memory.events")"
            fi
        fi
        rows+="${rows:+$'\n'}$id"$'\t'"$name"$'\t'"$current"$'\t'"$peak"$'\t'"$maximum"$'\t'"$oom"$'\t'"$oom_kill"
    done < <(docker ps --no-trunc --format '{{.ID}}\t{{.Names}}' 2>/dev/null)

    local recent_oom_events
    recent_oom_events="$(
        docker events --since "$since_epoch" --until "$NOW_EPOCH" --filter event=oom \
            --format '{{json .}}' 2>/dev/null |
            jq -sc '[.[] | {id:((.id // "")[0:12]),name:(.Actor.Attributes.name // null),
                epoch:(.time // null)}]' 2>/dev/null || printf '[]'
    )"

    jq -Rn --arg rows "$rows" --argjson recent_oom_events "$recent_oom_events" '
        def metric($value):
            if $value == "" then null
            elif $value == "max" then "max"
            elif ($value | test("^[0-9]+$")) then ($value | tonumber)
            else null end;
        (if $rows == "" then [] else
            [$rows | split("\n")[] | split("\t") |
                {id:(.[0][0:12]),name:.[1],memory_current_bytes:metric(.[2]),
                 memory_peak_bytes:metric(.[3]),memory_max_bytes:metric(.[4]),
                 oom:metric(.[5]),oom_kill:metric(.[6])}]
         end) as $all |
        {
            status:(if ($all | length) == 0 then "unknown" else "ok" end),
            running_count:($all | length),
            total_memory_current_bytes:([$all[].memory_current_bytes | numbers] | add // null),
            recent_oom_events:$recent_oom_events,
            containers:($all | map({id,name,memory_current_bytes,oom,oom_kill})),
            top_by_memory:($all | sort_by(.memory_current_bytes // -1) | reverse | .[:10])
        }
    '
}

virtual_machines_json() {
    if ! command -v virsh >/dev/null 2>&1; then
        jq -nc '{available:false,status:"virsh_unavailable",active_count:null,total_configured_bytes:null,total_rss_bytes:null,domains:null}'
        return
    fi

    local raw rc rows
    raw="$(virsh domstats --state --balloon --list-active --raw 2>/dev/null)"
    rc=$?
    if [[ "$rc" != "0" ]]; then
        jq -nc '{available:true,status:"query_failed",active_count:null,total_configured_bytes:null,total_rss_bytes:null,domains:null}'
        return
    fi
    rows="$(
        awk '
            function emit() {
                if (name != "") {
                    gsub(/[\t\r\n]/, " ", name)
                    printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n",
                        name, state, maximum, current, rss, unused, available, usable,
                        swap_in, swap_out, last_update
                }
            }
            /^Domain: / {
                emit(); name=$0; sub(/^Domain: /, "", name);
                sub(/^\047/, "", name); sub(/\047$/, "", name);
                state=maximum=current=rss=unused=available=usable=swap_in=swap_out=last_update="";
                next
            }
            /^  state.state=/ {split($0,a,"="); state=a[2]; next}
            /^  balloon.maximum=/ {split($0,a,"="); maximum=a[2]; next}
            /^  balloon.current=/ {split($0,a,"="); current=a[2]; next}
            /^  balloon.rss=/ {split($0,a,"="); rss=a[2]; next}
            /^  balloon.unused=/ {split($0,a,"="); unused=a[2]; next}
            /^  balloon.available=/ {split($0,a,"="); available=a[2]; next}
            /^  balloon.usable=/ {split($0,a,"="); usable=a[2]; next}
            /^  balloon.swap_in=/ {split($0,a,"="); swap_in=a[2]; next}
            /^  balloon.swap_out=/ {split($0,a,"="); swap_out=a[2]; next}
            /^  balloon.last-update=/ {split($0,a,"="); last_update=a[2]; next}
            END {emit()}
        ' <<<"$raw"
    )"

    jq -Rn --arg rows "$rows" '
        def integer($value):
            if ($value | test("^[0-9]+$")) then ($value | tonumber) else null end;
        def kib($value): (integer($value) | if type == "number" then . * 1024 else null end);
        (if $rows == "" then [] else
            [$rows | split("\n")[] | split("\t") |
                {name:.[0],state_code:integer(.[1]),configured_bytes:kib(.[2]),
                 current_balloon_bytes:kib(.[3]),rss_bytes:kib(.[4]),
                 guest_unused_bytes:kib(.[5]),guest_available_bytes:kib(.[6]),
                 guest_usable_bytes:kib(.[7]),swap_in_bytes:kib(.[8]),
                 swap_out_bytes:kib(.[9]),last_update_epoch:integer(.[10])}]
         end) as $domains |
        {
            available:true,status:"ok",active_count:($domains | length),
            total_configured_bytes:([$domains[].configured_bytes | numbers] | add // null),
            total_rss_bytes:([$domains[].rss_bytes | numbers] | add // null),
            domains:$domains
        }
    '
}

host_json() {
    local run_deep="$1"
    local meminfo vmstat cpu_stat boot_id="" uptime_seconds="" load1="" load5="" load15=""
    meminfo="$(
        awk '
            BEGIN {OFS="\t"}
            /^(MemTotal|MemFree|MemAvailable|Buffers|Cached|SReclaimable|SUnreclaim|Shmem|SwapTotal|SwapFree|Dirty|Writeback|PageTables|KernelStack|Committed_AS|CommitLimit):/ {
                key=$1; sub(/:$/, "", key); values[key]=$2 * 1024
            }
            END {
                keys="MemTotal MemFree MemAvailable Buffers Cached SReclaimable SUnreclaim Shmem SwapTotal SwapFree Dirty Writeback PageTables KernelStack Committed_AS CommitLimit"
                count=split(keys,a," "); for(i=1;i<=count;i++) printf "%s%s", values[a[i]], (i<count?OFS:ORS)
            }
        ' /proc/meminfo 2>/dev/null
    )"
    vmstat="$(
        awk '
            $1 == "oom_kill" {oom=$2}
            $1 == "pgmajfault" {major=$2}
            $1 == "pswpin" {swapin=$2}
            $1 == "pswpout" {swapout=$2}
            $1 ~ /^pgscan_/ {scan += $2}
            $1 ~ /^pgsteal_/ {steal += $2}
            $1 ~ /^allocstall_/ {stall += $2}
            $1 == "compact_stall" {compact_stall=$2}
            $1 == "compact_fail" {compact_fail=$2}
            END {printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n", oom,major,swapin,swapout,scan,steal,stall,compact_stall,compact_fail}
        ' /proc/vmstat 2>/dev/null
    )"
    cpu_stat="$(awk '/^cpu / {total=0; for(i=2;i<=NF;i++) total+=$i; print total "\t" $6; exit}' /proc/stat 2>/dev/null)"
    [[ -r /proc/sys/kernel/random/boot_id ]] && boot_id="$(</proc/sys/kernel/random/boot_id)"
    [[ -r /proc/uptime ]] && uptime_seconds="$(awk '{printf "%d", $1}' /proc/uptime)"
    if [[ -r /proc/loadavg ]]; then
        read -r load1 load5 load15 _ </proc/loadavg
    fi
    local procs_blocked=""
    [[ -r /proc/stat ]] && procs_blocked="$(awk '$1 == "procs_blocked" {print $2; exit}' /proc/stat)"

    local pressure_available=false pressure_memory="" pressure_io=""
    if [[ -r /proc/pressure/memory ]]; then pressure_available=true; pressure_memory="$(tr '\n' ';' </proc/pressure/memory)"; fi
    if [[ -r /proc/pressure/io ]]; then pressure_io="$(tr '\n' ';' </proc/pressure/io)"; fi

    local arc_available=false arc_size="" arc_target="" arc_max="" arc_throttle=""
    if [[ -r /proc/spl/kstat/zfs/arcstats ]]; then
        arc_available=true
        read -r arc_size arc_target arc_max arc_throttle < <(
            awk '$1=="size"{size=$3} $1=="c"{target=$3} $1=="c_max"{max=$3} $1=="memory_throttle_count"{throttle=$3} END{print size,target,max,throttle}' /proc/spl/kstat/zfs/arcstats
        )
    fi

    local kernel_available=false kernel_memcg_count="" kernel_global_count="" kernel_anomaly_count=""
    local kernel_signature="" kernel_kind="unknown" matches="" latest=""
    if [[ -r /var/log/syslog ]]; then
        kernel_available=true
        matches="$(
            tail -n 1000 /var/log/syslog 2>/dev/null |
                grep -Ei 'invoked oom-killer|Out of memory: Killed process|Memory cgroup out of memory|oom-kill:|blocked for more than|soft lockup|hard lockup|watchdog:|I/O error|Buffer I/O|blk_update_request|machine check|hardware error|EDAC' || true
        )"
        kernel_memcg_count="$(grep -Eic 'memory cgroup|CONSTRAINT_MEMCG|task_memcg=' <<<"$matches" || true)"
        kernel_global_count="$(grep -Eic 'invoked oom-killer|Out of memory: Killed process' <<<"$matches" || true)"
        kernel_anomaly_count="$(grep -Eic 'blocked for more than|soft lockup|hard lockup|watchdog:|I/O error|Buffer I/O|blk_update_request|machine check|hardware error|EDAC' <<<"$matches" || true)"
        latest="$(tail -n 1 <<<"$matches")"
        if [[ -n "$latest" ]]; then
            kernel_signature="$(cksum <<<"$latest" | awk '{print $1 ":" $2}')"
            if grep -Eiq 'memory cgroup|CONSTRAINT_MEMCG|task_memcg=' <<<"$latest"; then kernel_kind="memcg"
            elif grep -Eiq 'invoked oom-killer|Out of memory: Killed process' <<<"$latest"; then kernel_kind="global_candidate"
            elif grep -Eiq 'blocked for more than|soft lockup|hard lockup|watchdog:' <<<"$latest"; then kernel_kind="hang_or_lockup"
            elif grep -Eiq 'I/O error|Buffer I/O|blk_update_request' <<<"$latest"; then kernel_kind="io_error"
            else kernel_kind="hardware_error"; fi
        fi
    fi

    local docker_checked=false docker_cgroup_version="" docker_no_swap_limit=""
    if [[ "$run_deep" == "1" ]]; then
        docker_checked=true
        local docker_info_result docker_info_rc
        docker_info_result="$(docker info --format '{{.CgroupVersion}}|{{json .Warnings}}' 2>&1)"
        docker_info_rc=$?
        if [[ "$docker_info_rc" == "0" ]]; then
            docker_cgroup_version="${docker_info_result%%|*}"; docker_no_swap_limit=false
            grep -Fqi 'no swap limit support' <<<"$docker_info_result" && docker_no_swap_limit=true
        fi
    fi

    local persistent_syslog=false
    if [[ -r /boot/config/rsyslog.conf ]] && grep -Eq '^\*\.debug[[:space:]]+\?flash' /boot/config/rsyslog.conf; then
        persistent_syslog=true
    fi
    local top_processes
    top_processes="$(host_top_processes_json)"

    jq -nc --arg hostname "$(hostname)" --arg boot_id "$boot_id" --arg uptime "$uptime_seconds" \
        --arg meminfo "$meminfo" --arg vmstat "$vmstat" --arg cpu_stat "$cpu_stat" \
        --arg load1 "$load1" --arg load5 "$load5" --arg load15 "$load15" --arg blocked "$procs_blocked" \
        --argjson pressure_available "$pressure_available" --arg pressure_memory "$pressure_memory" --arg pressure_io "$pressure_io" \
        --argjson arc_available "$arc_available" --arg arc_size "$arc_size" --arg arc_target "$arc_target" --arg arc_max "$arc_max" --arg arc_throttle "$arc_throttle" \
        --argjson kernel_available "$kernel_available" --arg kernel_memcg_count "$kernel_memcg_count" \
        --arg kernel_global_count "$kernel_global_count" --arg kernel_anomaly_count "$kernel_anomaly_count" \
        --arg kernel_signature "$kernel_signature" --arg kernel_kind "$kernel_kind" \
        --argjson persistent_syslog "$persistent_syslog" --argjson docker_checked "$docker_checked" \
        --arg docker_cgroup_version "$docker_cgroup_version" --arg docker_no_swap_limit "$docker_no_swap_limit" \
        --argjson top_processes "$top_processes" '
        def number_or_null($value): if ($value | test("^[0-9]+$")) then ($value | tonumber) else null end;
        def decimal_or_null($value): if ($value | test("^[0-9]+([.][0-9]+)?$")) then ($value | tonumber) else null end;
        ($meminfo | split("\t")) as $m | ($vmstat | split("\t")) as $v | ($cpu_stat | split("\t")) as $cpu |
        {
            hostname:$hostname,boot_id:(if $boot_id=="" then null else $boot_id end),uptime_seconds:number_or_null($uptime),
            memory_total_bytes:number_or_null($m[0]),memory_free_bytes:number_or_null($m[1]),memory_available_bytes:number_or_null($m[2]),
            buffers_bytes:number_or_null($m[3]),cached_bytes:number_or_null($m[4]),sreclaimable_bytes:number_or_null($m[5]),
            sunreclaim_bytes:number_or_null($m[6]),shmem_bytes:number_or_null($m[7]),swap_total_bytes:number_or_null($m[8]),
            swap_free_bytes:number_or_null($m[9]),dirty_bytes:number_or_null($m[10]),writeback_bytes:number_or_null($m[11]),
            page_tables_bytes:number_or_null($m[12]),kernel_stack_bytes:number_or_null($m[13]),committed_as_bytes:number_or_null($m[14]),commit_limit_bytes:number_or_null($m[15]),
            load:{one:decimal_or_null($load1),five:decimal_or_null($load5),fifteen:decimal_or_null($load15),procs_blocked:number_or_null($blocked)},
            cpu:{total_jiffies:number_or_null($cpu[0]),iowait_jiffies:number_or_null($cpu[1]),iowait_percent_since_previous:null},
            vmstat:{oom_kill:number_or_null($v[0]),pgmajfault:number_or_null($v[1]),pswpin:number_or_null($v[2]),pswpout:number_or_null($v[3]),
                pgscan:number_or_null($v[4]),pgsteal:number_or_null($v[5]),allocstall:number_or_null($v[6]),compact_stall:number_or_null($v[7]),compact_fail:number_or_null($v[8])},
            pressure:{available:$pressure_available,memory_raw:(if $pressure_memory=="" then null else $pressure_memory end),io_raw:(if $pressure_io=="" then null else $pressure_io end)},
            zfs_arc:{available:$arc_available,size_bytes:number_or_null($arc_size),target_bytes:number_or_null($arc_target),max_bytes:number_or_null($arc_max),memory_throttle_count:number_or_null($arc_throttle)},
            top_processes:$top_processes,
            docker:{checked:$docker_checked,cgroup_version:(if $docker_cgroup_version=="" then null else $docker_cgroup_version end),
                no_swap_limit_support:(if $docker_no_swap_limit=="true" then true elif $docker_no_swap_limit=="false" then false else null end)},
            kernel_oom_signal:{source:(if $kernel_available then "/var/log/syslog-tail" else null end),available:$kernel_available,
                persistent_local_mirror:$persistent_syslog,memcg_pattern_count:number_or_null($kernel_memcg_count),
                global_candidate_pattern_count:number_or_null($kernel_global_count),anomaly_pattern_count:number_or_null($kernel_anomaly_count),
                latest_signature:(if $kernel_signature=="" then null else $kernel_signature end),latest_kind:(if $kernel_signature=="" then null else $kernel_kind end)}
        }
    '
}

log_summary_json() {
    local moviemuse_summary flare_summary
    moviemuse_summary="$(
        docker logs --since 10m --tail 200 "$MOVIEMUSE_CONTAINER" 2>&1 |
            awk '
                BEGIN {IGNORECASE=1}
                /oom|out of memory/ {oom++}
                /worker_offline|worker offline|算力端.*离线/ {worker_offline++}
                /large-v3|whisper|ctranslate2/ {model++}
                /error|exception|traceback|failed/ {error++}
                END {printf "%d|%d|%d|%d", oom+0, worker_offline+0, model+0, error+0}
            '
    )"
    flare_summary="$(
        docker logs --since 10m --tail 200 "$FLARE_CONTAINER" 2>&1 |
            awk '
                BEGIN {IGNORECASE=1}
                /oom|out of memory/ {oom++}
                /session/ {session++}
                /chromium|chrome/ {chromium++}
                /error|exception|traceback|failed/ {error++}
                END {printf "%d|%d|%d|%d", oom+0, session+0, chromium+0, error+0}
            '
    )"
    local mm_oom mm_worker mm_model mm_error flare_oom flare_session flare_chromium flare_error
    IFS='|' read -r mm_oom mm_worker mm_model mm_error <<<"$moviemuse_summary"
    IFS='|' read -r flare_oom flare_session flare_chromium flare_error <<<"$flare_summary"
    jq -nc \
        --argjson mm_oom "$mm_oom" --argjson mm_worker "$mm_worker" \
        --argjson mm_model "$mm_model" --argjson mm_error "$mm_error" \
        --argjson flare_oom "$flare_oom" --argjson flare_session "$flare_session" \
        --argjson flare_chromium "$flare_chromium" --argjson flare_error "$flare_error" '
        {
            window_minutes:10,
            moviemuse_keyword_counts:{
                oom:$mm_oom,worker_offline:$mm_worker,model:$mm_model,error:$mm_error
            },
            flaresolverr_keyword_counts:{
                oom:$flare_oom,session:$flare_session,chromium:$flare_chromium,error:$flare_error
            },
            raw_lines_stored:false
        }
    '
}

kernel_incident_evidence_json() {
    local source="/var/log/syslog"
    if [[ -r /boot/logs/syslog ]]; then
        source="/boot/logs/syslog"
    elif [[ ! -r "$source" ]]; then
        jq -nc '{available:false,source:null,lines:[]}'
        return
    fi
    local lines
    lines="$(
        tail -n 4000 "$source" 2>/dev/null |
            grep -Ei 'invoked oom-killer|Out of memory: Killed process|Memory cgroup out of memory|oom-kill:|Mem-Info|active_anon|inactive_anon|blocked for more than|soft lockup|hard lockup|watchdog:|I/O error|Buffer I/O|blk_update_request|machine check|hardware error|EDAC' |
            tail -n 80 || true
    )"
    jq -Rn --arg lines "$lines" --arg source "$source" '
        {available:true,source:($source + "-relevant-tail"),line_limit:80,
         lines:(if $lines == "" then [] else ($lines | split("\n")) end)}
    '
}

write_error() {
    local message="$1"
    [[ "$MODE" == "collect" ]] || {
        printf '%s\n' "$message" >&2
        return
    }
    local error_file="$STATE_DIR/collector-errors.log"
    if [[ -f "$error_file" ]] && (( $(stat -c '%s' "$error_file" 2>/dev/null || printf 0) > 1048576 )); then
        tail -n 200 "$error_file" >"$STATE_DIR/.collector-errors.$$.tmp"
        mv -f "$STATE_DIR/.collector-errors.$$.tmp" "$error_file"
    fi
    printf '%s %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$message" >>"$error_file"
}

initialize_output_tree() {
    mkdir -p "$DATA_DIR" || fail "cannot create $DATA_DIR"
    [[ ! -L "$DATA_DIR" ]] || fail "monitoring data root must not be a symlink"
    DATA_REAL="$(readlink -f "$DATA_DIR")"
    [[ -n "$DATA_REAL" && "$DATA_REAL" != "/" ]] || fail "cannot resolve monitoring data root"
    if [[ "$TEST_MODE" != "1" && "$DATA_REAL" != "$DEFAULT_DATA_DIR" ]]; then
        fail "resolved production data root is not $DEFAULT_DATA_DIR"
    fi

    STATE_DIR="$DATA_REAL/state"
    SAMPLES_DIR="$DATA_REAL/samples"
    EVENTS_DIR="$DATA_REAL/events"
    INCIDENTS_DIR="$DATA_REAL/incidents"
    for child_dir in "$STATE_DIR" "$SAMPLES_DIR" "$EVENTS_DIR" "$INCIDENTS_DIR"; do
        mkdir -p "$child_dir" || fail "cannot create $child_dir"
        [[ ! -L "$child_dir" ]] || fail "monitoring child directories must not be symlinks"
        case "$(readlink -f "$child_dir")" in
            "$DATA_REAL"/*)
                ;;
            *)
                fail "monitoring child directory escaped the validated root"
                ;;
        esac
    done
}

cleanup_retention() {
    local cleanup_date="$1"
    local previous_cleanup=""
    [[ -r "$STATE_DIR/last-cleanup-date" ]] && previous_cleanup="$(<"$STATE_DIR/last-cleanup-date")"
    [[ "$previous_cleanup" != "$cleanup_date" ]] || return

    find "$SAMPLES_DIR" -maxdepth 1 -type f -name 'health-????-??-??.jsonl' -mtime +13 -delete
    find "$EVENTS_DIR" -maxdepth 1 -type f -name 'events-????-??-??.jsonl' -mtime +29 -delete
    find "$INCIDENTS_DIR" -maxdepth 1 -type f -name 'incident-*.json' -mtime +29 -delete

    local incident_count excess
    incident_count="$(
        find "$INCIDENTS_DIR" -maxdepth 1 -type f -name 'incident-*.json' -printf '%f\n' |
            wc -l
    )"
    if [[ "$incident_count" =~ ^[0-9]+$ ]] && ((incident_count > 500)); then
        excess=$((incident_count - 500))
        find "$INCIDENTS_DIR" -maxdepth 1 -type f -name 'incident-*.json' -printf '%f\n' |
            sort |
            awk -v limit="$excess" 'NR <= limit' |
            while IFS= read -r old_incident; do
                [[ "$old_incident" =~ ^incident-[A-Za-z0-9_.:-]+\.json$ ]] || continue
                rm -f "$INCIDENTS_DIR/$old_incident"
            done
    fi
    printf '%s\n' "$cleanup_date" >"$STATE_DIR/.last-cleanup-date.$$.tmp"
    mv -f "$STATE_DIR/.last-cleanup-date.$$.tmp" "$STATE_DIR/last-cleanup-date"
}

START_MS="$(epoch_ms)"
NOW_EPOCH="$(date +%s)"
NOW_ISO="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
TODAY_UTC="$(date -u +'%Y-%m-%d')"
HOSTNAME_VALUE="$(hostname)"

PREVIOUS_SAMPLE='{}'
if [[ "$MODE" == "collect" ]]; then
    initialize_output_tree
    exec 9>"$STATE_DIR/collector.lock"
    if ! flock -n 9; then
        jq -nc --arg timestamp "$NOW_ISO" --argjson epoch "$NOW_EPOCH" \
            '{timestamp:$timestamp,epoch:$epoch,reason:"previous_run_still_active"}' \
            >>"$STATE_DIR/skipped-runs.jsonl"
        exit 0
    fi

    available_kib="$(df -Pk "$DATA_REAL" | awk 'NR == 2 {print $4}')"
    if [[ "$available_kib" =~ ^[0-9]+$ ]] && ((available_kib < 131072)); then
        write_error "free space below 128 MiB; detailed sample skipped"
        exit 1
    fi
    sample_file="$SAMPLES_DIR/health-$TODAY_UTC.jsonl"
    if [[ -f "$sample_file" ]] && (( $(stat -c '%s' "$sample_file" 2>/dev/null || printf 0) > 20971520 )); then
        write_error "daily sample file exceeded 20 MiB; sample skipped"
        exit 1
    fi
    if [[ -r "$STATE_DIR/previous-sample.json" ]] &&
        jq -e 'type == "object"' "$STATE_DIR/previous-sample.json" >/dev/null 2>&1; then
        PREVIOUS_SAMPLE="$(<"$STATE_DIR/previous-sample.json")"
    fi
    cleanup_retention "$TODAY_UTC"
fi

LAST_DEEP_SLOT="$(jq -r '.monitor_state.last_deep_slot // -1' <<<"$PREVIOUS_SAMPLE" 2>/dev/null)"
CURRENT_DEEP_SLOT=$((NOW_EPOCH / 300))
RUN_DEEP=0
if [[ "$MODE" == "probe" || "$LAST_DEEP_SLOT" != "$CURRENT_DEEP_SLOT" ]]; then
    RUN_DEEP=1
fi

LAST_QUICK_DATE="$(jq -r '.monitor_state.last_quick_check_date // empty' <<<"$PREVIOUS_SAMPLE" 2>/dev/null)"
RUN_QUICK=0
if [[ "$MODE" == "collect" && "$RUN_DEEP" == "1" && "$LAST_QUICK_DATE" != "$TODAY_UTC" ]]; then
    RUN_QUICK=1
fi

HOST_JSON="$(host_json "$RUN_DEEP")"
DOCKER_EVENT_SINCE="$(jq -r '.epoch // empty' <<<"$PREVIOUS_SAMPLE" 2>/dev/null)"
if [[ ! "$DOCKER_EVENT_SINCE" =~ ^[0-9]+$ ]] || ((DOCKER_EVENT_SINCE < NOW_EPOCH - 86400)); then
    DOCKER_EVENT_SINCE=$((NOW_EPOCH - 120))
fi
DOCKER_INVENTORY_JSON="$(docker_inventory_json "$DOCKER_EVENT_SINCE")"
VIRTUAL_MACHINES_JSON="$(virtual_machines_json)"
MOVIEMUSE_JSON="$(container_json "$MOVIEMUSE_CONTAINER")"
FLARE_CONTAINER_JSON="$(container_json "$FLARE_CONTAINER")"
HEALTH_JSON="$(health_json)"
FLARE_SESSIONS_JSON="$(flare_sessions_json)"
DB_FILES_JSON="$(
    jq -nc \
        --argjson main "$(metric_file_json "$APP_DATA_DIR/subscriptions.sqlite3")" \
        --argjson wal "$(metric_file_json "$APP_DATA_DIR/subscriptions.sqlite3-wal")" \
        --argjson shm "$(metric_file_json "$APP_DATA_DIR/subscriptions.sqlite3-shm")" \
        '{
            files:{main:$main,wal:$wal,shm:$shm},
            aggregate_status:"not_due",
            snapshot_mode:"immutable_main",
            wal_visibility:null,
            quick_check:{attempted:false,status:null,result:null}
        }'
)"
WORKER_JSON="$(jq -nc '{configured:null,online:null,status:null,mode:null,configured_model_class:null}')"
if [[ "$RUN_DEEP" == "1" ]]; then
    DB_JSON="$(sqlite_json "$RUN_QUICK")"
    WORKER_JSON="$(worker_json)"
else
    DB_JSON="$DB_FILES_JSON"
fi

FLARE_JSON="$(
    jq -nc --argjson container "$FLARE_CONTAINER_JSON" --argjson sessions "$FLARE_SESSIONS_JSON" \
        '{container:$container,sessions:$sessions}'
)"

BASE_SAMPLE="$(
    jq -nc \
        --arg schema_version "$SCHEMA_VERSION" --arg timestamp "$NOW_ISO" \
        --argjson epoch "$NOW_EPOCH" --arg host "$HOSTNAME_VALUE" \
        --argjson deep_sample "$([[ "$RUN_DEEP" == "1" ]] && printf true || printf false)" \
        --argjson host_metrics "$HOST_JSON" --argjson moviemuse "$MOVIEMUSE_JSON" \
        --argjson health "$HEALTH_JSON" --argjson flaresolverr "$FLARE_JSON" \
        --argjson docker_inventory "$DOCKER_INVENTORY_JSON" \
        --argjson virtual_machines "$VIRTUAL_MACHINES_JSON" \
        --argjson database "$DB_JSON" --argjson worker "$WORKER_JSON" '
        {
            schema_version:($schema_version | tonumber),
            record_type:"health_sample",
            timestamp:$timestamp,
            epoch:$epoch,
            host:$host_metrics,
            sample:{deep:$deep_sample,duration_ms:null},
            docker_containers:$docker_inventory,
            virtual_machines:$virtual_machines,
            moviemuse:($moviemuse + {health:$health}),
            flaresolverr:$flaresolverr,
            database:$database,
            compute_worker:$worker
        }
    '
)"

BASE_SAMPLE="$(
    jq -nc --argjson current "$BASE_SAMPLE" --argjson now "$NOW_EPOCH" '
        $current |
        if (.virtual_machines.domains | type) == "array" then
            .virtual_machines.domains |= map(
                .stats_age_seconds =
                    (if (.last_update_epoch | type) == "number" and $now >= .last_update_epoch
                     then $now - .last_update_epoch else null end)
            )
        else . end
    '
)"

BASE_SAMPLE="$(
    jq -nc --argjson previous "$PREVIOUS_SAMPLE" --argjson current "$BASE_SAMPLE" '
        def delta($old; $new):
            if ($old | type) == "number" and ($new | type) == "number" and $new >= $old
            then $new - $old else null end;
        ($previous.host.boot_id // null) as $old_boot |
        ($current.host.boot_id // null) as $new_boot |
        if $old_boot != null and $old_boot == $new_boot then
            (delta($previous.host.cpu.total_jiffies; $current.host.cpu.total_jiffies)) as $cpu_delta |
            (delta($previous.host.cpu.iowait_jiffies; $current.host.cpu.iowait_jiffies)) as $iowait_delta |
            $current |
            .host.cpu.iowait_percent_since_previous =
                (if ($cpu_delta | type) == "number" and $cpu_delta > 0 and ($iowait_delta | type) == "number"
                 then (($iowait_delta * 10000 / $cpu_delta | round) / 100) else null end) |
            .host.vmstat_delta = {
                oom_kill:delta($previous.host.vmstat.oom_kill; .host.vmstat.oom_kill),
                pgmajfault:delta($previous.host.vmstat.pgmajfault; .host.vmstat.pgmajfault),
                pswpin:delta($previous.host.vmstat.pswpin; .host.vmstat.pswpin),
                pswpout:delta($previous.host.vmstat.pswpout; .host.vmstat.pswpout),
                pgscan:delta($previous.host.vmstat.pgscan; .host.vmstat.pgscan),
                pgsteal:delta($previous.host.vmstat.pgsteal; .host.vmstat.pgsteal),
                allocstall:delta($previous.host.vmstat.allocstall; .host.vmstat.allocstall),
                compact_stall:delta($previous.host.vmstat.compact_stall; .host.vmstat.compact_stall),
                compact_fail:delta($previous.host.vmstat.compact_fail; .host.vmstat.compact_fail)
            }
        else
            $current | .host.vmstat_delta = {
                oom_kill:null,pgmajfault:null,pswpin:null,pswpout:null,pgscan:null,
                pgsteal:null,allocstall:null,compact_stall:null,compact_fail:null
            }
        end
    '
)"

MONITOR_STATE="$(
    jq -nc --argjson previous "$PREVIOUS_SAMPLE" --argjson current "$BASE_SAMPLE" \
        --argjson now "$NOW_EPOCH" --arg today "$TODAY_UTC" \
        --argjson deep_slot "$CURRENT_DEEP_SLOT" \
        --argjson ran_deep "$([[ "$RUN_DEEP" == "1" ]] && printf true || printf false)" \
        --argjson ran_quick "$([[ "$RUN_QUICK" == "1" ]] && printf true || printf false)" '
        ($previous.monitor_state // {}) as $old |
        ($current.moviemuse.health.ok == true) as $health_ok |
        ($current.flaresolverr.sessions.api_ok == true) as $flare_api_ok |
        ($current.flaresolverr.sessions.session_count) as $session_count |
        ($current.flaresolverr.container.processes.chromium_count) as $chromium_count |
        ($previous.flaresolverr.sessions.sessions) as $old_sessions |
        ($current.flaresolverr.sessions.sessions) as $new_sessions |
        ($current.database.files.main.bytes) as $db_bytes |
        ($current.database.files.wal.bytes) as $wal_bytes |
        ($previous.database.files.wal.bytes) as $old_wal_bytes |
        ($current.host.memory_total_bytes) as $host_total |
        ($current.host.memory_available_bytes) as $host_available |
        ($current.host.cpu.iowait_percent_since_previous) as $host_iowait |
        ($current.host.load.procs_blocked) as $host_blocked |
        {
            health_failure_streak:(
                if $health_ok then 0 else (($old.health_failure_streak // 0) + 1) end
            ),
            flare_api_failure_streak:(
                if $flare_api_ok then 0 else (($old.flare_api_failure_streak // 0) + 1) end
            ),
            flare_orphan_streak:(
                if ($session_count | type) != "number" or ($chromium_count | type) != "number" then
                    ($old.flare_orphan_streak // 0)
                elif $session_count == 0 and $chromium_count > 0 then
                    (($old.flare_orphan_streak // 0) + 1)
                else 0
                end
            ),
            wal_growth_streak:(
                if ($wal_bytes | type) == "number" and ($old_wal_bytes | type) == "number"
                    and $wal_bytes > 67108864 and $wal_bytes > $old_wal_bytes then
                    (($old.wal_growth_streak // 0) + 1)
                else 0
                end
            ),
            host_low_memory_streak:(
                if ($host_total | type) == "number" and $host_total > 0
                    and ($host_available | type) == "number"
                    and ($host_available / $host_total) < 0.20 then
                    (($old.host_low_memory_streak // 0) + 1)
                else 0 end
            ),
            host_iowait_streak:(
                if ($host_iowait | type) == "number" and $host_iowait >= 25 then
                    (($old.host_iowait_streak // 0) + 1)
                else 0 end
            ),
            host_blocked_streak:(
                if ($host_blocked | type) == "number" and $host_blocked >= 8 then
                    (($old.host_blocked_streak // 0) + 1)
                else 0 end
            ),
            session_transition_epochs:(
                (($old.session_transition_epochs // []) | map(select(. >= ($now - 3600)))) +
                (if ($old_sessions | type) == "array" and ($new_sessions | type) == "array"
                    and $old_sessions != $new_sessions then [$now] else [] end)
            ),
            db_baseline_date:(
                if ($db_bytes | type) == "number" and $old.db_baseline_date != $today then $today
                else ($old.db_baseline_date // $today)
                end
            ),
            db_baseline_bytes:(
                if ($db_bytes | type) != "number" then ($old.db_baseline_bytes // null)
                elif $old.db_baseline_date != $today then $db_bytes
                else ($old.db_baseline_bytes // $db_bytes)
                end
            ),
            last_deep_slot:(if $ran_deep then $deep_slot else ($old.last_deep_slot // -1) end),
            last_quick_check_date:(
                if $ran_quick then $today else ($old.last_quick_check_date // null) end
            )
        }
    '
)"

EVENTS_JSON="$(
    jq -nc --argjson previous "$PREVIOUS_SAMPLE" --argjson current "$BASE_SAMPLE" \
        --argjson state "$MONITOR_STATE" --arg timestamp "$NOW_ISO" --argjson epoch "$NOW_EPOCH" '
        def event($kind; $details):
            {kind:$kind,timestamp:$timestamp,epoch:$epoch,details:$details};
        [
            $current.docker_containers.containers[]? as $container |
            ($previous.docker_containers.containers // [] |
                map(select(.id == $container.id)) | first // null) as $old |
            select($old != null and ($old.oom_kill | type) == "number"
                and ($container.oom_kill | type) == "number"
                and $container.oom_kill > $old.oom_kill) |
            {id:$container.id,name:$container.name,previous:$old.oom_kill,current:$container.oom_kill}
        ] as $container_oom_increments |
        (($previous.virtual_machines.domains // []) | map(.name) | sort) as $old_vms |
        (($current.virtual_machines.domains // []) | map(.name) | sort) as $new_vms |
        [
            if (($previous.epoch // null) | type) == "number"
                and ($current.epoch - $previous.epoch) > 150 then
                event("sample_gap"; {previous_epoch:$previous.epoch,current_epoch:$current.epoch,
                    gap_seconds:($current.epoch - $previous.epoch)})
            else empty end,
            if (($previous.host.boot_id // "") != "")
                and (($current.host.boot_id // "") != "")
                and $previous.host.boot_id != $current.host.boot_id then
                event("host_reboot"; {previous_boot_id:$previous.host.boot_id,
                    current_boot_id:$current.host.boot_id,current_uptime_seconds:$current.host.uptime_seconds})
            else empty end,
            if ($old_vms | length) > 0 and $old_vms != $new_vms then
                event("virtual_machine_set_changed"; {previous:$old_vms,current:$new_vms})
            else empty end,
            if ($container_oom_increments | length) > 0 then
                event("docker_container_oom_kill"; {containers:$container_oom_increments})
            else empty end,
            if ($current.docker_containers.recent_oom_events // [] | length) > 0 then
                event("docker_daemon_oom_event"; {
                    containers:$current.docker_containers.recent_oom_events})
            else empty end,
            if (($previous.moviemuse.id // "") != "")
                and $previous.moviemuse.id != $current.moviemuse.id then
                event("moviemuse_identity_change"; {
                    previous_id:$previous.moviemuse.id,
                    current_id:$current.moviemuse.id,
                    previous_image_id:$previous.moviemuse.image_id,
                    current_image_id:$current.moviemuse.image_id
                })
            else empty end,
            if (($previous.flaresolverr.container.id // "") != "")
                and $previous.flaresolverr.container.id != $current.flaresolverr.container.id then
                event("flaresolverr_identity_change"; {
                    previous_id:$previous.flaresolverr.container.id,
                    current_id:$current.flaresolverr.container.id,
                    previous_image_id:$previous.flaresolverr.container.image_id,
                    current_image_id:$current.flaresolverr.container.image_id
                })
            else empty end,
            if $previous.moviemuse.id == $current.moviemuse.id
                and ($previous.moviemuse.restart_count | type) == "number"
                and ($current.moviemuse.restart_count | type) == "number"
                and $current.moviemuse.restart_count > $previous.moviemuse.restart_count then
                event("moviemuse_restart_count_increment"; {
                    previous:$previous.moviemuse.restart_count,
                    current:$current.moviemuse.restart_count
                })
            else empty end,
            if $previous.flaresolverr.container.id == $current.flaresolverr.container.id
                and ($previous.flaresolverr.container.restart_count | type) == "number"
                and ($current.flaresolverr.container.restart_count | type) == "number"
                and $current.flaresolverr.container.restart_count
                    > $previous.flaresolverr.container.restart_count then
                event("flaresolverr_restart_count_increment"; {
                    previous:$previous.flaresolverr.container.restart_count,
                    current:$current.flaresolverr.container.restart_count
                })
            else empty end,
            if ($previous.flaresolverr.sessions.sessions | type) == "array"
                and ($current.flaresolverr.sessions.sessions | type) == "array"
                and $previous.flaresolverr.sessions.sessions != $current.flaresolverr.sessions.sessions then
                event("flare_sessions_changed"; {
                    previous:$previous.flaresolverr.sessions.sessions,
                    current:$current.flaresolverr.sessions.sessions
                })
            else empty end,
            if $previous.moviemuse.id == $current.moviemuse.id
                and ($previous.moviemuse.cgroup.events.oom_kill | type) == "number"
                and ($current.moviemuse.cgroup.events.oom_kill | type) == "number"
                and $current.moviemuse.cgroup.events.oom_kill > $previous.moviemuse.cgroup.events.oom_kill then
                event("moviemuse_memcg_oom_kill"; {
                    previous:$previous.moviemuse.cgroup.events.oom_kill,
                    current:$current.moviemuse.cgroup.events.oom_kill
                })
            else empty end,
            if ($previous.host.kernel_oom_signal.latest_signature // "") != ""
                and ($current.host.kernel_oom_signal.latest_signature // "") != ""
                and $previous.host.kernel_oom_signal.latest_signature
                    != $current.host.kernel_oom_signal.latest_signature then
                event("kernel_signal_changed"; {
                    kind:$current.host.kernel_oom_signal.latest_kind,
                    signature:$current.host.kernel_oom_signal.latest_signature
                })
            else empty end
        ]
    '
)"

ALERTS_JSON="$(
    jq -nc --argjson previous "$PREVIOUS_SAMPLE" --argjson current "$BASE_SAMPLE" \
        --argjson state "$MONITOR_STATE" '
        def alert($code; $severity; $value; $threshold):
            {code:$code,severity:$severity,value:$value,threshold:$threshold};
        [
            $current.docker_containers.containers[]? as $container |
            ($previous.docker_containers.containers // [] |
                map(select(.id == $container.id)) | first // null) as $old |
            select($old != null and ($old.oom_kill | type) == "number"
                and ($container.oom_kill | type) == "number"
                and $container.oom_kill > $old.oom_kill) |
            {name:$container.name,increment:($container.oom_kill - $old.oom_kill)}
        ] as $container_oom_increments |
        [
            if ($current.host.memory_total_bytes | type) == "number"
                and $current.host.memory_total_bytes > 0
                and ($current.host.memory_available_bytes | type) == "number"
                and ($current.host.memory_available_bytes / $current.host.memory_total_bytes) < 0.10 then
                alert("host_memory_available_low";"critical";$current.host.memory_available_bytes;
                    ($current.host.memory_total_bytes * 0.10 | floor))
            elif $state.host_low_memory_streak >= 3 then
                alert("host_memory_available_low";"warning";$current.host.memory_available_bytes;
                    ($current.host.memory_total_bytes * 0.20 | floor))
            else empty end,
            if ($current.host.vmstat_delta.oom_kill | type) == "number"
                and $current.host.vmstat_delta.oom_kill > 0 then
                alert("host_vmstat_oom_kill_increment";"critical";$current.host.vmstat_delta.oom_kill;0)
            else empty end,
            if ($container_oom_increments | length) > 0 then
                alert("docker_container_oom_kill_increment";"critical";$container_oom_increments;[])
            else empty end,
            if ($current.docker_containers.recent_oom_events // [] | length) > 0 then
                alert("docker_daemon_oom_event";"critical";
                    $current.docker_containers.recent_oom_events;[])
            else empty end,
            if $state.host_iowait_streak >= 3 then
                alert("host_iowait_sustained";"warning";$current.host.cpu.iowait_percent_since_previous;25)
            else empty end,
            if ($current.host.load.procs_blocked | type) == "number"
                and $current.host.load.procs_blocked >= 32 then
                alert("host_many_blocked_processes";"critical";$current.host.load.procs_blocked;32)
            elif $state.host_blocked_streak >= 3 then
                alert("host_many_blocked_processes";"warning";$current.host.load.procs_blocked;8)
            else empty end,
            if $current.moviemuse.running != true then
                alert("moviemuse_not_running";"critical";$current.moviemuse.running;true)
            else empty end,
            if $current.flaresolverr.container.running != true then
                alert("flaresolverr_not_running";"warning";$current.flaresolverr.container.running;true)
            else empty end,
            if $current.moviemuse.oom_killed == true then
                alert("moviemuse_docker_oom_killed";"critical";true;false)
            else empty end,
            if $current.flaresolverr.container.oom_killed == true then
                alert("flaresolverr_docker_oom_killed";"critical";true;false)
            else empty end,
            if $previous.moviemuse.id == $current.moviemuse.id
                and ($previous.moviemuse.restart_count | type) == "number"
                and ($current.moviemuse.restart_count | type) == "number"
                and $current.moviemuse.restart_count > $previous.moviemuse.restart_count then
                alert("moviemuse_restart_count_increment";"warning";
                    ($current.moviemuse.restart_count - $previous.moviemuse.restart_count);0)
            else empty end,
            if $previous.flaresolverr.container.id == $current.flaresolverr.container.id
                and ($previous.flaresolverr.container.restart_count | type) == "number"
                and ($current.flaresolverr.container.restart_count | type) == "number"
                and $current.flaresolverr.container.restart_count
                    > $previous.flaresolverr.container.restart_count then
                alert("flaresolverr_restart_count_increment";"warning";
                    ($current.flaresolverr.container.restart_count
                     - $previous.flaresolverr.container.restart_count);0)
            else empty end,
            if ($current.moviemuse.cgroup.memory_current_bytes | type) == "number"
                and $current.moviemuse.cgroup.memory_current_bytes > 1717986918 then
                alert("moviemuse_memory_current";"critical";
                    $current.moviemuse.cgroup.memory_current_bytes;1717986918)
            elif ($current.moviemuse.cgroup.memory_current_bytes | type) == "number"
                and $current.moviemuse.cgroup.memory_current_bytes > 1288490189 then
                alert("moviemuse_memory_current";"warning";
                    $current.moviemuse.cgroup.memory_current_bytes;1288490189)
            else empty end,
            if ($current.moviemuse.cgroup.stat.anon_bytes | type) == "number"
                and $current.moviemuse.cgroup.stat.anon_bytes > 1288490189 then
                alert("moviemuse_anon";"critical";
                    $current.moviemuse.cgroup.stat.anon_bytes;1288490189)
            elif ($current.moviemuse.cgroup.stat.anon_bytes | type) == "number"
                and $current.moviemuse.cgroup.stat.anon_bytes > 838860800 then
                alert("moviemuse_anon";"warning";
                    $current.moviemuse.cgroup.stat.anon_bytes;838860800)
            else empty end,
            if $previous.moviemuse.id == $current.moviemuse.id
                and ($previous.moviemuse.cgroup.events.oom | type) == "number"
                and ($current.moviemuse.cgroup.events.oom | type) == "number"
                and $current.moviemuse.cgroup.events.oom > $previous.moviemuse.cgroup.events.oom then
                alert("moviemuse_memcg_oom_increment";"critical";
                    ($current.moviemuse.cgroup.events.oom - $previous.moviemuse.cgroup.events.oom);0)
            else empty end,
            if $previous.flaresolverr.container.id == $current.flaresolverr.container.id
                and ($previous.flaresolverr.container.cgroup.events.oom | type) == "number"
                and ($current.flaresolverr.container.cgroup.events.oom | type) == "number"
                and $current.flaresolverr.container.cgroup.events.oom
                    > $previous.flaresolverr.container.cgroup.events.oom then
                alert("flaresolverr_memcg_oom_increment";"critical";
                    ($current.flaresolverr.container.cgroup.events.oom
                     - $previous.flaresolverr.container.cgroup.events.oom);0)
            else empty end,
            if $state.health_failure_streak >= 3 then
                alert("health_failed_three_times";"critical";$state.health_failure_streak;3)
            else empty end,
            if $state.flare_api_failure_streak >= 3 then
                alert("flare_session_api_unavailable";"warning";$state.flare_api_failure_streak;3)
            else empty end,
            if ($current.flaresolverr.sessions.session_count | type) == "number"
                and $current.flaresolverr.sessions.session_count >= 3 then
                alert("flare_multiple_sessions";"critical";
                    $current.flaresolverr.sessions.session_count;1)
            elif ($current.flaresolverr.sessions.session_count | type) == "number"
                and $current.flaresolverr.sessions.session_count > 1 then
                alert("flare_multiple_sessions";"warning";
                    $current.flaresolverr.sessions.session_count;1)
            else empty end,
            if $state.flare_orphan_streak >= 30 then
                alert("flare_orphan_chromium_suspected";"critical";$state.flare_orphan_streak;30)
            elif $state.flare_orphan_streak >= 10 then
                alert("flare_orphan_chromium_suspected";"warning";$state.flare_orphan_streak;10)
            else empty end,
            if $current.moviemuse.running != true
                and ($current.flaresolverr.sessions.moviemuse_session_count | type) == "number"
                and $current.flaresolverr.sessions.moviemuse_session_count > 0 then
                alert("moviemuse_stopped_with_flare_session";"critical";
                    $current.flaresolverr.sessions.moviemuse_session_count;0)
            else empty end,
            if ($state.session_transition_epochs | length) > 20 then
                alert("flare_session_churn";"critical";($state.session_transition_epochs | length);20)
            elif ($state.session_transition_epochs | length) > 6 then
                alert("flare_session_churn";"warning";($state.session_transition_epochs | length);6)
            else empty end,
            if ($current.database.worker_offline_last_hour | type) == "number"
                and $current.database.worker_offline_last_hour > 100 then
                alert("worker_offline_write_rate";"critical";
                    $current.database.worker_offline_last_hour;100)
            elif ($current.database.worker_offline_last_hour | type) == "number"
                and $current.database.worker_offline_last_hour > 10 then
                alert("worker_offline_write_rate";"warning";
                    $current.database.worker_offline_last_hour;10)
            else empty end,
            if ($current.database.worker_offline_payload_max_last_hour_bytes | type) == "number"
                and $current.database.worker_offline_payload_max_last_hour_bytes >= 8192 then
                alert("worker_offline_payload_size";"critical";
                    $current.database.worker_offline_payload_max_last_hour_bytes;8192)
            elif ($current.database.worker_offline_payload_max_last_hour_bytes | type) == "number"
                and $current.database.worker_offline_payload_max_last_hour_bytes > 1024 then
                alert("worker_offline_payload_size";"warning";
                    $current.database.worker_offline_payload_max_last_hour_bytes;1024)
            else empty end,
            if ($current.database.event_count | type) == "number"
                and $current.database.event_count >= 50000 then
                alert("task_events_near_retention_limit";"critical";$current.database.event_count;50000)
            elif ($current.database.event_count | type) == "number"
                and $current.database.event_count >= 45000 then
                alert("task_events_near_retention_limit";"warning";$current.database.event_count;45000)
            else empty end,
            if ($current.database.files.main.bytes | type) == "number"
                and ($state.db_baseline_bytes | type) == "number"
                and ($current.database.files.main.bytes - $state.db_baseline_bytes) > 20971520 then
                alert("sqlite_daily_growth";"critical";
                    ($current.database.files.main.bytes - $state.db_baseline_bytes);20971520)
            elif ($current.database.files.main.bytes | type) == "number"
                and ($state.db_baseline_bytes | type) == "number"
                and ($current.database.files.main.bytes - $state.db_baseline_bytes) > 5242880 then
                alert("sqlite_daily_growth";"warning";
                    ($current.database.files.main.bytes - $state.db_baseline_bytes);5242880)
            else empty end,
            if $state.wal_growth_streak >= 3 then
                alert("sqlite_wal_sustained_growth";"warning";$state.wal_growth_streak;3)
            else empty end,
            if $current.database.javdb_source_enabled == false
                and ($current.moviemuse.processes.javdb_chromium_count | type) == "number"
                and $current.moviemuse.processes.javdb_chromium_count > 0 then
                alert("javdb_disabled_but_browser_present";"critical";
                    $current.moviemuse.processes.javdb_chromium_count;0)
            else empty end,
            if $current.moviemuse.health.subtitle_mode == "remote"
                and $current.compute_worker.online == false
                and $current.moviemuse.processes.heavy_model_hint == true then
                alert("remote_offline_with_local_model_hint";"critical";true;false)
            else empty end,
            if $current.compute_worker.configured == true
                and $current.compute_worker.online == false then
                alert("remote_worker_offline";"warning";false;true)
            else empty end,
            if $current.sample.deep == true
                and $current.database.aggregate_status != "ok" then
                alert("sqlite_aggregate_unavailable";"warning";
                    $current.database.aggregate_status;"ok")
            else empty end,
            if $current.database.quick_check.attempted == true
                and $current.database.quick_check.status != "ok" then
                alert("sqlite_quick_check_failed";"critical";
                    $current.database.quick_check.status;"ok")
            else empty end,
            if ((($previous.host.kernel_oom_signal.global_candidate_pattern_count | type) == "number"
                    and ($current.host.kernel_oom_signal.global_candidate_pattern_count | type) == "number"
                    and $current.host.kernel_oom_signal.global_candidate_pattern_count
                        > $previous.host.kernel_oom_signal.global_candidate_pattern_count)
                or (($previous.host.kernel_oom_signal.latest_signature // "") != ""
                    and ($current.host.kernel_oom_signal.latest_signature // "") != ""
                    and $previous.host.kernel_oom_signal.latest_signature
                        != $current.host.kernel_oom_signal.latest_signature
                    and $current.host.kernel_oom_signal.latest_kind == "global_candidate")) then
                alert("host_global_oom_candidate";"critical";
                    $current.host.kernel_oom_signal.latest_signature;"unchanged")
            elif ($previous.host.kernel_oom_signal.anomaly_pattern_count | type) == "number"
                and ($current.host.kernel_oom_signal.anomaly_pattern_count | type) == "number"
                and $current.host.kernel_oom_signal.anomaly_pattern_count
                    > $previous.host.kernel_oom_signal.anomaly_pattern_count then
                alert("host_kernel_anomaly";"critical";
                    $current.host.kernel_oom_signal.latest_kind;"unchanged")
            else empty end
        ]
    '
)"

END_MS="$(epoch_ms)"
DURATION_MS=$((END_MS - START_MS))
((DURATION_MS >= 0)) || DURATION_MS=0

FINAL_SAMPLE="$(
    jq -nc --argjson sample "$BASE_SAMPLE" --argjson state "$MONITOR_STATE" \
        --argjson events "$EVENTS_JSON" --argjson alerts "$ALERTS_JSON" \
        --argjson duration_ms "$DURATION_MS" '
        $sample |
        .sample.duration_ms = $duration_ms |
        .monitor_state = $state |
        .events = $events |
        .alerts = $alerts
    '
)"

if ! jq -e '
    type == "object"
    and .schema_version == 2
    and .record_type == "health_sample"
    and (.epoch | type) == "number"
    and (.moviemuse | type) == "object"
    and (.flaresolverr | type) == "object"
    and (.docker_containers | type) == "object"
    and (.virtual_machines | type) == "object"
    and (.database | type) == "object"
    and (.alerts | type) == "array"
    and (.events | type) == "array"
' >/dev/null 2>&1 <<<"$FINAL_SAMPLE"; then
    write_error "sample assembly failed schema validation; no sample written"
    exit 1
fi

if [[ "$MODE" == "probe" ]]; then
    printf '%s\n' "$FINAL_SAMPLE"
    exit 0
fi

NEW_ALERTS="$(
    jq -nc --argjson current "$FINAL_SAMPLE" --argjson previous "$PREVIOUS_SAMPLE" '
        ($previous.alerts // [] | map(.code)) as $old_codes |
        [$current.alerts[] | select(.code as $code | ($old_codes | index($code) | not))]
    '
)"
SIGNIFICANT_EVENT_COUNT="$(
    jq '[.events[] | select(
        (.kind | endswith("_identity_change")) or
        (.kind == "sample_gap") or (.kind == "host_reboot") or
        (.kind == "virtual_machine_set_changed") or
        (.kind == "docker_container_oom_kill") or (.kind == "docker_daemon_oom_event")
    )] | length' <<<"$FINAL_SAMPLE"
)"
NEW_ALERT_COUNT="$(jq 'length' <<<"$NEW_ALERTS")"

if ! printf '%s\n' "$FINAL_SAMPLE" >>"$SAMPLES_DIR/health-$TODAY_UTC.jsonl"; then
    write_error "failed to append daily sample"
    exit 1
fi
if [[ "$(jq '.events | length' <<<"$FINAL_SAMPLE")" != "0" ]]; then
    if ! jq -c --arg schema_version "$SCHEMA_VERSION" '.events[] |
        {schema_version:($schema_version | tonumber),record_type:"monitor_event"} + .' \
        <<<"$FINAL_SAMPLE" >>"$EVENTS_DIR/events-$TODAY_UTC.jsonl"; then
        write_error "failed to append monitor events"
    fi
fi

if ((NEW_ALERT_COUNT > 0 || SIGNIFICANT_EVENT_COUNT > 0)); then
    severity="$(
        jq -r '
            if any(.[]; .severity == "critical") then "critical"
            elif any(.[]; .severity == "warning") then "warning"
            else "info"
            end
        ' <<<"$NEW_ALERTS"
    )"
    primary_code="$(jq -r '.[0].code // empty' <<<"$NEW_ALERTS")"
    if [[ -z "$primary_code" ]]; then
        primary_code="$(jq -r '.events[0].kind // "monitor-event"' <<<"$FINAL_SAMPLE")"
    fi
    primary_code="$(tr -cd 'A-Za-z0-9_.-' <<<"$primary_code")"
    [[ -n "$primary_code" ]] || primary_code="monitor-event"
    compact_timestamp="$(date -u +'%Y%m%dT%H%M%SZ')"
    incident_file="$INCIDENTS_DIR/incident-$compact_timestamp-$severity-$primary_code.json"
    [[ ! -e "$incident_file" ]] || incident_file="$INCIDENTS_DIR/incident-$compact_timestamp-$severity-$primary_code-$$.json"
    LOG_SUMMARY="$(log_summary_json)"
    KERNEL_EVIDENCE="$(kernel_incident_evidence_json)"
    if jq -nc --arg schema_version "$SCHEMA_VERSION" \
        --arg incident_id "$(basename "$incident_file" .json)" \
        --arg timestamp "$NOW_ISO" --argjson epoch "$NOW_EPOCH" \
        --arg severity "$severity" --argjson triggers "$NEW_ALERTS" \
        --argjson current "$FINAL_SAMPLE" --argjson previous "$PREVIOUS_SAMPLE" \
        --argjson log_summary "$LOG_SUMMARY" --argjson kernel_evidence "$KERNEL_EVIDENCE" '
        {
            schema_version:($schema_version | tonumber),
            record_type:"incident",
            incident_id:$incident_id,
            timestamp:$timestamp,
            epoch:$epoch,
            severity:$severity,
            triggers:$triggers,
            events:$current.events,
            current:$current,
            previous:(
                if ($previous | type) == "object" and ($previous.timestamp // "") != ""
                then $previous else null end
            ),
            log_summary:$log_summary,
            kernel_evidence:$kernel_evidence
        }
    ' >"$STATE_DIR/.incident.$$.tmp"; then
        mv -f "$STATE_DIR/.incident.$$.tmp" "$incident_file" ||
            write_error "failed to finalize incident snapshot"
    else
        rm -f "$STATE_DIR/.incident.$$.tmp"
        write_error "failed to assemble incident snapshot"
    fi
fi

if printf '%s\n' "$FINAL_SAMPLE" >"$STATE_DIR/.previous-sample.$$.tmp"; then
    if ! mv -f "$STATE_DIR/.previous-sample.$$.tmp" "$STATE_DIR/previous-sample.json"; then
        write_error "failed to finalize previous-sample state"
        exit 1
    fi
else
    write_error "failed to write previous-sample state"
    exit 1
fi

if ((DURATION_MS > 5000)); then
    write_error "collector duration ${DURATION_MS}ms exceeded 5000ms target"
fi

exit 0
