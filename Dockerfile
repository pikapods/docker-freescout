# FreeScout image — self-maintained, derived from serversideup/php.
# See README.md for design notes and usage.
#
# Build (FREESCOUT_VERSION is required — there is deliberately no default, so a
# build can never silently produce a stale version):
#   podman build \
#     --build-arg FREESCOUT_VERSION=1.8.232 \
#     --build-arg PHP_VERSION=8.4 \
#     -t ghcr.io/pikapods/docker-freescout:1.8.232 .
#
# CI also passes BASE_IMAGE (digest-pinned), BASE_DIGEST, IMAGE_REVISION,
# GIT_SHA, and BUILD_DATE to populate OCI labels and pin the base. Local
# builds work without them via the defaults.

ARG PHP_VERSION=8.4
# BASE_IMAGE lets CI pin to a digest (serversideup/php@sha256:...) so the build
# is reproducible and we can label the exact base used. Local `podman build .`
# falls back to the tag-based default.
ARG BASE_IMAGE=serversideup/php:${PHP_VERSION}-fpm-nginx-alpine
FROM ${BASE_IMAGE}

# Re-declare PHP_VERSION post-FROM so it's visible to LABEL below.
ARG PHP_VERSION
ARG FREESCOUT_VERSION
ARG FREESCOUT_REPO=https://github.com/freescout-helpdesk/freescout
# Build-identity args populated by CI. Defaults keep local builds working.
ARG IMAGE_REVISION=r1
ARG BASE_DIGEST=
ARG GIT_SHA=
ARG BUILD_DATE=

LABEL org.opencontainers.image.title="FreeScout" \
      org.opencontainers.image.description="Self-maintained FreeScout container" \
      org.opencontainers.image.source="https://github.com/pikapods/docker-freescout" \
      org.opencontainers.image.licenses="AGPL-3.0" \
      org.opencontainers.image.version="${FREESCOUT_VERSION}-${IMAGE_REVISION}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.base.name="serversideup/php:${PHP_VERSION}-fpm-nginx-alpine" \
      org.opencontainers.image.base.digest="${BASE_DIGEST}"

USER root

# Runtime + build dependencies.
# Runtime: postgresql-client (pg_isready), mysql-client (mysqladmin ping), tzdata.
# No dcron — the scheduler runs as an s6 longrun service.
#
# The FREESCOUT_VERSION guard rides along here rather than on the clone below:
# an unset ARG expands to "" and `git clone --branch=""` fails without naming
# the cause. On this first RUN it fails in seconds, not after the extension
# compile, and costs no extra layer.
RUN [ -n "${FREESCOUT_VERSION}" ] \
        || { echo "FREESCOUT_VERSION build-arg is required" >&2; exit 1; } \
    && apk add --no-cache \
        git \
        postgresql-client \
        mysql-client \
        tzdata \
    && install-php-extensions \
        pdo_pgsql \
        pgsql \
        pdo_mysql \
        gnupg \
        imap \
        intl \
        bcmath \
        gd \
        exif \
        pcntl \
        opcache \
        redis

# Clone FreeScout at a pinned version. Source baked into the image —
# image tag = app version, no in-data drift.
RUN git clone --depth=1 --branch="${FREESCOUT_VERSION}" \
        "${FREESCOUT_REPO}" /var/www/html \
    && cd /var/www/html \
    && rm -rf .git .github tests

# Composer install. The base image already has composer.
# Two separate RUNs: composer failure must abort the build, but
# `freescout:build` (front-end asset compile) is best-effort.
#
# `rm -rf vendor` before install: upstream FreeScout tarballs/tags ship a
# bundled vendor/ tree that can be stale (verified against 1.8.219 — Composer
# fails at optimized-autoload generation scanning
# vendor/rap2hpoutre/laravel-log-viewer/src/controllers). Removing it first
# gives Composer a clean slate.
#
# `composer require fzaninotto/faker` after the main --no-dev install:
# FreeScout pins faker in require-dev, so --no-dev drops it. Vanilla
# FreeScout never resolves Illuminate\Database\Eloquent\Factory, so the
# gap is invisible — but third-party modules do. The Workflows module's
# WorkflowsServiceProvider::boot() calls registerFactories() → app()
# unconditionally, which constructs Faker\Factory and fatals with
# "Class Faker\Factory not found". Pin matches FreeScout's require-dev so
# the overrides/fzaninotto/faker autoload patches still line up.
#
# The `composer require` re-runs the solver, which (unlike `composer install`,
# served entirely from composer.lock and dist archives) makes Composer
# enumerate the two VCS `repositories` FreeScout declares —
# freescout-help-desk/{laravel-selfupdater,Purifier} — through api.github.com.
# Unauthenticated that budget is 60 requests/hour PER IP, shared across every
# CI runner behind the same NAT. On the resulting 403 Composer assumes the repo
# is private and, unable to prompt under --no-interaction, falls back to
# `git clone git@github.com:...` — SSH, which this image has no client for:
#
#   Failed to clone the git@github.com:freescout-help-desk/laravel-selfupdater.git
#   repository, try running in interactive mode ...
#   error: cannot run ssh: No such file or directory
#
# Two independent guards, because the failure needs both to be robust:
#   - insteadOf rewrites that fallback to HTTPS, which resolves anonymously for
#     both (public) repos. Covers every build, including the UID-rebuild in
#     tests/test_image.py, which gets no secret.
#   - COMPOSER_AUTH from an optional build secret lifts the API budget so the
#     fallback is rarely reached at all. CI feeds it the workflow's GITHUB_TOKEN;
#     `required=false` keeps `podman build .` working with no secret at all.
RUN --mount=type=secret,id=composer_auth,required=false \
    if [ -s /run/secrets/composer_auth ]; then \
        COMPOSER_AUTH="$(cat /run/secrets/composer_auth)"; \
        export COMPOSER_AUTH; \
    fi \
    && git config --global url."https://github.com/".insteadOf "git@github.com:" \
    && cd /var/www/html \
    && rm -rf vendor \
    && composer install \
        --no-dev \
        --no-interaction \
        --no-progress \
        --optimize-autoloader \
        --ignore-platform-reqs \
    && composer require fzaninotto/faker:v1.9.2 \
        --no-interaction \
        --no-progress \
        --update-no-dev \
        --optimize-autoloader \
        --ignore-platform-reqs
RUN cd /var/www/html && (php artisan freescout:build || true)

# Replace in-source storage, Modules, .env with symlinks into /data.
# Targets will not resolve until /data is mounted at runtime — fine; the
# bootstrap script mkdir -p's them on first boot.
#
# /data itself must exist and be owned by www-data: the container runs as a
# non-root user (UID 82 on Alpine) which cannot create /data under /.
#
# The boot-gate sentinel ships raised: while it exists nginx answers every
# request with 500 (server-opts.d/00-freescout-bootstrap-gate.conf), so no
# request reaches PHP against a half-migrated schema. The bootstrap oneshot
# removes it as its final step.
RUN rm -rf /var/www/html/storage /var/www/html/Modules /var/www/html/.env \
    && ln -s /data/storage /var/www/html/storage \
    && ln -s /data/Modules /var/www/html/Modules \
    && ln -s /data/config /var/www/html/.env \
    && touch /var/www/html/.freescout-bootstrap-incomplete \
    && mkdir -p /data \
    && chown www-data:www-data /data \
    && chown -R www-data:www-data /var/www/html

# Build-arg UID/GID override. The base image fixes www-data at 82:82; rebuild
# with --build-arg WWW_DATA_UID=$(id -u) --build-arg WWW_DATA_GID=$(id -g) for
# bind-mount UX without host-side chown. Guarded so the default-build path
# adds no extra layer work. See README "User & permissions".
ARG WWW_DATA_UID=82
ARG WWW_DATA_GID=82
RUN if [ "$WWW_DATA_UID" != "82" ] || [ "$WWW_DATA_GID" != "82" ]; then \
        docker-php-serversideup-set-id www-data "${WWW_DATA_UID}:${WWW_DATA_GID}" \
     && docker-php-serversideup-set-file-permissions --owner "${WWW_DATA_UID}:${WWW_DATA_GID}" \
     && chown "${WWW_DATA_UID}:${WWW_DATA_GID}" /data; \
    fi

VOLUME /data

# Overlay our s6 bootstrap/scheduler services + nginx site config.
COPY rootfs/ /

# - chown /etc/nginx to www-data: ServerSideUp's 10-init-webserver-config
#   runs as www-data and renders /etc/nginx/nginx.conf at boot. After our
#   COPY rootfs/ the directory ends up root-owned and nginx fails to start
#   with "Permission denied" opening nginx.conf.
# - docker-php-serversideup-s6-init converts the base's own entrypoint.d
#   scripts into chained oneshots and orders nginx/php-fpm after the last one,
#   so both start only once their config is rendered. Our bootstrap is a
#   hand-written oneshot outside that chain on purpose: nginx must serve the
#   boot gate and /healthcheck while the bootstrap waits for the DB.
RUN chmod +x /usr/local/bin/freescout-bootstrap \
             /etc/s6-overlay/s6-rc.d/freescout-scheduler/run \
             /usr/local/bin/freescout-db-guard \
             /usr/local/bin/freescout-healthcheck \
    && rm /etc/nginx/server-opts.d/security.conf \
    && chown -R www-data:www-data /etc/nginx \
    && docker-php-serversideup-s6-init

# Image defaults.
# AUTORUN_ENABLED=false: we own the boot sequence; the base's laravel-automations
# would otherwise migrate twice and clobber our explicit cache management.
# SSL_MODE=off: TLS terminates at the reverse proxy.
# ENABLE_FREESCOUT_SCHEDULER=TRUE: FreeScout is broken without scheduled tasks
# (mail fetch, queues). Intentional break from tiredofit's FALSE default.
ENV AUTORUN_ENABLED=false \
    SSL_MODE=off \
    ENABLE_FREESCOUT_SCHEDULER=TRUE \
    APP_BASE_DIR=/var/www/html

# Health probe hits /login (full nginx + php-fpm + Laravel path) with liveness
# semantics: only transport failures and 502/503/504 mark the container
# unhealthy — app-level errors (e.g. 500 while the DB is down) don't, since a
# restart can't fix those. The script spoofs the Host header to match APP_URL
# (TrustHosts 403s loopback otherwise) and self-limits curl below --timeout so
# failures log a reason instead of being SIGKILLed with an empty health-log
# entry — see rootfs/.../freescout-healthcheck.
# retries=5: remaining failure causes are fpm saturated or dead; ~2.5min of
# tolerance avoids restart churn on transient load spikes.
# start-period bumped from 60s — first-boot after-app-update + storage:link + cold
# opcache can take a while.
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=5 \
    CMD freescout-healthcheck || exit 1

EXPOSE 8080

USER www-data
