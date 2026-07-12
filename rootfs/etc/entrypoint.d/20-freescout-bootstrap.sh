#!/bin/sh
# FreeScout bootstrap — s6 init oneshot. nginx + php-fpm start in parallel
# and serve 5xx until this completes; the scheduler longrun waits on it.
# POSIX sh. Pipelines are avoided so artisan exit status is never masked.
set -eu

APP_DIR=/var/www/html
ENV_FILE=/data/config

log() { printf '[freescout-bootstrap] %s\n' "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

# ---------------------------------------------------------------------------
# Pre-flight guard: refuse the /data/config-as-directory layout.
# ---------------------------------------------------------------------------
if [ -d /data/config ]; then
    echo "ERROR: /data/config is a directory. This image expects /data/config to be a regular .env file (old tiredofit layout)." >&2
    echo "       The /data/config/config directory layout is not supported. Migrate by moving /data/config/config to /data/config." >&2
    exit 1
fi

# Preflight writability check. Without this, an unchowned bind-mount surfaces
# as a bare `mkdir: Permission denied` deep in the boot sequence and the
# container crash-loops with no actionable hint. Use redirection rather than
# `touch` so a pre-existing unwritable `.write-test` still trips the guard.
if ! ( : > /data/.write-test ) 2>/dev/null; then
    cat >&2 <<EOF
ERROR: /data is not writable by the container (container UID:GID $(id -u):$(id -g)).
       Ownership of the bind-mount target must match how the container sees it.
       The right fix depends on your runtime — see README "User & permissions":
         - rootful docker/podman bind mount: chown the host dir to $(id -u):$(id -g)
         - rootless podman: add --userns=keep-id:uid=$(id -u),gid=$(id -g)
         - or use a named volume
         - or rebuild with --build-arg WWW_DATA_UID=...
EOF
    exit 1
fi
rm -f /data/.write-test

# ---------------------------------------------------------------------------
# 1. Validate required env, map DB_TYPE -> DB_CONNECTION, default DB_PORT.
# ---------------------------------------------------------------------------
APP_KEY=${APP_KEY:-}
# SITE_URL is accepted as a legacy alias for tiredofit drop-in compat.
APP_URL=${APP_URL:-${SITE_URL:-}}
: "${APP_URL:?APP_URL (or legacy SITE_URL) is required}"
: "${DB_HOST:?DB_HOST is required}"
: "${DB_NAME:?DB_NAME is required}"
: "${DB_USER:?DB_USER is required}"
# DB_PASS may be empty (passwordless local dev DBs); don't enforce.
DB_PASS=${DB_PASS:-}

DB_TYPE_RAW=${DB_TYPE:-pgsql}
case "$DB_TYPE_RAW" in
    pgsql|postgres|postgresql)
        DB_CONNECTION=pgsql
        DB_PORT_DEFAULT=5432
        ;;
    mysql|mariadb)
        DB_CONNECTION=mysql
        DB_PORT_DEFAULT=3306
        ;;
    *)
        die "unsupported DB_TYPE='$DB_TYPE_RAW' (expected pgsql|mysql|mariadb)"
        ;;
esac
DB_PORT=${DB_PORT:-$DB_PORT_DEFAULT}

# DB_WAIT_TIMEOUT: seconds to wait for the DB per wait episode (initial wait
# and each mid-boot re-wait). 0 = wait forever (default) — self-recovering on
# platforms where a stopped container is not restarted. >0 = fail fast; pair
# with a restart policy that retries.
DB_WAIT_TIMEOUT=${DB_WAIT_TIMEOUT:-0}
case "$DB_WAIT_TIMEOUT" in
    ''|*[!0-9]*) die "DB_WAIT_TIMEOUT must be a non-negative integer (got '$DB_WAIT_TIMEOUT')" ;;
esac

# ---------------------------------------------------------------------------
# 1b. Clean up the broken /data/storage/logs symlink left over from old
#     tiredofit installs (storage/logs -> /logs/laravel/). `mkdir -p` follows
#     symlinks and fails when the target is missing, killing the container
#     at boot. Only act on a dangling link; a live symlink stays.
# ---------------------------------------------------------------------------
if [ -L /data/storage/logs ] && [ ! -e /data/storage/logs ]; then
    log "removing broken symlink: /data/storage/logs -> $(readlink /data/storage/logs)"
    rm -f /data/storage/logs
fi

# ---------------------------------------------------------------------------
# 2. Ensure /data tree exists. Idempotent.
# ---------------------------------------------------------------------------
mkdir -p \
    /data/Modules \
    /data/storage/cache \
    /data/storage/sessions \
    /data/storage/framework/cache \
    /data/storage/framework/sessions \
    /data/storage/framework/views \
    /data/storage/framework/testing \
    /data/storage/views \
    /data/storage/logs \
    /data/storage/app/public

# Seed storage/app/public/.gitignore. FreeScout's System Status check reads this
# file *through* the public/storage symlink (public/storage/.gitignore ->
# /data/storage/app/public/.gitignore) and demands non-empty content; a missing
# file trips a spurious "Create symlink manually" warning even though the symlink
# is valid. Upstream ships it in storage/app/public/ but the image rm -rf's that
# tree before symlinking, so we replant it here. Idempotent: only write if absent
# or empty (-s). Never clobber existing content.
if [ ! -s /data/storage/app/public/.gitignore ]; then
    log "seeding /data/storage/app/public/.gitignore"
    printf '*\n!.gitignore\n' > /data/storage/app/public/.gitignore
fi

# ---------------------------------------------------------------------------
# 3. Patch /data/config (the .env). User state — never rewritten wholesale.
# ---------------------------------------------------------------------------
# write_env_key: unconditional set. Used for ops-managed keys; empty values
# stay empty (do NOT delete). Required ops vars are validated above.
write_env_key() {
    key=$1; val=$2; file=$3
    awk -v k="$key" -v v="$val" '
        BEGIN { found = 0 }
        $0 ~ "^"k"=" { print k"="v; found = 1; next }
        { print }
        END { if (!found) print k"="v }
    ' "$file" > "$file.tmp" && mv "$file.tmp" "$file"
}

# set_env_key: validated key + sentinel deletion. Used for FREESCOUT_*
# passthrough only — operator may have hand-set the key and expect to be
# able to clear it via env.
# Key must match [A-Z0-9_]+ — keeps the awk regex `^"k"=` safe from
# user-supplied metachars.
set_env_key() {
    key=$1; val=$2; file=$3
    case "$key" in
        *[!A-Z0-9_]*|"")
            log "skip invalid env key: '$key'"
            return 0
            ;;
    esac
    case "$val" in
        unset|null|"")
            delete_env_key "$key" "$file"
            return $?
            ;;
    esac
    write_env_key "$key" "$val" "$file"
}

delete_env_key() {
    key=$1; file=$2
    awk -v k="$key" '$0 !~ "^"k"="' "$file" > "$file.tmp" && mv "$file.tmp" "$file"
}

# Seed a minimal .env on first boot.
if [ ! -f "$ENV_FILE" ]; then
    log "seeding new $ENV_FILE"
    : > "$ENV_FILE"
fi

# APP_KEY resolution: env override -> existing /data/config value -> generate.
# write_env_key only runs in the override branch so we don't clobber a
# Laravel-written value on subsequent boots.
existing_app_key=$(awk -F= '/^APP_KEY=/ { sub(/^APP_KEY=/,""); v=$0 } END { print v }' "$ENV_FILE")

if [ -n "$APP_KEY" ]; then
    write_env_key APP_KEY "$APP_KEY" "$ENV_FILE"
elif [ -n "$existing_app_key" ]; then
    log "APP_KEY: using existing value from $ENV_FILE"
else
    log "APP_KEY: generating via php artisan key:generate"
    # key:generate uses preg_replace on an existing APP_KEY= line; seed an
    # empty one if missing so the substitution lands.
    grep -q '^APP_KEY=' "$ENV_FILE" || printf 'APP_KEY=\n' >> "$ENV_FILE"
    ( cd "$APP_DIR" && php artisan key:generate --force --no-interaction ) >&2 \
        || die "php artisan key:generate failed"
fi

# Ops-managed keys: always set from env, sentinels do NOT apply.
write_env_key APP_URL        "$APP_URL"        "$ENV_FILE"
write_env_key DB_CONNECTION  "$DB_CONNECTION"  "$ENV_FILE"
write_env_key DB_HOST        "$DB_HOST"        "$ENV_FILE"
write_env_key DB_PORT        "$DB_PORT"        "$ENV_FILE"
write_env_key DB_DATABASE    "$DB_NAME"        "$ENV_FILE"
write_env_key DB_USERNAME    "$DB_USER"        "$ENV_FILE"
write_env_key DB_PASSWORD    "$DB_PASS"        "$ENV_FILE"

# FREESCOUT_* passthrough — strip prefix, patch into .env. Set-through-once:
# removing the env var later does not clear the file value (use sentinel
# `unset|null|""` to delete).
# Use `env -0` (NUL-separated; safe for values containing newlines). Do NOT
# use /proc/self/environ — inside this pipeline `self` resolves to the helper
# process (tr / busybox), not the bootstrap shell, so its environ is empty.
env -0 | tr '\0' '\n' | while IFS= read -r entry; do
    case "$entry" in
        FREESCOUT_*=*)
            kv=${entry#FREESCOUT_}
            key=${kv%%=*}
            val=${kv#*=}
            set_env_key "$key" "$val" "$ENV_FILE"
            ;;
    esac
done

# ---------------------------------------------------------------------------
# 4. Symlinks already created at build time. Nothing to do.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 5. Laravel storage:link (idempotent — exits 0 if link already exists).
#    Streamed directly to stderr; no pipeline to mask exit status.
# ---------------------------------------------------------------------------
( cd "$APP_DIR" && php artisan storage:link ) >&2 || \
    log "WARN: php artisan storage:link returned non-zero"

# ---------------------------------------------------------------------------
# 6. Wait for DB. Readiness = Laravel can run a query (freescout-db-guard
#    ping), not just an open port — pg_isready/mysqladmin only gate the PHP
#    boot cost and validate neither credentials nor database existence.
#    DB_WAIT_TIMEOUT=0 (default) waits forever: the observed failure mode is
#    platforms without a restart policy, where fail-fast means staying dead.
#    An indefinitely-waiting oneshot is safe under s6: the only stage-2
#    timeout is S6_CMD_WAIT_FOR_SERVICES_MAXTIME, which the base sets to 0.
# ---------------------------------------------------------------------------
port_open() {
    case "$DB_CONNECTION" in
        pgsql) pg_isready -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" >/dev/null 2>&1 ;;
        mysql) mysqladmin ping -h "$DB_HOST" -P "$DB_PORT" --silent >/dev/null 2>&1 ;;
    esac
}

wait_for_db() {
    wait_start=$(date +%s)
    next_log=0
    while :; do
        last_err="TCP endpoint $DB_HOST:$DB_PORT not accepting connections"
        if port_open; then
            ping_rc=0
            ping_err=$( cd "$APP_DIR" && freescout-db-guard ping 2>&1 ) || ping_rc=$?
            case "$ping_rc" in
                0) log "DB ready (Laravel query succeeded)"; return 0 ;;
                3) last_err=$ping_err ;;
                4) last_err="$ping_err — server rejected the connection; check DB_USER/DB_PASS/DB_NAME if this persists" ;;
                *) log "$ping_err"; die "freescout-db-guard ping crashed (exit $ping_rc)" ;;
            esac
        fi
        now=$(date +%s)
        elapsed=$(( now - wait_start ))
        if [ "$DB_WAIT_TIMEOUT" -gt 0 ] && [ "$elapsed" -ge "$DB_WAIT_TIMEOUT" ]; then
            die "DB not ready after ${elapsed}s (DB_WAIT_TIMEOUT=$DB_WAIT_TIMEOUT); last error: $last_err"
        fi
        if [ "$now" -ge "$next_log" ]; then
            log "waiting for $DB_CONNECTION at $DB_HOST:$DB_PORT (${elapsed}s elapsed, timeout=${DB_WAIT_TIMEOUT}s): $last_err"
            next_log=$(( now + 15 ))
        fi
        sleep 2
    done
}

wait_for_db

# ---------------------------------------------------------------------------
# 6b. Preflight: refuse to migrate against a non-FreeScout database.
#     Empty DB and an existing FreeScout DB both pass; anything else aborts.
#     Exit 3/4 = the DB dropped between wait and preflight — re-wait and
#     retry, bounded so a flapping DB with DB_WAIT_TIMEOUT>0 still terminates.
#     `pf_rc=0` is reset *before* the call to avoid leaking a stale value —
#     `||` only fires on non-zero.
# ---------------------------------------------------------------------------
log "preflight: checking DB is empty or FreeScout-owned"
pf_tries=0
while :; do
    pf_rc=0
    ( cd "$APP_DIR" && freescout-db-guard preflight ) || pf_rc=$?
    case "$pf_rc" in
        0) break ;;
        1) exit 1 ;;   # wrong-DB refusal; guard already printed an actionable error
        3|4)
            pf_tries=$(( pf_tries + 1 ))
            [ "$pf_tries" -le 5 ] || die "DB kept dropping during preflight (5 attempts)"
            log "DB connection lost during preflight (exit $pf_rc); re-waiting"
            sleep 2
            wait_for_db
            ;;
        *) die "freescout-db-guard preflight crashed (exit $pf_rc)" ;;
    esac
done
unset pf_rc pf_tries

# ---------------------------------------------------------------------------
# 7. Install user modules. One alias at a time, no --force.
# ---------------------------------------------------------------------------
if [ -d /data/Modules ]; then
    for mod_dir in /data/Modules/*/; do
        [ -d "$mod_dir" ] || continue
        alias=""
        if [ -f "${mod_dir}module.json" ]; then
            alias=$(awk -F'"' '/"alias"[[:space:]]*:/ { print $4; exit }' "${mod_dir}module.json")
        fi
        if [ -z "$alias" ]; then
            alias=$(basename "$mod_dir" | tr 'A-Z' 'a-z')
        fi
        log "installing module: $alias"
        if ! ( cd "$APP_DIR" && php artisan freescout:module-install "$alias" ) >&2; then
            log "WARN: module-install $alias returned non-zero (already installed?)"
        fi
    done
fi

# ---------------------------------------------------------------------------
# 8. freescout:after-app-update — runs migrations, clears cache, queue:restart,
#    and module post-update hooks. Must succeed. Artisan's exit code cannot
#    distinguish a connection drop from a migration bug, so on failure ask the
#    guard: ping OK means the DB is fine and the failure was real (fatal);
#    ping 3/4 means the DB dropped mid-run — re-wait and retry (the command is
#    designed to be re-runnable every boot). Bounded at 3 runs.
# ---------------------------------------------------------------------------
log "running freescout:after-app-update"
aau_attempt=1
while ! ( cd "$APP_DIR" && php artisan freescout:after-app-update ) >&2; do
    aau_ping_rc=0
    ( cd "$APP_DIR" && freescout-db-guard ping ) >/dev/null 2>&1 || aau_ping_rc=$?
    case "$aau_ping_rc" in
        3|4) ;;
        *) die "freescout:after-app-update failed (migrations did not complete)" ;;
    esac
    aau_attempt=$(( aau_attempt + 1 ))
    [ "$aau_attempt" -le 3 ] || die "freescout:after-app-update failed 3 times with the DB dropping mid-run"
    log "DB connection lost during after-app-update; re-waiting (attempt $aau_attempt/3)"
    wait_for_db
done
unset aau_attempt aau_ping_rc

# ---------------------------------------------------------------------------
# 9. Seed admin if first boot and ADMIN_EMAIL is set.
#    users-count goes through the PHP guard so the bootstrap stays
#    driver-agnostic — all Laravel-aware DB logic lives in one place.
# ---------------------------------------------------------------------------
if [ -n "${ADMIN_EMAIL:-}" ]; then
    # Exit 3/4 = connection dropped after migrations — re-wait, retry once.
    # create-user failure below stays fatal (tiny window, artisan's exit code
    # is unclassifiable).
    uc_tries=0
    while :; do
        uc_rc=0
        user_count=$(cd "$APP_DIR" && freescout-db-guard users-count) || uc_rc=$?
        case "$uc_rc" in
            0) break ;;
            3|4)
                uc_tries=$(( uc_tries + 1 ))
                [ "$uc_tries" -le 1 ] || die "freescout-db-guard users-count failed (DB dropped twice)"
                log "DB connection lost during users-count (exit $uc_rc); re-waiting"
                wait_for_db
                ;;
            *) die "freescout-db-guard users-count failed (exit $uc_rc)" ;;
        esac
    done
    unset uc_rc uc_tries
    case "$user_count" in
        ''|*[!0-9]*) die "unexpected users-count output: '$user_count'" ;;
    esac
    if [ "$user_count" -eq 0 ]; then
        log "seeding admin user $ADMIN_EMAIL"
        : "${ADMIN_PASS:?ADMIN_PASS required when ADMIN_EMAIL is set}"
        if ! ( cd "$APP_DIR" && php artisan freescout:create-user \
                --role=admin \
                --email="$ADMIN_EMAIL" \
                --password="$ADMIN_PASS" \
                --firstName="${ADMIN_FIRST_NAME:-Admin}" \
                --lastName="${ADMIN_LAST_NAME:-User}" ) >&2; then
            die "admin create-user failed"
        fi
    else
        log "users table not empty (count=$user_count); skipping admin seed"
    fi
fi

log "bootstrap complete"
