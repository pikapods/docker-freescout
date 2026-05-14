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
READY_DEADLINE_S = 180
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
    end = time.time() + deadline_s
    while time.time() < end:
        if _exec(container, "pg_isready", "-U", "postgres").returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError(f"postgres container {container} not ready within {deadline_s}s")


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
            "-p", "0:8080",
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


def test_login_responds_200(stack):
    with _http_get(f"http://127.0.0.1:{stack['port']}/login") as r:
        assert r.status == 200
        body = r.read().decode("utf-8", errors="replace")
    # Cheap content sanity — FreeScout's login template renders a password field.
    assert 'type="password"' in body or "password" in body.lower()


def test_logs_clean(stack):
    logs = _sh("docker", "logs", stack["fs"], check=False)
    combined = logs.stdout + logs.stderr
    bad = re.findall(r"RuntimeException|PHP Fatal", combined)
    assert not bad, f"bad patterns in container logs: {bad[:5]}"


def test_scheduler_longrun_alive(stack):
    # The scheduler longrun is a `while :;` shell loop; the process is
    # always present unless s6 has given up restarting it.
    # Plain `ps` (no flags) is the BusyBox-safe form — `-ef` is not
    # portably supported. The `[f]…` bracket trick keeps the grep
    # process from matching itself without needing `grep -v grep`.
    r = _exec(
        stack["fs"], "sh", "-c",
        "ps | grep '[f]reescout-scheduler/run'",
    )
    assert r.returncode == 0, (
        "scheduler longrun process not found "
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
