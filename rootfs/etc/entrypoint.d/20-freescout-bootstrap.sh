#!/bin/sh
# FreeScout bootstrap — runs once before s6 starts nginx + php-fpm.
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

# ---------------------------------------------------------------------------
# 1. Validate required env, map DB_TYPE -> DB_CONNECTION, default DB_PORT.
# ---------------------------------------------------------------------------
: "${APP_KEY:?APP_KEY is required}"
: "${APP_URL:?APP_URL is required}"
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

# Ops-managed keys: always set, sentinels do NOT apply.
write_env_key APP_KEY        "$APP_KEY"        "$ENV_FILE"
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
# 6. Wait for DB. 30s deadline, fail fast on timeout.
# ---------------------------------------------------------------------------
log "waiting for $DB_CONNECTION at $DB_HOST:$DB_PORT (30s deadline)"
deadline=$(( $(date +%s) + 30 ))
while :; do
    case "$DB_CONNECTION" in
        pgsql)
            if pg_isready -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" >/dev/null 2>&1; then
                break
            fi
            ;;
        mysql)
            if mysqladmin ping -h "$DB_HOST" -P "$DB_PORT" --silent >/dev/null 2>&1; then
                break
            fi
            ;;
    esac
    if [ "$(date +%s)" -ge "$deadline" ]; then
        die "DB at $DB_HOST:$DB_PORT not reachable within 30s"
    fi
    sleep 1
done
log "DB is reachable"

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
#    and module post-update hooks. Must succeed; non-zero is fatal.
# ---------------------------------------------------------------------------
log "running freescout:after-app-update"
if ! ( cd "$APP_DIR" && php artisan freescout:after-app-update ) >&2; then
    die "freescout:after-app-update failed (migrations did not complete)"
fi

# ---------------------------------------------------------------------------
# 9. Seed admin if first boot and ADMIN_EMAIL is set.
#    Query the DB directly with the installed client — failures are fatal.
# ---------------------------------------------------------------------------
if [ -n "${ADMIN_EMAIL:-}" ]; then
    case "$DB_CONNECTION" in
        pgsql)
            user_count=$(PGPASSWORD="$DB_PASS" psql \
                -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" \
                -tA -c "SELECT COUNT(*) FROM users;") || \
                die "could not query users table count (psql)"
            ;;
        mysql)
            user_count=$(MYSQL_PWD="$DB_PASS" mysql \
                -h "$DB_HOST" -P "$DB_PORT" -u "$DB_USER" -D "$DB_NAME" \
                -N -B -e "SELECT COUNT(*) FROM users;") || \
                die "could not query users table count (mysql)"
            ;;
    esac
    user_count=$(printf '%s' "$user_count" | tr -d '[:space:]')
    case "$user_count" in
        ''|*[!0-9]*)
            die "unexpected users-count result: '$user_count'"
            ;;
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
