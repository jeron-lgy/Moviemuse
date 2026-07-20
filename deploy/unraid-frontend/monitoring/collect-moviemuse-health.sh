#!/usr/bin/env bash
#
# MovieMuse temporary Unraid host monitor.
#
# This script observes MovieMuse, FlareSolverr, cgroup v2 and the subscription
# SQLite database. It never restarts containers, destroys sessions or writes to
# the application data directory.

set -u
umask 077

readonly SCHEMA_VERSION="1"
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
    awk basename cat cksum curl date df docker find flock grep hostname jq mkdir mv
    readlink rm sed sort sqlite3 stat tail tr wc
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

host_json() {
    local run_deep="$1"
    local mem_total="" mem_available="" swap_total="" swap_free="" vmstat_oom=""
    [[ -r /proc/meminfo ]] && {
        mem_total="$(awk '/^MemTotal:/ {print $2 * 1024; exit}' /proc/meminfo)"
        mem_available="$(awk '/^MemAvailable:/ {print $2 * 1024; exit}' /proc/meminfo)"
        swap_total="$(awk '/^SwapTotal:/ {print $2 * 1024; exit}' /proc/meminfo)"
        swap_free="$(awk '/^SwapFree:/ {print $2 * 1024; exit}' /proc/meminfo)"
    }
    [[ -r /proc/vmstat ]] && vmstat_oom="$(awk '$1 == "oom_kill" {print $2; exit}' /proc/vmstat)"

    local kernel_available=false kernel_memcg_count="" kernel_global_count=""
    local kernel_signature="" kernel_kind="unknown" matches="" latest=""
    if [[ -r /var/log/syslog ]]; then
        kernel_available=true
        matches="$(
            tail -n 500 /var/log/syslog 2>/dev/null |
                grep -Ei 'invoked oom-killer|Out of memory: Killed process|Memory cgroup out of memory|oom-kill:' || true
        )"
        kernel_memcg_count="$(grep -Eic 'memory cgroup|CONSTRAINT_MEMCG|task_memcg=' <<<"$matches" || true)"
        kernel_global_count="$(grep -Eic 'invoked oom-killer|Out of memory: Killed process' <<<"$matches" || true)"
        latest="$(tail -n 1 <<<"$matches")"
        if [[ -n "$latest" ]]; then
            kernel_signature="$(cksum <<<"$latest" | awk '{print $1 ":" $2}')"
            if grep -Eiq 'memory cgroup|CONSTRAINT_MEMCG|task_memcg=' <<<"$latest"; then
                kernel_kind="memcg"
            elif grep -Eiq 'invoked oom-killer|Out of memory: Killed process' <<<"$latest"; then
                kernel_kind="global_candidate"
            fi
        fi
    fi

    local docker_checked=false docker_cgroup_version="" docker_no_swap_limit=""
    if [[ "$run_deep" == "1" ]]; then
        docker_checked=true
        local docker_info_result docker_info_rc
        docker_info_result="$(
            docker info --format '{{.CgroupVersion}}|{{json .Warnings}}' 2>&1
        )"
        docker_info_rc=$?
        if [[ "$docker_info_rc" == "0" ]]; then
            docker_cgroup_version="${docker_info_result%%|*}"
            docker_no_swap_limit=false
            if grep -Fqi 'no swap limit support' <<<"$docker_info_result"; then
                docker_no_swap_limit=true
            fi
        fi
    fi

    jq -nc --arg hostname "$(hostname)" \
        --arg mem_total "$mem_total" --arg mem_available "$mem_available" \
        --arg swap_total "$swap_total" --arg swap_free "$swap_free" \
        --arg vmstat_oom "$vmstat_oom" --argjson kernel_available "$kernel_available" \
        --arg kernel_memcg_count "$kernel_memcg_count" \
        --arg kernel_global_count "$kernel_global_count" \
        --arg kernel_signature "$kernel_signature" --arg kernel_kind "$kernel_kind" \
        --argjson docker_checked "$docker_checked" \
        --arg docker_cgroup_version "$docker_cgroup_version" \
        --arg docker_no_swap_limit "$docker_no_swap_limit" '
        def number_or_null($value):
            if ($value | test("^[0-9]+$")) then ($value | tonumber) else null end;
        {
            hostname:$hostname,
            memory_total_bytes:number_or_null($mem_total),
            memory_available_bytes:number_or_null($mem_available),
            swap_total_bytes:number_or_null($swap_total),
            swap_free_bytes:number_or_null($swap_free),
            vmstat_oom_kill:number_or_null($vmstat_oom),
            docker:{
                checked:$docker_checked,
                cgroup_version:(
                    if $docker_cgroup_version == "" then null else $docker_cgroup_version end
                ),
                no_swap_limit_support:(
                    if $docker_no_swap_limit == "true" then true
                    elif $docker_no_swap_limit == "false" then false
                    else null
                    end
                )
            },
            kernel_oom_signal:{
                source:(if $kernel_available then "/var/log/syslog-tail" else null end),
                available:$kernel_available,
                memcg_pattern_count:number_or_null($kernel_memcg_count),
                global_candidate_pattern_count:number_or_null($kernel_global_count),
                latest_signature:(if $kernel_signature == "" then null else $kernel_signature end),
                latest_kind:(if $kernel_signature == "" then null else $kernel_kind end)
            }
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
    if [[ -f "$sample_file" ]] && (( $(stat -c '%s' "$sample_file" 2>/dev/null || printf 0) > 16777216 )); then
        write_error "daily sample file exceeded 16 MiB; sample skipped"
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
        --argjson database "$DB_JSON" --argjson worker "$WORKER_JSON" '
        {
            schema_version:($schema_version | tonumber),
            record_type:"health_sample",
            timestamp:$timestamp,
            epoch:$epoch,
            host:$host_metrics,
            sample:{deep:$deep_sample,duration_ms:null},
            moviemuse:($moviemuse + {health:$health}),
            flaresolverr:$flaresolverr,
            database:$database,
            compute_worker:$worker
        }
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
                event("kernel_oom_signal_changed"; {
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
            if ($previous.host.kernel_oom_signal.latest_signature // "") != ""
                and ($current.host.kernel_oom_signal.latest_signature // "") != ""
                and $previous.host.kernel_oom_signal.latest_signature
                    != $current.host.kernel_oom_signal.latest_signature
                and $current.host.kernel_oom_signal.latest_kind == "global_candidate" then
                alert("host_global_oom_candidate";"critical";
                    $current.host.kernel_oom_signal.latest_signature;"unchanged")
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
    and .schema_version == 1
    and .record_type == "health_sample"
    and (.epoch | type) == "number"
    and (.moviemuse | type) == "object"
    and (.flaresolverr | type) == "object"
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
IDENTITY_EVENT_COUNT="$(jq '[.events[] | select(.kind | endswith("_identity_change"))] | length' <<<"$FINAL_SAMPLE")"
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

if ((NEW_ALERT_COUNT > 0 || IDENTITY_EVENT_COUNT > 0)); then
    severity="$(
        jq -r '
            if any(.[]; .severity == "critical") then "critical"
            elif any(.[]; .severity == "warning") then "warning"
            else "info"
            end
        ' <<<"$NEW_ALERTS"
    )"
    primary_code="$(jq -r '.[0].code // "identity-change"' <<<"$NEW_ALERTS")"
    primary_code="$(tr -cd 'A-Za-z0-9_.-' <<<"$primary_code")"
    [[ -n "$primary_code" ]] || primary_code="monitor-event"
    compact_timestamp="$(date -u +'%Y%m%dT%H%M%SZ')"
    incident_file="$INCIDENTS_DIR/incident-$compact_timestamp-$severity-$primary_code.json"
    [[ ! -e "$incident_file" ]] || incident_file="$INCIDENTS_DIR/incident-$compact_timestamp-$severity-$primary_code-$$.json"
    LOG_SUMMARY="$(log_summary_json)"
    if jq -nc --arg schema_version "$SCHEMA_VERSION" \
        --arg incident_id "$(basename "$incident_file" .json)" \
        --arg timestamp "$NOW_ISO" --argjson epoch "$NOW_EPOCH" \
        --arg severity "$severity" --argjson triggers "$NEW_ALERTS" \
        --argjson current "$FINAL_SAMPLE" --argjson previous "$PREVIOUS_SAMPLE" \
        --argjson log_summary "$LOG_SUMMARY" '
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
            log_summary:$log_summary
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
