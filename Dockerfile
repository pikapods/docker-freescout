# FreeScout image — self-maintained, derived from serversideup/php.
# See README.md for design notes and usage.
#
# Build:
#   podman build \
#     --build-arg FREESCOUT_VERSION=1.8.219 \
#     --build-arg PHP_VERSION=8.4 \
#     -t ghcr.io/pikapods/docker-freescout:1.8.219-php8.4 .

ARG PHP_VERSION=8.4
FROM serversideup/php:${PHP_VERSION}-fpm-nginx-alpine

ARG FREESCOUT_VERSION=1.8.219
ARG FREESCOUT_REPO=https://github.com/freescout-helpdesk/freescout

LABEL org.opencontainers.image.title="FreeScout" \
      org.opencontainers.image.description="Self-maintained FreeScout container" \
      org.opencontainers.image.source="https://github.com/pikapods/docker-freescout" \
      org.opencontainers.image.licenses="AGPL-3.0" \
      org.opencontainers.image.version="${FREESCOUT_VERSION}"

USER root

# Runtime + build dependencies.
# Runtime: postgresql-client (pg_isready), mysql-client (mysqladmin ping), tzdata.
# No dcron — the scheduler runs as an s6 longrun service.
RUN apk add --no-cache \
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
RUN cd /var/www/html \
    && rm -rf vendor \
    && composer install \
        --no-dev \
        --no-interaction \
        --no-progress \
        --optimize-autoloader \
        --ignore-platform-reqs
RUN cd /var/www/html && (php artisan freescout:build || true)

# Replace in-source storage, Modules, .env with symlinks into /data.
# Targets will not resolve until /data is mounted at runtime — fine; the
# bootstrap script mkdir -p's them on first boot.
#
# /data itself must exist and be owned by www-data: the container runs as a
# non-root user (UID 82 on Alpine) which cannot create /data under /.
RUN rm -rf /var/www/html/storage /var/www/html/Modules /var/www/html/.env \
    && ln -s /data/storage /var/www/html/storage \
    && ln -s /data/Modules /var/www/html/Modules \
    && ln -s /data/config /var/www/html/.env \
    && mkdir -p /data \
    && chown www-data:www-data /data \
    && chown -R www-data:www-data /var/www/html

VOLUME /data

# Overlay our entrypoint hook + s6 scheduler service + nginx site config.
COPY rootfs/ /

# - chmod *before* docker-php-serversideup-s6-init: the init tool moves
#   /etc/entrypoint.d/*.sh into /etc/s6-overlay/scripts/ and renames them, so
#   chmod afterwards at the original path would fail.
# - chown /etc/nginx to www-data: ServerSideUp's 10-init-webserver-config
#   runs as www-data and renders /etc/nginx/nginx.conf at boot. After our
#   COPY rootfs/ the directory ends up root-owned and nginx fails to start
#   with "Permission denied" opening nginx.conf.
RUN chmod +x /etc/entrypoint.d/20-freescout-bootstrap.sh \
             /etc/s6-overlay/s6-rc.d/freescout-scheduler/run \
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

# Health endpoint hits /login (Laravel route, returns 200, exercises nginx + php-fpm).
# start-period bumped from 60s — first-boot after-app-update + storage:link + cold
# opcache can take a while.
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD curl -fsS http://localhost:8080/login -o /dev/null || exit 1

EXPOSE 8080

USER www-data
