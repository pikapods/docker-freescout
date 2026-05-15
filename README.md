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

Published to both `ghcr.io/pikapods/docker-freescout` and
`pikapods/docker-freescout` (Docker Hub) — pick whichever registry you
prefer. See [Tags](#tags) for the available tag scheme.

Source: https://github.com/pikapods/docker-freescout

## Tags

| Tag                 | Mutability | Use for                                                                 |
|---------------------|------------|-------------------------------------------------------------------------|
| `latest`            | mutable    | Tracks the newest build in the pinned upstream series.                  |
| `<version>`         | mutable    | e.g. `1.8.219`. Latest revision of that FreeScout version (gets base-image security rebuilds in place). |
| `<version>-r<n>`    | immutable  | e.g. `1.8.219-r2`. Exact build, never reused. Pin here for reproducible deploys. |

## Why this image

A small, maintainable FreeScout image focused on simplicity: a short
entrypoint, validated artisan calls, idempotent boot, and a daily auto-rebuild
against upstream FreeScout releases. Stays drop-in compatible with
`tiredofit/docker-freescout` on env vars and volume layout so existing
deployments can switch by changing only the image tag.

## Quick start

The bundled `compose.yaml` brings up FreeScout plus a Postgres sidecar with
zero external dependencies — the fastest way to try the image:

```bash
git clone https://github.com/pikapods/docker-freescout.git
cd docker-freescout
docker compose up -d
# wait ~30s for first-boot migrations
curl -I http://localhost:8080/login   # → HTTP/1.1 200 OK
```

Default credentials are `admin@example.com` / `changeme` — change them
before any real deployment.

Against an existing database:

```bash
docker run -d --name freescout \
  -v freescout-data:/data \
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

### Running on podman

The compose file and `docker run` examples work as-is under
`podman compose` / `podman run`. Three podman-specific notes:

- **Build:** `podman build --format docker …`. Podman defaults to OCI
  manifests, which silently drop the `HEALTHCHECK` instruction; docker
  format embeds it.
- **Rootless permissions:** rootless podman remaps UIDs, so the
  container's `www-data` (UID 82) isn't host UID 82. For bind mounts, add
  `--userns=keep-id:uid=82,gid=82`. Rootful podman behaves like docker.
  Full decision matrix in [User & permissions](#user--permissions).
- **Healthcheck inspection:** `podman healthcheck run <container>` runs
  the check on demand. Docker runs it automatically; inspect with
  `docker inspect --format '{{.State.Health.Status}}' <container>`.

## Environment variables

### Core

| Var                   | Required | Purpose                                                              |
|-----------------------|----------|----------------------------------------------------------------------|
| `APP_URL`             | yes      | Public URL (no trailing slash). Legacy alias `SITE_URL` accepted for tiredofit drop-in compat. |
| `DB_TYPE`             | yes      | `pgsql` (or `postgres`/`postgresql`), `mysql`, or `mariadb`.         |
| `DB_HOST`             | yes      | DB hostname.                                                         |
| `DB_PORT`             | no       | DB port. Defaults to 5432 (pgsql) or 3306 (mysql/mariadb).           |
| `DB_NAME`             | yes      | DB name.                                                             |
| `DB_USER`             | yes      | DB user.                                                             |
| `DB_PASS`             | yes      | DB password.                                                         |
| `APP_KEY`             | no       | Laravel encryption key. Generated and persisted to `/data/config` on first boot. Pass `-e APP_KEY=…` only if you need to manage it externally (e.g. from a secret store); the image accepts whatever Laravel accepts. Once set, **do not change or remove it** — Laravel and FreeScout use it to decrypt sessions and encrypted columns; rotating it invalidates that data. |

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
default, inherited from `serversideup/php:*-alpine`). How those writes
surface on the host depends on your runtime; pick the row that matches:

| Setup                                  | What to do                                                                                                   | Host-side ownership of `/data` writes |
|----------------------------------------|--------------------------------------------------------------------------------------------------------------|---------------------------------------|
| Named volume (docker or podman)        | Nothing — daemon manages ownership. Default in `compose.yaml`.                                               | Inside daemon-managed volume; not user-visible. |
| Bind mount, rootful docker/podman      | `chown -R 82:82 <host-dir>` before first boot.                                                               | `82:82`.                              |
| Bind mount, rootless podman            | Add `--userns=keep-id:uid=82,gid=82` to `podman run`.                                                        | Invoking host user's UID/GID.         |
| Custom-UID rebuild                     | `docker build --build-arg WWW_DATA_UID=$(id -u) --build-arg WWW_DATA_GID=$(id -g) -t freescout:local .`      | The UID baked at build time.          |

The bootstrap runs a preflight writability check on `/data` and refuses to
start with a readable error if ownership is wrong, rather than failing
cryptically deep in `mkdir`.

Why not a runtime `PUID`/`PGID` env var? Upstream `serversideup/php` v3
[deliberately removed root from the boot path](https://github.com/serversideup/docker-php/issues/253),
and runtime UID remap requires reintroducing it. The supported lever is the
build-time `WWW_DATA_UID`/`WWW_DATA_GID` rebuild above. Note that docker's
`userns-remap` is daemon-wide and maps to a subordinate UID range, so it
*worsens* bind-mount UX rather than fixing it — there is no rootful-docker
runtime trick equivalent to podman's `--userns=keep-id`.

## Ports

| Port | Purpose                                                                  |
|------|--------------------------------------------------------------------------|
| 8080 | HTTP (serversideup's unprivileged default — `tiredofit` exposes 80).     |

Behind a reverse proxy this is invisible to end users; document any direct
exposure if you're not using a proxy.

## `.env` ownership model

`/data/config` is **user state**, not a regenerated artifact.

`APP_KEY` is resolved with a three-way fallback: an `APP_KEY` env var wins
(operator override); otherwise an existing non-empty value in `/data/config`
is preserved; otherwise the image generates one via `php artisan key:generate`
and persists it. Once written, `APP_KEY` is treated as immutable user state —
rotating it invalidates encrypted sessions and DB columns.

Each boot:

1. The image **always overwrites** a small set of ops-managed keys from the
   environment: `APP_URL`, `DB_CONNECTION`, `DB_HOST`, `DB_PORT`,
   `DB_DATABASE`, `DB_USERNAME`, `DB_PASSWORD`.
2. Any `FREESCOUT_*` env vars are **patched in** (set-through-once — see
   above).
3. **Everything else in the file is preserved untouched.** Hand-edits via
   `docker exec`, settings you've pasted in, custom mail config — all survive
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

## Building locally

```bash
docker build \
  --build-arg FREESCOUT_VERSION=1.8.219 \
  --build-arg PHP_VERSION=8.4 \
  -t freescout:test .
```

On podman, add `--format docker` — see the podman notes in Quick start for
why. The CI build pushes Docker-format manifests for the same reason.

## License

The FreeScout source is AGPL-3.0; this image inherits that license.
