# docker-freescout

[FreeScout](https://github.com/freescout-helpdesk/freescout) container image,
built on [`serversideup/php`](https://serversideup.net/open-source/docker-php/).

This image powers FreeScout on [PikaPods](https://www.pikapods.com) and is
maintained by the PikaPods team. It's published here for our users' reference
and the benefit of the wider community. To run your own FreeScout pod from
$2.3/month, see
[pikapods.com/pods?run=freescout](https://www.pikapods.com/pods?run=freescout).

Drop-in compatible with `tiredofit/docker-freescout` on env vars and volume
layout (modulo the deliberate breaks called out below).

Published as `ghcr.io/pikapods/docker-freescout:<freescout-version>`.

## Why this image

A small, maintainable FreeScout image focused on simplicity: a short
entrypoint, validated artisan calls, idempotent boot, and a daily auto-rebuild
against upstream FreeScout releases. Stays drop-in compatible with
`tiredofit/docker-freescout` on env vars and volume layout so existing
deployments can switch by changing only the image tag.

## Quick start

```bash
podman run -d --name freescout \
  -v freescout-data:/data \
  -e APP_KEY="base64:$(openssl rand -base64 32)" \
  -e APP_URL="https://support.example.com" \
  -e DB_TYPE=pgsql \
  -e DB_HOST=db.internal \
  -e DB_NAME=freescout \
  -e DB_USER=freescout \
  -e DB_PASS=... \
  -e ADMIN_EMAIL=admin@example.com \
  -e ADMIN_PASS=changeme \
  -p 8080:8080 \
  ghcr.io/pikapods/docker-freescout:latest
```

## Environment variables

### Core

| Var                   | Required | Purpose                                                              |
|-----------------------|----------|----------------------------------------------------------------------|
| `APP_KEY`             | yes      | Laravel encryption key. Must be preserved across restarts.           |
| `APP_URL`             | yes      | Public URL (no trailing slash).                                      |
| `DB_TYPE`             | yes      | `pgsql` (or `postgres`/`postgresql`), `mysql`, or `mariadb`.         |
| `DB_HOST`             | yes      | DB hostname.                                                         |
| `DB_PORT`             | no       | DB port. Defaults to 5432 (pgsql) or 3306 (mysql/mariadb).           |
| `DB_NAME`             | yes      | DB name.                                                             |
| `DB_USER`             | yes      | DB user.                                                             |
| `DB_PASS`             | yes      | DB password.                                                         |

### Admin seed (first boot only)

| Var                  | Required when      | Purpose                                  |
|----------------------|--------------------|------------------------------------------|
| `ADMIN_EMAIL`        | seeding admin      | Admin user email.                        |
| `ADMIN_PASS`         | `ADMIN_EMAIL` set  | Admin user password.                     |
| `ADMIN_FIRST_NAME`   | no                 | Defaults to `Admin`.                     |
| `ADMIN_LAST_NAME`    | no                 | Defaults to `User`.                      |

The admin is only seeded if `SELECT COUNT(*) FROM users` returns 0. Safe to
leave these set on subsequent boots — they're ignored once a user exists.

### Scheduler

| Var                          | Default | Purpose                                                |
|------------------------------|---------|--------------------------------------------------------|
| `ENABLE_FREESCOUT_SCHEDULER` | `TRUE`  | Set `FALSE` to disable the per-minute `schedule:run`.  |

**Default differs from `tiredofit/docker-freescout`** (which defaults to
`FALSE`). FreeScout doesn't fetch email or process queues without the
scheduler, so `TRUE` is the only sensible default.

### FreeScout `.env` passthrough

Any env var named `FREESCOUT_<KEY>` is stripped of its prefix and patched into
`/data/config` (the FreeScout `.env` file). Example:

```
FREESCOUT_MAIL_HOST=smtp.mailgun.org
FREESCOUT_MAIL_PORT=587
FREESCOUT_SESSION_SECURE_COOKIE=true
```

becomes

```
MAIL_HOST=smtp.mailgun.org
MAIL_PORT=587
SESSION_SECURE_COOKIE=true
```

inside `/data/config`.

**Set-through-once semantics.** Removing a `FREESCOUT_*` env var on a
subsequent boot does *not* clear the key from `.env`. To delete a key, set the
sentinel value `unset`, `null`, or empty string:

```
FREESCOUT_MAIL_HOST=unset    # removes the MAIL_HOST line
```

Matches the `tiredofit/docker-freescout` convention.

**Key validation.** Keys are stripped of the `FREESCOUT_` prefix and must
match `[A-Z0-9_]+`. Invalid keys (dots, dashes, lowercase, regex metachars)
are logged and skipped.

## Mounts

| Path                   | Purpose                                                          |
|------------------------|------------------------------------------------------------------|
| `/data`                | Persistent volume. Contains `config` (the `.env`), `Modules/`, `storage/`. |
| `/var/www/html`        | FreeScout source. Baked at build time — do **not** bind-mount.   |

The image creates `/var/www/html/{storage,Modules,.env}` as symlinks into
`/data` at build time. Anything you write under `/data/storage/` (uploads,
logs, cache) survives container restarts and image upgrades.

### User & permissions

Both nginx and php-fpm run as `www-data` (**UID 82 / GID 82** — Alpine's
default, inherited from `serversideup/php:*-alpine`).

Anything the container writes to `/data` lands as `82:82` on the host. For
rootless podman, map it explicitly with `--userns=keep-id:uid=82,gid=82`; for
rootful runs, `chown -R 82:82` on the host volume root before first start.

## Ports

| Port | Purpose                                                                  |
|------|--------------------------------------------------------------------------|
| 8080 | HTTP (serversideup's unprivileged default — `tiredofit` exposes 80).     |

Behind a reverse proxy this is invisible to end users; document any direct
exposure if you're not using a proxy.

## `.env` ownership model

`/data/config` is **user state**, not a regenerated artifact. Each boot:

1. The image **always overwrites** a small set of ops-managed keys: `APP_KEY`,
   `APP_URL`, `DB_CONNECTION`, `DB_HOST`, `DB_PORT`, `DB_DATABASE`,
   `DB_USERNAME`, `DB_PASSWORD`.
2. Any `FREESCOUT_*` env vars are **patched in** (set-through-once — see
   above).
3. **Everything else in the file is preserved untouched.** Hand-edits via
   `podman exec`, settings you've pasted in, custom mail config — all survive
   boots.

This differs from typical container behavior where env vars are the full
source of truth for configuration. The rationale: in-app settings (spam
filter config, webhook URLs, custom mail tuning) belong to the operator, not
the image. Treating `.env` as fully image-owned and rewriting it on every
boot resets those settings to defaults; here, env vars are *initializers and
patches*, not the canonical source.

## Deliberate breaks vs. `tiredofit/docker-freescout`

| Break                                                  | Rationale                                                |
|--------------------------------------------------------|----------------------------------------------------------|
| Default port `8080` (was `80`)                         | Unprivileged. Behind a proxy it's invisible.             |
| App lives at `/var/www/html` (was `/www/html`)         | `serversideup/php` convention. Override your bind-mounts. |
| `/data/config` is a **file** (the `.env`), not a dir   | Matches *old* tiredofit. A `/data/config/config` directory layout is rejected by a preflight guard. |
| `ENABLE_FREESCOUT_SCHEDULER` defaults `TRUE`           | FreeScout is broken without it.                          |
| `SETUP_TYPE`, `ENABLE_AUTO_UPDATE`, `DB_SSL`, `DATA_PATH` dropped | Not supported. Use `FREESCOUT_DB_SSLMODE` for TLS; data path is fixed at `/data`; updates happen via image tag. |

## Migration

### From `tiredofit/docker-freescout`

If you have `/data/config` as a directory containing the `.env`, the image
will refuse to start with a clear error. Flatten it to a file:

```bash
# Inside the volume:
mv /data/config /data/config.dir
mv /data/config.dir/config /data/config
rm -rf /data/config.dir
```

Then update the image tag and the reverse-proxy backend port (`80` → `8080`).
If you had bind-mounts pointing at `/www/html`, move them to `/var/www/html`.
Env vars carry over unchanged.

## Building locally

```bash
podman build --format docker \
  --build-arg FREESCOUT_VERSION=1.8.219 \
  --build-arg PHP_VERSION=8.4 \
  -t freescout:test .
```

`--format docker` is important: podman defaults to OCI manifests, which
silently drop the `HEALTHCHECK` instruction. Docker format embeds it in the
image metadata so `podman healthcheck run`, Quadlet, and orchestrators can
use it. The CI build pushes Docker-format manifests for the same reason.

Smoke test:

```bash
podman network create fs-smoke
podman run -d --name pg --network fs-smoke \
  -e POSTGRES_PASSWORD=test -e POSTGRES_DB=freescout postgres:16
podman run -d --name fs --network fs-smoke \
  -e APP_KEY="base64:$(openssl rand -base64 32)" \
  -e APP_URL=http://localhost:8080 \
  -e DB_TYPE=pgsql -e DB_HOST=pg -e DB_NAME=freescout \
  -e DB_USER=postgres -e DB_PASS=test \
  -e ADMIN_EMAIL=admin@test.local -e ADMIN_PASS=changeme \
  -p 8080:8080 freescout:test
curl -I http://localhost:8080/login   # → HTTP/1.1 200 OK
```

## Auto-rebuild

`.github/workflows/upstream-watch.yml` runs daily at 06:00 UTC, polls the
[freescout-helpdesk/freescout](https://github.com/freescout-helpdesk/freescout)
releases API, and triggers `build.yml` when a new tag appears. The build
workflow runs a smoke test (Postgres sidecar, `/login` returns 200, no
`RuntimeException` in logs) before pushing.

## License

The FreeScout source is AGPL-3.0; this image inherits that license.
