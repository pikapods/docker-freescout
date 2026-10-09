import base64
import json
import os
import re
import secrets
import subprocess
import time
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.runtime

IMAGE = os.environ["IMAGE"]
# The boot gate answers 500 until the bootstrap oneshot (DB wait + full
# migrations + module install) finishes, so waiting for a 200 now means
# waiting for the complete first boot — size the deadline for slow CI runners.
READY_DEADLINE_S = 300
HEALTHY_DEADLINE_S = 90

# FreeScout/Laravel rejects requests whose Host header does not match APP_URL
# with a 403. The fixture passes APP_URL=http://localhost:8080, so every test
# request must declare Host: localhost:8080 — the random host port we bind to
# is only the TCP destination.
APP_HOST_HEADER = "localhost:8080"


def _sh(*args, check=True, capture=True):
    return subprocess.run(
        list(args),
        capture_output=capture, text=True, check=check,
    )


def _exec(container, *args, check=False):
    return subprocess.run(
        ["docker", "exec", container, *args],
        capture_output=True, text=True, check=check,
    )


def _wait_pg_ready(container, deadline_s=30):
    # Probe TCP, not the unix socket. The postgres image briefly serves
    # the unix socket during init-script execution before restarting to
    # enable TCP; without `-h 127.0.0.1` the wait can return too early
    # and the first TCP client (psql or the freescout container) hits
    # ECONNREFUSED.
    end = time.time() + deadline_s
    while time.time() < end:
        if _exec(container, "pg_isready",
                 "-h", "127.0.0.1", "-U", "postgres").returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError(f"postgres container {container} not ready within {deadline_s}s")


def _wait_mysql_ready(container, deadline_s=60):
    # MariaDB init takes longer than postgres on first boot (datadir bootstrap
    # + grant rebuild). Credentials are required because the root account is
    # password-protected by the MARIADB_ROOT_PASSWORD env. MariaDB 11 dropped
    # the mysql/mysqladmin symlinks — invoke the native names directly.
    end = time.time() + deadline_s
    while time.time() < end:
        r = _exec(
            container, "mariadb-admin", "ping",
            "-h", "127.0.0.1", "-uroot", "-ptest", "--silent",
        )
        if r.returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError(f"mariadb container {container} not ready within {deadline_s}s")


def _http_get(url, timeout=10):
    req = urllib.request.Request(url, headers={"Host": APP_HOST_HEADER})
    return urllib.request.urlopen(req, timeout=timeout)


def _wait_http_200(url, deadline_s):
    end = time.time() + deadline_s
    last_err = None
    while time.time() < end:
        try:
            with _http_get(url, timeout=5) as r:
                if r.status == 200:
                    return
                last_err = f"status={r.status}"
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            last_err = repr(e)
        time.sleep(2)
    raise RuntimeError(f"{url} did not return 200 within {deadline_s}s (last={last_err})")


def _host_port(container, container_port):
    r = _sh("docker", "port", container, container_port)
    # Output like "0.0.0.0:32768\n[::]:32768\n" — take first line.
    line = r.stdout.splitlines()[0]
    return int(line.rsplit(":", 1)[1])


@pytest.fixture(scope="session")
def stack():
    suffix = secrets.token_hex(4)
    net = f"fs-net-{suffix}"
    pg = f"pg-{suffix}"
    fs = f"fs-{suffix}"
    app_key = "base64:" + base64.b64encode(secrets.token_bytes(32)).decode()

    _sh("docker", "network", "create", net)
    try:
        _sh(
            "docker", "run", "-d", "--name", pg, "--network", net,
            "-e", "POSTGRES_PASSWORD=test",
            "-e", "POSTGRES_DB=freescout",
            "postgres:16",
        )
        _wait_pg_ready(pg)

        _sh(
            "docker", "run", "-d", "--name", fs, "--network", net,
            "-e", f"APP_KEY={app_key}",
            "-e", "APP_URL=http://localhost:8080",
            "-e", "DB_TYPE=pgsql",
            "-e", f"DB_HOST={pg}",
            "-e", "DB_NAME=freescout",
            "-e", "DB_USER=postgres",
            "-e", "DB_PASS=test",
            "-e", "ADMIN_EMAIL=admin@smoke.local",
            "-e", "ADMIN_PASS=changeme",
            "-p", ":8080",
            IMAGE,
        )
        port = _host_port(fs, "8080")
        try:
            _wait_http_200(f"http://127.0.0.1:{port}/login", READY_DEADLINE_S)
        except RuntimeError:
            print(_sh("docker", "logs", fs, check=False).stdout)
            print(_sh("docker", "logs", fs, check=False).stderr)
            raise

        yield {"fs": fs, "pg": pg, "net": net, "port": port}
    finally:
        for name in (fs, pg):
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        subprocess.run(["docker", "network", "rm", net], capture_output=True)


@pytest.fixture(scope="session")
def stack_mariadb():
    """Mirror of `stack` against MariaDB. Proves the guard's
    driver-agnostic claim end-to-end — Schema::getAllTables() and
    Schema::hasTable() behave on MariaDB as well as Postgres."""
    suffix = secrets.token_hex(4)
    net = f"fs-net-{suffix}"
    db = f"db-{suffix}"
    fs = f"fs-{suffix}"
    app_key = "base64:" + base64.b64encode(secrets.token_bytes(32)).decode()

    _sh("docker", "network", "create", net)
    try:
        _sh(
            "docker", "run", "-d", "--name", db, "--network", net,
            "-e", "MARIADB_ROOT_PASSWORD=test",
            "-e", "MARIADB_DATABASE=freescout",
            "mariadb:11",
        )
        _wait_mysql_ready(db)

        _sh(
            "docker", "run", "-d", "--name", fs, "--network", net,
            "-e", f"APP_KEY={app_key}",
            "-e", "APP_URL=http://localhost:8080",
            "-e", "DB_TYPE=mariadb",
            "-e", f"DB_HOST={db}",
            "-e", "DB_NAME=freescout",
            "-e", "DB_USER=root",
            "-e", "DB_PASS=test",
            "-e", "ADMIN_EMAIL=admin@smoke.local",
            "-e", "ADMIN_PASS=changeme",
            "-p", ":8080",
            IMAGE,
        )
        port = _host_port(fs, "8080")
        try:
            _wait_http_200(f"http://127.0.0.1:{port}/login", READY_DEADLINE_S)
        except RuntimeError:
            print(_sh("docker", "logs", fs, check=False).stdout)
            print(_sh("docker", "logs", fs, check=False).stderr)
            raise

        yield {"fs": fs, "db": db, "net": net, "port": port}
    finally:
        for name in (fs, db):
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        subprocess.run(["docker", "network", "rm", net], capture_output=True)


@pytest.fixture(scope="session")
def stack_no_appkey():
    suffix = secrets.token_hex(4)
    net = f"fs-net-{suffix}"
    pg = f"pg-{suffix}"
    fs = f"fs-{suffix}"
    vol = f"fs-data-{suffix}"

    _sh("docker", "network", "create", net)
    _sh("docker", "volume", "create", vol)
    try:
        _sh(
            "docker", "run", "-d", "--name", pg, "--network", net,
            "-e", "POSTGRES_PASSWORD=test",
            "-e", "POSTGRES_DB=freescout",
            "postgres:16",
        )
        _wait_pg_ready(pg)

        _sh(
            "docker", "run", "-d", "--name", fs, "--network", net,
            "-e", "APP_URL=http://localhost:8080",
            "-e", "DB_TYPE=pgsql",
            "-e", f"DB_HOST={pg}",
            "-e", "DB_NAME=freescout",
            "-e", "DB_USER=postgres",
            "-e", "DB_PASS=test",
            "-e", "ADMIN_EMAIL=admin@smoke.local",
            "-e", "ADMIN_PASS=changeme",
            "-v", f"{vol}:/data",
            "-p", ":8080",
            IMAGE,
        )
        port = _host_port(fs, "8080")
        try:
            _wait_http_200(f"http://127.0.0.1:{port}/login", READY_DEADLINE_S)
        except RuntimeError:
            print(_sh("docker", "logs", fs, check=False).stdout)
            print(_sh("docker", "logs", fs, check=False).stderr)
            raise

        yield {"fs": fs, "pg": pg, "net": net, "port": port, "vol": vol}
    finally:
        for name in (fs, pg):
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        subprocess.run(["docker", "network", "rm", net], capture_output=True)
        subprocess.run(["docker", "volume", "rm", vol], capture_output=True)


def _read_app_key(container):
    r = _exec(container, "grep", "-E", "^APP_KEY=", "/data/config")
    assert r.returncode == 0, f"APP_KEY missing from /data/config (stderr={r.stderr!r})"
    return r.stdout.strip()


def test_app_key_generated_in_env_file(stack_no_appkey):
    r = _exec(stack_no_appkey["fs"], "grep", "-E", "^APP_KEY=.+", "/data/config")
    assert r.returncode == 0, "APP_KEY= line is missing or empty in /data/config"
    value = r.stdout.strip().split("=", 1)[1]
    assert value, "APP_KEY value is empty"


def test_app_key_stable_across_restart(stack_no_appkey):
    fs = stack_no_appkey["fs"]
    key1 = _read_app_key(fs)
    _sh("docker", "restart", fs)
    # `-p 0:8080` makes the host port ephemeral; Docker may reassign it on
    # restart, so re-query rather than reusing the fixture's pre-restart port.
    port = _host_port(fs, "8080")
    try:
        _wait_http_200(f"http://127.0.0.1:{port}/login", READY_DEADLINE_S)
    except RuntimeError:
        print(_sh("docker", "logs", fs, check=False).stdout)
        print(_sh("docker", "logs", fs, check=False).stderr)
        raise
    key2 = _read_app_key(fs)
    assert key1 == key2, f"APP_KEY changed across restart: {key1!r} -> {key2!r}"


def test_login_responds_200(stack):
    with _http_get(f"http://127.0.0.1:{stack['port']}/login") as r:
        assert r.status == 200
        body = r.read().decode("utf-8", errors="replace")
    # Cheap content sanity — FreeScout's login template renders a password field.
    assert 'type="password"' in body or "password" in body.lower()


@pytest.mark.parametrize("ext", ["log", "sql", "conf", "bak", "ini", "sh", "swp"])
def test_attachment_url_reaches_laravel(stack, ext):
    # serversideup/php-nginx's server-opts.d/security.conf denies any URL
    # ending in these extensions outright. FreeScout legitimately serves
    # user-uploaded attachments through Laravel
    # (routes/open.php -> OpenController@downloadAttachment), so the
    # extension regex must not pre-empt the route. With a bogus token the
    # controller returns its own 403/404; the failure mode we guard
    # against is nginx's stock 403 page, which means the request never
    # reached PHP.
    url = (
        f"http://127.0.0.1:{stack['port']}/storage/attachment/"
        f"0/0/0/x.{ext}?id=0&token=bogus"
    )
    req = urllib.request.Request(url, headers={"Host": APP_HOST_HEADER})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        body = e.read()
    # nginx's stock error page contains `<center>nginx</center>` in the
    # footer. FreeScout's responses do not.
    assert b"<center>nginx</center>" not in body, (
        f".{ext} attachment URL was blocked by nginx instead of routed to "
        f"Laravel; body={body[:200]!r}"
    )


def test_attachment_url_hidden_file_still_denied(stack):
    # Boundary check on the override in
    # rootfs/etc/nginx/server-opts.d/00-freescout-attachments.conf: it
    # bypasses *only* the sensitive-extension deny. The sibling hidden-file
    # deny (`location ~ /\.(?!well-known)`) must continue to fire for
    # attachment paths whose filename starts with a dot.
    url = (
        f"http://127.0.0.1:{stack['port']}/storage/attachment/"
        "0/0/0/.env?id=0&token=bogus"
    )
    req = urllib.request.Request(url, headers={"Host": APP_HOST_HEADER})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            status, body = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read()
    assert status == 403, f"expected nginx 403 on hidden-file attachment URL, got {status}"
    assert b"<center>nginx</center>" in body, (
        "expected nginx's stock 403 page (hidden-file deny still in effect); "
        f"body={body[:200]!r}"
    )


def test_server_name_never_nginx_catchall(stack):
    # SwiftMailer sends $_SERVER['SERVER_NAME'] as the SMTP EHLO argument, so
    # nginx's catch-all `server_name _` reaching PHP means `EHLO _` and a 501
    # from strict relays (pikapods/docker-freescout#1). conf.d/01-server-name.conf
    # + fastcgi_params must pass the request Host instead, and nothing at all
    # when the Host is unusable.
    #
    # Probe a bare PHP file rather than a route: TrustHosts would 403 the
    # spoofed Host headers before any Laravel code echoed the value back.
    probe = "/var/www/html/public/__servername.php"
    write = _exec(
        stack["fs"], "sh", "-c",
        f"printf '%s' \"<?php echo \\$_SERVER['SERVER_NAME'] ?? 'UNSET';\" > {probe}",
    )
    assert write.returncode == 0, f"could not write probe: {write.stderr!r}"
    try:
        def body_for(host):
            req = urllib.request.Request(
                f"http://127.0.0.1:{stack['port']}/__servername.php",
                headers={"Host": host},
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.read().decode("utf-8", errors="replace").strip()

        assert body_for("mail.example.com") == "mail.example.com"
        # No usable hostname -> unset, so SwiftMailer falls back to [127.0.0.1].
        assert body_for("_") == "UNSET"
    finally:
        _exec(stack["fs"], "rm", "-f", probe)


def test_logs_clean(stack):
    logs = _sh("docker", "logs", stack["fs"], check=False)
    combined = logs.stdout + logs.stderr
    bad = re.findall(r"RuntimeException|PHP Fatal", combined)
    assert not bad, f"bad patterns in container logs: {bad[:5]}"


def test_scheduler_longrun_alive(stack):
    # The scheduler longrun is a `while :;` shell loop; the process is
    # always present unless s6 has given up restarting it.
    # Read /proc/<pid>/cmdline directly — busybox `ps` on Alpine
    # truncates or omits args for shebang-launched scripts
    # (`#!/command/with-contenv sh`), so the run-script path doesn't
    # appear in `ps` output. /proc cmdline is world-readable and
    # contains the kernel's view of argv with no truncation.
    r = _exec(
        stack["fs"], "sh", "-c",
        "cat /proc/[0-9]*/cmdline 2>/dev/null | tr '\\0' '\\n' "
        "| grep -qF freescout-scheduler/run",
    )
    assert r.returncode == 0, (
        "scheduler longrun process not present in /proc cmdlines "
        f"(stdout={r.stdout!r}, stderr={r.stderr!r})"
    )


@pytest.mark.parametrize("path", [
    "/data/storage/framework/cache",
    "/data/storage/framework/sessions",
    "/data/storage/framework/views",
    "/data/storage/logs",
    "/data/Modules",
    "/data/config",
])
def test_bootstrap_populated_data(stack, path):
    flag = "-f" if path == "/data/config" else "-d"
    r = _exec(stack["fs"], "test", flag, path)
    assert r.returncode == 0, f"bootstrap did not produce {path}"


@pytest.mark.parametrize("path", [
    # The seed file itself in the volume...
    "/data/storage/app/public/.gitignore",
    # ...and as resolved through the public/storage symlink chain, which is
    # exactly what FreeScout's System Status check reads. Both must be non-empty
    # (-s) or the check prints the spurious "Create symlink manually" warning.
    "/var/www/html/public/storage/.gitignore",
])
def test_public_storage_gitignore_seeded(stack, path):
    r = _exec(stack["fs"], "test", "-s", path)
    assert r.returncode == 0, f"missing/empty {path}; System Status would warn"


def test_env_file_has_db_keys(stack):
    r = _exec(stack["fs"], "cat", "/data/config")
    assert r.returncode == 0, r.stderr
    for key in ("APP_KEY=", "APP_URL=", "DB_CONNECTION=pgsql", "DB_HOST=", "DB_DATABASE=freescout"):
        assert key in r.stdout, f"{key!r} not written to /data/config"


def test_healthcheck_reports_healthy(stack):
    end = time.time() + HEALTHY_DEADLINE_S
    last = None
    while time.time() < end:
        r = _sh("docker", "inspect", "--format", "{{json .State.Health}}", stack["fs"])
        health = json.loads(r.stdout)
        if not health:
            pytest.skip("image has no HEALTHCHECK or daemon does not surface health")
        last = health.get("Status")
        if last == "healthy":
            return
        if last == "unhealthy":
            pytest.fail(f"container went unhealthy: {health.get('Log', [])[-1:]!r}")
        time.sleep(3)
    pytest.fail(f"healthcheck still {last!r} after {HEALTHY_DEADLINE_S}s")


@pytest.fixture(scope="session")
def stack_public_url():
    # Mirrors `stack` but with a non-localhost APP_URL — the case operators
    # actually run in. Pins down that the loopback healthcheck doesn't depend
    # on APP_URL=http://localhost:..., which would otherwise be the only way
    # to satisfy FreeScout's TrustHosts middleware
    # (see rootfs/usr/local/bin/freescout-healthcheck).
    #
    # Waits directly on docker's healthcheck status rather than HTTP-polling
    # /login: urllib follows redirects, and with APP_URL=https://... FreeScout
    # will issue 3xx to the public host, whose name doesn't resolve in CI.
    suffix = secrets.token_hex(4)
    net = f"fs-net-{suffix}"
    pg = f"pg-{suffix}"
    fs = f"fs-{suffix}"
    app_url_host = "support.example.test"

    _sh("docker", "network", "create", net)
    try:
        _sh(
            "docker", "run", "-d", "--name", pg, "--network", net,
            "-e", "POSTGRES_PASSWORD=test",
            "-e", "POSTGRES_DB=freescout",
            "postgres:16",
        )
        _wait_pg_ready(pg)

        _sh(
            "docker", "run", "-d", "--name", fs, "--network", net,
            "-e", f"APP_URL=https://{app_url_host}",
            "-e", "DB_TYPE=pgsql",
            "-e", f"DB_HOST={pg}",
            "-e", "DB_NAME=freescout",
            "-e", "DB_USER=postgres",
            "-e", "DB_PASS=test",
            "-e", "ADMIN_EMAIL=admin@smoke.local",
            "-e", "ADMIN_PASS=changeme",
            "-p", ":8080",
            IMAGE,
        )
        port = _host_port(fs, "8080")

        # Bootstrap takes ~60-90s before HEALTHCHECK's 120s start-period even
        # begins counting; pad the deadline generously.
        deadline = time.time() + READY_DEADLINE_S + HEALTHY_DEADLINE_S
        last = None
        while time.time() < deadline:
            r = _sh("docker", "inspect", "--format",
                    "{{json .State.Health}}", fs)
            health = json.loads(r.stdout)
            if not health:
                pytest.skip("image has no HEALTHCHECK or daemon does not surface health")
            last = health.get("Status")
            if last == "healthy":
                break
            if last == "unhealthy":
                print(_sh("docker", "logs", fs, check=False).stdout)
                print(_sh("docker", "logs", fs, check=False).stderr)
                print(json.dumps(health.get("Log", []), indent=2))
                raise RuntimeError(f"container went unhealthy: {health.get('Log', [])[-1:]!r}")
            time.sleep(3)
        else:
            print(_sh("docker", "logs", fs, check=False).stdout)
            print(_sh("docker", "logs", fs, check=False).stderr)
            raise RuntimeError(f"healthcheck still {last!r} after deadline")

        yield {"fs": fs, "pg": pg, "net": net, "port": port, "host": app_url_host}
    finally:
        for name in (fs, pg):
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        subprocess.run(["docker", "network", "rm", net], capture_output=True)


def test_healthcheck_healthy_with_public_app_url(stack_public_url):
    # The fixture only yields once docker has reported the container `healthy`,
    # so the positive assertion is already satisfied. Confirm the negative:
    # an un-spoofed Host: 127.0.0.1 hits TrustHosts and gets 403 — proves the
    # healthcheck genuinely traversed (and survived) that middleware.
    req = urllib.request.Request(f"http://127.0.0.1:{stack_public_url['port']}/login")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            status = r.status
    except urllib.error.HTTPError as e:
        status = e.code
    assert status == 403, f"expected 403 from TrustHosts on un-spoofed Host, got {status}"


def test_healthcheck_healthy_with_db_down(stack_public_url):
    # Liveness semantics: the probe must NOT report unhealthy while the DB is
    # unreachable — restarting FreeScout can't fix its database. With an https
    # APP_URL the scheme redirect fires before anything touches the DB, so
    # /login answers 302 even mid-outage; with an http APP_URL it would be a
    # Laravel 500 — the script classifies both healthy. Also pins that the
    # probe emits a diagnostic line; the previous curl-only probe left empty
    # health-log entries when it failed, which was undebuggable in production.
    pg = stack_public_url["pg"]
    _sh("docker", "stop", pg)
    try:
        r = _exec(stack_public_url["fs"], "freescout-healthcheck")
        assert r.returncode == 0, (
            f"probe unhealthy with DB down: {r.stdout!r} {r.stderr!r}"
        )
        assert r.stdout.startswith("healthy: HTTP "), (
            f"expected 'healthy: HTTP <code>' diagnostic line, got {r.stdout!r}"
        )
    finally:
        _sh("docker", "start", pg)
        _wait_pg_ready(pg)


def test_happy_path_mariadb(stack_mariadb):
    # Healthcheck-style assertion: if `stack_mariadb` came up at all, the
    # guard accepted an empty MariaDB and migrations ran to completion —
    # which is the only end-to-end signal that Schema::getAllTables() /
    # Schema::hasTable() work on MariaDB the same as on pgsql. Hit /login
    # explicitly anyway to defend against the fixture's wait being subtly
    # short.
    with _http_get(f"http://127.0.0.1:{stack_mariadb['port']}/login") as r:
        assert r.status == 200


def _users_count(container):
    # `freescout-db-guard users-count` is the same probe the bootstrap
    # uses for the seed gate, so it directly exercises the production
    # codepath. Stdout contract: exactly one integer.
    r = _exec(container, "freescout-db-guard", "users-count", check=True)
    return int(r.stdout.strip())


def test_admin_not_reseeded_on_restart(stack_no_appkey):
    # Test the invariant directly: the user count must not change across
    # a restart. Asserting on a log line is unreliable here because
    # stack_no_appkey is session-scoped and other tests already restart
    # it — a stale 'skipping admin seed' from an earlier restart can
    # false-pass even if this restart reseeded.
    fs = stack_no_appkey["fs"]
    before = _users_count(fs)
    assert before == 1, (
        f"expected exactly one seeded admin before restart, got {before}"
    )
    _sh("docker", "restart", fs)
    port = _host_port(fs, "8080")
    try:
        _wait_http_200(f"http://127.0.0.1:{port}/login", READY_DEADLINE_S)
    except RuntimeError:
        print(_sh("docker", "logs", fs, check=False).stdout)
        print(_sh("docker", "logs", fs, check=False).stderr)
        raise
    after = _users_count(fs)
    assert after == before, (
        f"users count changed across restart: {before} -> {after}; "
        "admin appears to have been reseeded"
    )


@pytest.fixture
def module_stack():
    """Stack whose /data volume is pre-seeded with a minimal module BEFORE
    the first boot, mirroring a deployment with web-installed modules on a
    persistent volume. Function-scoped: the fake module must not leak into
    the shared session stacks."""
    suffix = secrets.token_hex(4)
    net = f"fs-net-{suffix}"
    pg = f"pg-{suffix}"
    fs = f"fs-{suffix}"
    vol = f"fs-data-{suffix}"
    app_key = "base64:" + base64.b64encode(secrets.token_bytes(32)).decode()
    containers = []

    def start_fs(name):
        containers.append(name)
        _sh(
            "docker", "run", "-d", "--name", name, "--network", net,
            "-e", f"APP_KEY={app_key}",
            "-e", "APP_URL=http://localhost:8080",
            "-e", "DB_TYPE=pgsql",
            "-e", f"DB_HOST={pg}",
            "-e", "DB_NAME=freescout",
            "-e", "DB_USER=postgres",
            "-e", "DB_PASS=test",
            "-e", "ADMIN_EMAIL=admin@smoke.local",
            "-e", "ADMIN_PASS=changeme",
            "-v", f"{vol}:/data",
            "-p", ":8080",
            IMAGE,
        )

    _sh("docker", "network", "create", net)
    _sh("docker", "volume", "create", vol)
    try:
        # Seed the module through the image itself so /data stays owned by
        # www-data. module.json carries the keys the module scanner reads;
        # Public/ holds the asset the public symlink must expose. Compact
        # single-line JSON on purpose: it is valid, occurs in the wild, and
        # defeats line-oriented alias extraction — the bootstrap must parse
        # it correctly (json_decode, not awk).
        module_json = json.dumps(
            {
                "name": "TestMod", "alias": "testmod", "description": "",
                "keywords": [], "active": 0, "order": 0, "providers": [],
                "aliases": {}, "files": [], "requires": [],
            },
        )
        # A real migration in the module: nwidart's module:migrate is a
        # silent no-op in this image (it strips base_path() off a path that
        # resolves into /data, so Laravel globs an empty directory and exits
        # 0) — the bootstrap must run module migrations via `migrate
        # --path`, and this probe table is how the tests notice if that
        # regresses back to the no-op. No single quotes in the PHP: the
        # whole file rides inside a single-quoted sh word.
        probe_migration = (
            "<?php\n"
            "use Illuminate\\Database\\Migrations\\Migration;\n"
            "use Illuminate\\Database\\Schema\\Blueprint;\n"
            "use Illuminate\\Support\\Facades\\Schema;\n"
            "class CreateTestmodProbeTable extends Migration {\n"
            "    public function up() { Schema::create(\"testmod_probe\","
            " function (Blueprint $table) { $table->increments(\"id\"); }); }\n"
            "    public function down() { Schema::dropIfExists(\"testmod_probe\"); }\n"
            "}\n"
        )
        _sh(
            "docker", "run", "--rm", "--entrypoint", "sh",
            "-v", f"{vol}:/data", IMAGE, "-c",
            "mkdir -p /data/Modules/TestMod/Public/css"
            " /data/Modules/TestMod/Database/Migrations && "
            f"printf '%s\\n' '{module_json}'"
            " > /data/Modules/TestMod/module.json && "
            f"printf '%s' '{probe_migration}'"
            " > /data/Modules/TestMod/Database/Migrations/"
            "2020_01_01_000000_create_testmod_probe_table.php && "
            "echo 'body{}' > /data/Modules/TestMod/Public/css/module.css",
        )
        _sh(
            "docker", "run", "-d", "--name", pg, "--network", net,
            "-e", "POSTGRES_PASSWORD=test",
            "-e", "POSTGRES_DB=freescout",
            "postgres:16",
        )
        _wait_pg_ready(pg)

        start_fs(fs)
        port = _host_port(fs, "8080")
        try:
            _wait_http_200(f"http://127.0.0.1:{port}/login", READY_DEADLINE_S)
        except RuntimeError:
            print(_sh("docker", "logs", fs, check=False).stdout)
            print(_sh("docker", "logs", fs, check=False).stderr)
            raise

        yield {"fs": fs, "pg": pg, "net": net, "port": port, "vol": vol,
               "start_fs": start_fs}
    finally:
        for name in containers + [pg]:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        subprocess.run(["docker", "network", "rm", net], capture_output=True)
        subprocess.run(["docker", "volume", "rm", vol], capture_output=True)


MODULE_LINK = "/var/www/html/public/modules/testmod"
MODULE_CSS = MODULE_LINK + "/css/module.css"


def test_module_public_symlink_seeded_across_fresh_containers(module_stack):
    # public/ lives in the image layer, so the public/modules/<alias>
    # symlinks vanish whenever the container is recreated (which on podman
    # quadlet/pod platforms is every restart AND every image update). Until
    # the bootstrap re-seeds them, every page referencing module assets
    # 500s in the minify provider ("File ... does not exist"). Assert the
    # links exist after first boot, reappear in a fresh container on the
    # same volume, and are seeded BEFORE the DB wait so the assets resolve
    # for the whole migration window.
    fs = module_stack["fs"]
    r = _exec(fs, "test", "-L", MODULE_LINK)
    assert r.returncode == 0, f"{MODULE_LINK} is not a symlink after first boot"
    r = _exec(fs, "test", "-f", MODULE_CSS)
    assert r.returncode == 0, "module css does not resolve through the symlink"

    # The module's probe migration must have actually run — guards against
    # module migrations regressing to the silent module:migrate no-op (see
    # the module_stack fixture comment).
    r = _exec(
        module_stack["pg"], "psql", "-U", "postgres", "-d", "freescout", "-tAc",
        "select 1 from information_schema.tables where table_name='testmod_probe'",
    )
    assert r.stdout.strip() == "1", (
        f"module probe migration did not run (psql said {r.stdout!r} {r.stderr!r})"
    )

    # Fresh container on the same volume, with the DB down: the bootstrap
    # parks in wait_for_db, so the symlink appearing now proves seeding
    # happens filesystem-only, before any DB dependency.
    _sh("docker", "rm", "-f", fs)
    _sh("docker", "stop", module_stack["pg"])
    fs2 = fs + "-fresh"
    module_stack["start_fs"](fs2)

    end = time.time() + 60
    while time.time() < end:
        if _exec(fs2, "test", "-L", MODULE_LINK).returncode == 0:
            break
        time.sleep(1)
    else:
        print(_sh("docker", "logs", fs2, check=False).stderr)
        pytest.fail(f"{MODULE_LINK} not re-seeded in fresh container while DB down")

    # Boot gate: while the bootstrap is parked in the DB wait, nginx must
    # answer 500 — not 2xx/3xx (the app is not ready) and not 502/503/504
    # (the liveness healthcheck would count those unhealthy and get a slow
    # first boot killed by the restart policy).
    port2 = _host_port(fs2, "8080")
    end = time.time() + 60
    status = None
    while time.time() < end:
        try:
            with _http_get(f"http://127.0.0.1:{port2}/login", timeout=5) as r:
                status = r.status
            break
        except urllib.error.HTTPError as e:
            status = e.code
            break
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            time.sleep(1)  # nginx not accepting yet
    if status is None:
        print(_sh("docker", "logs", fs2, check=False).stderr)
        pytest.fail("nginx never accepted a connection within 60s while DB down")
    assert status == 500, f"expected boot-gate 500 while DB down, got {status}"

    # Let the boot finish and confirm end-to-end resolution again.
    _sh("docker", "start", module_stack["pg"])
    _wait_pg_ready(module_stack["pg"])
    port = _host_port(fs2, "8080")
    try:
        _wait_http_200(f"http://127.0.0.1:{port}/login", READY_DEADLINE_S)
    except RuntimeError:
        print(_sh("docker", "logs", fs2, check=False).stdout)
        print(_sh("docker", "logs", fs2, check=False).stderr)
        raise
    r = _exec(fs2, "test", "-f", MODULE_CSS)
    assert r.returncode == 0, "module css does not resolve in fresh container"


def test_module_symlink_seeding_edge_cases():
    # Exercise step 2b's edge cases by running the bootstrap oneshot
    # directly with an unreachable DB and a 1s wait timeout: it dies at the
    # DB wait, but symlink seeding has already run by then. No DB stack
    # needed. Three modules, processed in glob order:
    #
    # - DangleMod (first): Public is a dangling symlink persisted on /data.
    #   `mkdir -p` on it fails, which under set -e would kill this and every
    #   later boot — the bootstrap must remove the dangling link and carry
    #   on (a regression aborts seeding here and fails the TestMod asserts).
    # - EvilMod: declared-but-invalid alias "../bad" must be skipped
    #   outright — no traversal link at public/bad, and no silent fallback
    #   link under the directory name (module-install would miss the module
    #   under a substitute alias while exiting 0).
    # - TestMod: a pre-existing real directory at public/modules/testmod.
    #   BusyBox `ln -sfn` against it exits 0 and creates the link INSIDE
    #   it; the bootstrap must move the directory aside first (mirroring
    #   upstream ModuleInstall).
    app_key = "base64:" + base64.b64encode(secrets.token_bytes(32)).decode()
    script = (
        "mkdir -p /data/Modules/DangleMod && "
        "printf '%s' '{\"name\":\"DangleMod\",\"alias\":\"danglemod\"}'"
        " > /data/Modules/DangleMod/module.json && "
        "ln -s /nonexistent-target /data/Modules/DangleMod/Public && "
        "mkdir -p /data/Modules/EvilMod/Public && "
        "printf '%s' '{\"name\":\"EvilMod\",\"alias\":\"../bad\"}'"
        " > /data/Modules/EvilMod/module.json && "
        "mkdir -p /data/Modules/TestMod/Public/css && "
        "printf '%s' '{\"name\":\"TestMod\",\"alias\":\"testmod\"}'"
        " > /data/Modules/TestMod/module.json && "
        "echo 'body{}' > /data/Modules/TestMod/Public/css/module.css && "
        "mkdir -p /var/www/html/public/modules/testmod && "
        "DB_WAIT_TIMEOUT=1 sh /usr/local/bin/freescout-bootstrap; "
        # Named checks: each failed post-condition prints its own CHECK
        # FAILED line so a regression identifies itself in the test output.
        "fail=0; "
        "ck() { \"$@\" || { echo \"CHECK FAILED: $*\" >&2; fail=1; }; }; "
        "ck test -L /var/www/html/public/modules/testmod; "
        "ck test -f /var/www/html/public/modules/testmod/css/module.css; "
        "ck test -L /var/www/html/public/modules/danglemod; "
        "ck test -d /data/Modules/DangleMod/Public; "
        "ck test ! -e /var/www/html/public/modules/evilmod; "
        "ck test ! -e /var/www/html/public/bad; "
        "exit $fail"
    )
    r = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "sh",
         "-e", f"APP_KEY={app_key}",
         "-e", "APP_URL=http://localhost:8080",
         "-e", "DB_TYPE=pgsql",
         "-e", "DB_HOST=127.0.0.1",
         "-e", "DB_NAME=x", "-e", "DB_USER=x", "-e", "DB_PASS=x",
         IMAGE, "-c", script],
        capture_output=True, text=True, timeout=180,
    )
    assert r.returncode == 0, (
        "module symlink-seeding edge cases failed — see CHECK FAILED lines\n"
        f"{r.stderr[-2000:]}"
    )


# ---------------------------------------------------------------------------
# Wrong-DB preflight tests. Each spins up a fresh DB sidecar, pre-populates
# it via `docker exec`, then runs the freescout container and waits for it
# to exit. The guard's job is to abort the boot before migrations corrupt
# someone else's database, so we assert on exit code + stderr content.
# ---------------------------------------------------------------------------

@pytest.fixture
def bad_db_stack():
    resources = {"networks": [], "containers": []}

    def factory(driver, setup_sql):
        suffix = secrets.token_hex(4)
        net = f"fs-net-{suffix}"
        db = f"db-{suffix}"
        fs = f"fs-{suffix}"
        resources["networks"].append(net)
        resources["containers"].extend([db, fs])

        _sh("docker", "network", "create", net)

        if driver == "pgsql":
            _sh(
                "docker", "run", "-d", "--name", db, "--network", net,
                "-e", "POSTGRES_PASSWORD=test",
                "-e", "POSTGRES_DB=freescout",
                "postgres:16",
            )
            _wait_pg_ready(db)
            # Force TCP; psql defaults to a unix socket the postgres
            # image doesn't bind in the locations psql probes.
            r = _exec(db, "psql", "-h", "127.0.0.1", "-U", "postgres",
                      "-d", "freescout", "-v", "ON_ERROR_STOP=1",
                      "-c", setup_sql)
            assert r.returncode == 0, (
                f"pgsql setup failed: stdout={r.stdout!r} stderr={r.stderr!r}"
            )
            db_env = ["-e", "DB_TYPE=pgsql", "-e", "DB_USER=postgres"]
        elif driver == "mariadb":
            _sh(
                "docker", "run", "-d", "--name", db, "--network", net,
                "-e", "MARIADB_ROOT_PASSWORD=test",
                "-e", "MARIADB_DATABASE=freescout",
                "mariadb:11",
            )
            _wait_mysql_ready(db)
            r = _exec(db, "mariadb", "-uroot", "-ptest", "freescout",
                      "-e", setup_sql)
            assert r.returncode == 0, (
                f"mariadb setup failed: stdout={r.stdout!r} stderr={r.stderr!r}"
            )
            db_env = ["-e", "DB_TYPE=mariadb", "-e", "DB_USER=root"]
        else:
            raise ValueError(f"unknown driver {driver!r}")

        app_key = "base64:" + base64.b64encode(secrets.token_bytes(32)).decode()
        _sh(
            "docker", "run", "-d", "--name", fs, "--network", net,
            "--restart=no",
            "-e", f"APP_KEY={app_key}",
            "-e", "APP_URL=http://localhost:8080",
            *db_env,
            "-e", f"DB_HOST={db}",
            "-e", "DB_NAME=freescout",
            "-e", "DB_PASS=test",
            IMAGE,
        )
        # docker wait blocks until the container exits and prints the
        # exit code on stdout. Timeout guards against a buggy guard that
        # hangs instead of aborting.
        try:
            w = subprocess.run(
                ["docker", "wait", fs],
                capture_output=True, text=True, timeout=180,
            )
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "kill", fs], capture_output=True)
            logs = _sh("docker", "logs", fs, check=False)
            raise RuntimeError(
                "freescout container did not exit within 180s; "
                f"logs:\n{logs.stdout}\n{logs.stderr}"
            )
        exit_code = int(w.stdout.strip())
        logs = _sh("docker", "logs", fs, check=False)
        return exit_code, logs.stdout + logs.stderr

    yield factory

    for name in resources["containers"]:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    for net in resources["networks"]:
        subprocess.run(["docker", "network", "rm", net], capture_output=True)


# Discovered FreeScout migration filename — kept in sync at build time by
# tests/test_image.py::test_freescout_create_mailboxes_migration_present.
FS_CREATE_MAILBOXES_MIG = "2018_06_25_065719_create_mailboxes_table"


def test_aborts_foreign_laravel_pgsql(bad_db_stack):
    # A non-FreeScout Laravel app: migrations table exists, the create-
    # mailboxes row is absent, and none of the FreeScout core tables are
    # present.
    sql = (
        "CREATE TABLE migrations ("
        "  id serial PRIMARY KEY,"
        "  migration varchar(255) NOT NULL,"
        "  batch int NOT NULL"
        "); "
        "INSERT INTO migrations (migration, batch) "
        "VALUES ('2099_01_01_000000_some_other_app', 1);"
    )
    code, logs = bad_db_stack("pgsql", sql)
    assert code != 0, f"guard should have aborted; logs:\n{logs}"
    assert "missing FreeScout core tables" in logs, (
        f"expected foreign-laravel diagnostic; logs:\n{logs}"
    )


def test_aborts_foreign_non_laravel_pgsql(bad_db_stack):
    code, logs = bad_db_stack("pgsql", "CREATE TABLE my_app_table (id int);")
    assert code != 0, f"guard should have aborted; logs:\n{logs}"
    assert "no Laravel migrations table" in logs, (
        f"expected foreign-non-laravel diagnostic; logs:\n{logs}"
    )


def test_aborts_foreign_non_laravel_mariadb(bad_db_stack):
    code, logs = bad_db_stack("mariadb", "CREATE TABLE my_app_table (id int);")
    assert code != 0, f"guard should have aborted; logs:\n{logs}"
    assert "no Laravel migrations table" in logs, (
        f"expected foreign-non-laravel diagnostic; logs:\n{logs}"
    )


def test_aborts_fake_mailboxes(bad_db_stack):
    # A `mailboxes` table alone isn't proof of FreeScout — the guard
    # demands the create_mailboxes_table migration row too. Without a
    # migrations table at all, this lands in the "no Laravel migrations
    # table" branch.
    code, logs = bad_db_stack("pgsql", "CREATE TABLE mailboxes (id int);")
    assert code != 0, f"guard should have aborted; logs:\n{logs}"
    assert "no Laravel migrations table" in logs, (
        f"expected non-laravel diagnostic for bare mailboxes; logs:\n{logs}"
    )


def test_aborts_partial_freescout(bad_db_stack):
    # mailboxes + conversations + migrations row, but threads and
    # customers are still missing — fingerprint must remain fail-closed.
    sql = (
        "CREATE TABLE migrations ("
        "  id serial PRIMARY KEY,"
        "  migration varchar(255) NOT NULL,"
        "  batch int NOT NULL"
        "); "
        f"INSERT INTO migrations (migration, batch) "
        f"VALUES ('{FS_CREATE_MAILBOXES_MIG}', 1); "
        "CREATE TABLE mailboxes (id int); "
        "CREATE TABLE conversations (id int);"
    )
    code, logs = bad_db_stack("pgsql", sql)
    assert code != 0, f"guard should have aborted; logs:\n{logs}"
    assert "missing FreeScout core tables: threads, customers" in logs, (
        f"expected partial-freescout diagnostic naming threads+customers; "
        f"logs:\n{logs}"
    )


def test_aborts_missing_fs_mig_row(bad_db_stack):
    # All four FS tables exist but the migrations table is empty — the
    # create_mailboxes_table row is what proves Laravel built this schema
    # from the FreeScout codebase. Without it, refuse.
    sql = (
        "CREATE TABLE migrations ("
        "  id serial PRIMARY KEY,"
        "  migration varchar(255) NOT NULL,"
        "  batch int NOT NULL"
        "); "
        "CREATE TABLE mailboxes (id int); "
        "CREATE TABLE conversations (id int); "
        "CREATE TABLE threads (id int); "
        "CREATE TABLE customers (id int);"
    )
    code, logs = bad_db_stack("pgsql", sql)
    assert code != 0, f"guard should have aborted; logs:\n{logs}"
    assert "missing FreeScout create_mailboxes_table migration row" in logs, (
        f"expected missing-mig-row diagnostic; logs:\n{logs}"
    )


# ---------------------------------------------------------------------------
# DB-availability resilience tests. With the default DB_WAIT_TIMEOUT=0 the
# bootstrap waits in place for the DB — a late or flapping Postgres must
# never require a container restart to recover (the incident this guards
# against). DB_WAIT_TIMEOUT>0 opts back into fail-fast. All containers run
# with --restart=no so any "recovery" observed is in-place, not a restart.
# ---------------------------------------------------------------------------

# > the old hardcoded 30s deadline; surviving this long proves the wait is
# no longer bounded by it.
BEYOND_OLD_DEADLINE_S = 45


def _run_freescout(net, name, db_host, *extra_env, db_pass="test"):
    app_key = "base64:" + base64.b64encode(secrets.token_bytes(32)).decode()
    _sh(
        "docker", "run", "-d", "--name", name, "--network", net,
        "--restart=no",
        "-e", f"APP_KEY={app_key}",
        "-e", "APP_URL=http://localhost:8080",
        "-e", "DB_TYPE=pgsql",
        "-e", f"DB_HOST={db_host}",
        "-e", "DB_NAME=freescout",
        "-e", "DB_USER=postgres",
        "-e", f"DB_PASS={db_pass}",
        "-p", ":8080",
        *extra_env,
        IMAGE,
    )


def _is_running(container):
    r = _sh("docker", "inspect", "--format", "{{.State.Running}}", container)
    return r.stdout.strip() == "true"


def _logs(container):
    r = _sh("docker", "logs", container, check=False)
    return r.stdout + r.stderr


@pytest.fixture
def db_wait_resources():
    resources = {"networks": [], "containers": []}
    yield resources
    for name in resources["containers"]:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    for net in resources["networks"]:
        subprocess.run(["docker", "network", "rm", net], capture_output=True)


def test_late_db_start_recovers_without_restart(db_wait_resources):
    # Postgres does not exist yet when freescout boots — DB_HOST doesn't even
    # resolve. The container must wait in place and come up on its own once
    # the DB appears.
    suffix = secrets.token_hex(4)
    net, pg, fs = f"fs-net-{suffix}", f"pg-{suffix}", f"fs-{suffix}"
    db_wait_resources["networks"].append(net)
    db_wait_resources["containers"].extend([pg, fs])

    _sh("docker", "network", "create", net)
    _run_freescout(
        net, fs, pg,
        "-e", "ADMIN_EMAIL=admin@smoke.local",
        "-e", "ADMIN_PASS=changeme",
    )
    time.sleep(BEYOND_OLD_DEADLINE_S)
    assert _is_running(fs), (
        f"container died while DB was absent; logs:\n{_logs(fs)}"
    )
    assert "waiting for pgsql" in _logs(fs), (
        f"expected wait-loop progress logging; logs:\n{_logs(fs)}"
    )

    _sh(
        "docker", "run", "-d", "--name", pg, "--network", net,
        "-e", "POSTGRES_PASSWORD=test",
        "-e", "POSTGRES_DB=freescout",
        "postgres:16",
    )
    port = _host_port(fs, "8080")
    try:
        _wait_http_200(f"http://127.0.0.1:{port}/login", READY_DEADLINE_S)
    except RuntimeError:
        print(_logs(fs))
        raise
    r = _sh("docker", "inspect", "--format", "{{.RestartCount}}", fs)
    assert r.stdout.strip() == "0", "recovery must not depend on a restart"


def test_db_down_at_boot_recovers_when_db_returns(db_wait_resources):
    # Flap variant: a previously-initialized freescout re-boots while its DB
    # is down (the exact PikaPods incident shape). It must wait, then finish
    # the boot once the DB is back.
    suffix = secrets.token_hex(4)
    net, pg, fs = f"fs-net-{suffix}", f"pg-{suffix}", f"fs-{suffix}"
    db_wait_resources["networks"].append(net)
    db_wait_resources["containers"].extend([pg, fs])

    _sh("docker", "network", "create", net)
    _sh(
        "docker", "run", "-d", "--name", pg, "--network", net,
        "-e", "POSTGRES_PASSWORD=test",
        "-e", "POSTGRES_DB=freescout",
        "postgres:16",
    )
    _wait_pg_ready(pg)
    _run_freescout(net, fs, pg)
    port = _host_port(fs, "8080")
    try:
        _wait_http_200(f"http://127.0.0.1:{port}/login", READY_DEADLINE_S)
    except RuntimeError:
        print(_logs(fs))
        raise

    _sh("docker", "stop", pg)
    _sh("docker", "restart", fs)
    time.sleep(BEYOND_OLD_DEADLINE_S)
    assert _is_running(fs), (
        f"re-boot with DB down must wait, not die; logs:\n{_logs(fs)}"
    )

    _sh("docker", "start", pg)
    port = _host_port(fs, "8080")  # ephemeral port may change on restart
    try:
        _wait_http_200(f"http://127.0.0.1:{port}/login", READY_DEADLINE_S)
    except RuntimeError:
        print(_logs(fs))
        raise


def test_db_wait_timeout_fails_fast(db_wait_resources):
    # Opt-in fail-fast: with DB_WAIT_TIMEOUT>0 and no DB, the container must
    # exit non-zero (S6_BEHAVIOUR_IF_STAGE2_FAILS=2 halts on oneshot failure)
    # so a restart policy can take over.
    suffix = secrets.token_hex(4)
    net, fs = f"fs-net-{suffix}", f"fs-{suffix}"
    db_wait_resources["networks"].append(net)
    db_wait_resources["containers"].append(fs)

    _sh("docker", "network", "create", net)
    _run_freescout(net, fs, "no-such-db-host", "-e", "DB_WAIT_TIMEOUT=15")
    try:
        w = subprocess.run(
            ["docker", "wait", fs],
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "kill", fs], capture_output=True)
        raise RuntimeError(
            f"container did not fail fast with DB_WAIT_TIMEOUT=15; logs:\n{_logs(fs)}"
        )
    assert int(w.stdout.strip()) != 0, f"expected non-zero exit; logs:\n{_logs(fs)}"
    assert "DB not ready after" in _logs(fs), (
        f"expected timeout diagnostic with last error; logs:\n{_logs(fs)}"
    )


def test_wrong_password_waits_with_credential_hint(db_wait_resources):
    # Credential rejection is retryable by design (a not-yet-provisioned role
    # is indistinguishable from a typo'd password), but the logs must carry
    # an unambiguous hint instead of a generic connection error.
    suffix = secrets.token_hex(4)
    net, pg, fs = f"fs-net-{suffix}", f"pg-{suffix}", f"fs-{suffix}"
    db_wait_resources["networks"].append(net)
    db_wait_resources["containers"].extend([pg, fs])

    _sh("docker", "network", "create", net)
    _sh(
        "docker", "run", "-d", "--name", pg, "--network", net,
        "-e", "POSTGRES_PASSWORD=test",
        "-e", "POSTGRES_DB=freescout",
        "postgres:16",
    )
    _wait_pg_ready(pg)
    _run_freescout(net, fs, pg, db_pass="wrong")
    time.sleep(BEYOND_OLD_DEADLINE_S)
    assert _is_running(fs), (
        f"wrong credentials must keep retrying, not kill the container; "
        f"logs:\n{_logs(fs)}"
    )
    assert "check DB_USER/DB_PASS/DB_NAME" in _logs(fs), (
        f"expected credential hint in wait-loop logs; logs:\n{_logs(fs)}"
    )
