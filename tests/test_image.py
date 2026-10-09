import json
import os
import re
import subprocess

import pytest

IMAGE = os.environ["IMAGE"]


def _inspect():
    out = subprocess.run(
        ["docker", "inspect", IMAGE],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)[0]


@pytest.fixture(scope="session")
def inspect():
    return _inspect()


def _run(*args, check=False):
    return subprocess.run(
        ["docker", "run", "--rm", "--entrypoint=", IMAGE, *args],
        capture_output=True, text=True, check=check,
    )


class TestImageMetadata:
    def test_required_oci_labels(self, inspect):
        labels = inspect["Config"].get("Labels") or {}
        for key in (
            "org.opencontainers.image.source",
            "org.opencontainers.image.version",
            "org.opencontainers.image.licenses",
            "org.opencontainers.image.title",
        ):
            assert labels.get(key), f"missing OCI label: {key}"

    def test_runs_as_non_root(self, inspect):
        user = inspect["Config"].get("User", "")
        assert user in ("www-data", "82"), f"expected www-data/82, got {user!r}"

    def test_healthcheck_defined(self, inspect):
        assert inspect["Config"].get("Healthcheck"), "no Healthcheck defined"

    def test_exposes_8080(self, inspect):
        ports = inspect["Config"].get("ExposedPorts") or {}
        assert "8080/tcp" in ports, f"8080/tcp not exposed; got {list(ports)}"

    def test_image_size_under_limit(self, inspect):
        size_mb = inspect["Size"] / (1024 * 1024)
        assert size_mb < 1500, f"image size {size_mb:.0f} MB exceeds 1500 MB guardrail"

    def test_default_env_present(self, inspect):
        env = dict(e.split("=", 1) for e in inspect["Config"].get("Env") or [])
        assert env.get("AUTORUN_ENABLED") == "false"
        assert env.get("SSL_MODE") == "off"
        assert env.get("ENABLE_FREESCOUT_SCHEDULER") == "TRUE"
        assert env.get("APP_BASE_DIR") == "/var/www/html"


class TestImageFilesystem:
    @pytest.mark.parametrize("link,target", [
        ("/var/www/html/storage", "/data/storage"),
        ("/var/www/html/Modules", "/data/Modules"),
        ("/var/www/html/.env", "/data/config"),
    ])
    def test_data_symlinks(self, link, target):
        r = _run("readlink", link)
        assert r.returncode == 0, f"readlink {link} failed: {r.stderr}"
        assert r.stdout.strip() == target, f"{link} -> {r.stdout.strip()!r}, expected {target!r}"

    def test_data_dir_owned_by_www_data(self):
        r = _run("stat", "-c", "%U:%G", "/data")
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "www-data:www-data"

    @pytest.mark.parametrize("binary", [
        "php", "nginx", "composer", "pg_isready", "mysqladmin", "curl", "git",
    ])
    def test_runtime_binaries_present(self, binary):
        r = _run("which", binary)
        assert r.returncode == 0, f"{binary} not found on PATH"
        assert r.stdout.strip(), f"which {binary} returned empty"

    @pytest.mark.parametrize("ext", [
        "pdo_pgsql", "pgsql", "pdo_mysql", "gnupg", "imap", "intl",
        "bcmath", "gd", "exif", "pcntl", "Zend OPcache", "redis",
    ])
    def test_php_extensions_loaded(self, ext):
        r = _run("php", "-m")
        assert r.returncode == 0, r.stderr
        modules = {line.strip() for line in r.stdout.splitlines() if line.strip()}
        assert ext in modules, f"PHP module {ext!r} not loaded; got {sorted(modules)}"

    def test_s6_scheduler_run_executable(self):
        r = _run("test", "-x", "/etc/s6-overlay/s6-rc.d/freescout-scheduler/run")
        assert r.returncode == 0, "freescout-scheduler run script missing or not executable"

    def test_s6_scheduler_depends_on_bootstrap(self):
        r = _run(
            "test", "-f",
            "/etc/s6-overlay/s6-rc.d/freescout-scheduler/dependencies.d/freescout-bootstrap",
        )
        assert r.returncode == 0, "scheduler dependency marker on bootstrap missing"

    def test_s6_dependency_closure(self):
        # nginx/php-fpm must start after the config oneshots (a fresh container
        # whose nginx wins the race against 10-init-webserver-config loads a
        # server-less config and wedges), but never after the bootstrap: nginx
        # has to serve the boot gate and /healthcheck while it waits for the DB.
        r = _run("sh", "-c",
                 "cd /etc/s6-overlay/s6-rc.d && "
                 "for f in */dependencies.d/*; do echo \"$f\"; done")
        assert r.returncode == 0, r.stderr
        deps = {}
        for line in r.stdout.split():
            svc, _, dep = line.split("/")
            deps.setdefault(svc, set()).add(dep)

        def closure(svc):
            seen, stack = set(), [svc]
            while stack:
                for d in deps.get(stack.pop(), ()):
                    if d not in seen:
                        seen.add(d)
                        stack.append(d)
            return seen

        nginx, fpm = closure("nginx"), closure("php-fpm")
        assert "10-init-webserver-config" in nginx, sorted(nginx)
        assert "5-fpm-pool-user" in fpm, sorted(fpm)
        assert "freescout-bootstrap" not in nginx, sorted(nginx)
        assert "freescout-bootstrap" not in fpm, sorted(fpm)

    def test_s6_user_bundle_layout(self):
        # s6-overlay >= 3.2.3 ignores user-bundles.d entirely if the legacy
        # s6-rc.d/user exists, then fails writing its type file as www-data.
        r = _run("sh", "-c",
                 "test ! -e /etc/s6-overlay/s6-rc.d/user && "
                 "cd /etc/s6-overlay/user-bundles.d/user/contents.d && "
                 "test -f freescout-bootstrap && test -f freescout-scheduler")
        assert r.returncode == 0, "s6 user bundle layout wrong"

    def test_s6_bootstrap_oneshot_installed(self):
        r = _run("test", "-x", "/usr/local/bin/freescout-bootstrap")
        assert r.returncode == 0, "freescout-bootstrap missing or not executable"
        r = _run("cat", "/etc/s6-overlay/s6-rc.d/freescout-bootstrap/type")
        assert r.stdout.strip() == "oneshot", r.stdout

    def test_boot_gate_sentinel_shipped_raised(self):
        # The boot gate must be closed from the very first request of a
        # fresh container: the sentinel ships in the image and the bootstrap
        # removes it as its final step.
        r = _run("test", "-f", "/var/www/html/.freescout-bootstrap-incomplete")
        assert r.returncode == 0, "boot-gate sentinel missing from image"

    def test_boot_gate_nginx_conf_matches_sentinel(self):
        r = _run("cat", "/etc/nginx/server-opts.d/00-freescout-bootstrap-gate.conf")
        assert r.returncode == 0, "boot-gate nginx conf missing"
        assert "/var/www/html/.freescout-bootstrap-incomplete" in r.stdout, (
            "gate conf does not reference the sentinel path"
        )
        assert "return 500" in r.stdout, (
            "gate must answer 500 — 503 would trip the liveness healthcheck"
        )
        assert "$uri = /healthcheck" in r.stdout, (
            "gate must exempt php-fpm's /healthcheck ping path, or the s6 "
            "nginx readiness check never succeeds and the container cannot "
            "halt on bootstrap failure"
        )

    def test_freescout_app_present(self):
        for path in ("/var/www/html/artisan", "/var/www/html/composer.json"):
            r = _run("test", "-f", path)
            assert r.returncode == 0, f"{path} missing"

    def test_freescout_create_mailboxes_migration_present(self):
        # The DB guard discovers the create_mailboxes migration filename at
        # runtime via glob — if upstream renames or squashes it, the guard
        # exits 2 and the container won't boot. Fail at image-build time
        # instead so we catch drift before deploy.
        r = _run(
            "sh", "-c",
            "ls /var/www/html/database/migrations/*_create_mailboxes_table.php "
            "2>/dev/null | wc -l",
        )
        assert r.returncode == 0, r.stderr
        count = int(r.stdout.strip())
        assert count == 1, (
            f"expected exactly one *_create_mailboxes_table.php migration, "
            f"got {count}"
        )

    def test_db_guard_installed_executable(self):
        path = "/usr/local/bin/freescout-db-guard"
        r = _run("test", "-x", path)
        assert r.returncode == 0, f"{path} missing or not executable"
        r = _run(path)
        # No-args invocation hits the usage branch and exits 2 with a
        # usage line on stderr.
        assert r.returncode == 2, f"expected exit 2 from no-args, got {r.returncode}"
        assert "usage: freescout-db-guard" in r.stderr, (
            f"usage line missing from stderr: {r.stderr!r}"
        )


def _rebuild_build_args(labels):
    """Recover the build args of the image under test from its OCI labels.

    The rebuild below must reproduce the image under test, not the Dockerfile
    defaults. Passing only WWW_DATA_UID/GID once had CI rebuilding a stale
    FreeScout against an unpinned base and asserting against that instead of
    the artifact that ships.
    """
    version = labels.get("org.opencontainers.image.version", "")
    m = re.fullmatch(r"(?P<version>.+)-r\d+", version)
    if not m:
        pytest.fail(
            "cannot derive FREESCOUT_VERSION: label "
            f"org.opencontainers.image.version={version!r} is missing or not "
            "in <version>-r<n> form"
        )
    args = [f"FREESCOUT_VERSION={m.group('version')}"]

    # BASE_IMAGE alone pins the base, so PHP_VERSION is not worth deriving:
    # post-FROM it only feeds the base.name label, and the base image's own
    # ENV PHP_VERSION shadows the ARG there anyway (which is why base.name
    # carries the patch version, e.g. 8.4.23, not the 8.4 CI passes).
    #
    # base.digest is empty for local builds (ARG BASE_DIGEST=); fall back to
    # the tag-based reference so those still rebuild against the same base.
    base_name = labels.get("org.opencontainers.image.base.name", "")
    repo, _, _ = base_name.partition(":")
    digest = labels.get("org.opencontainers.image.base.digest", "")
    if repo and digest:
        args.append(f"BASE_IMAGE={repo}@{digest}")
    elif base_name:
        args.append(f"BASE_IMAGE={base_name}")

    return args


# Marked runtime: a full image rebuild (~30s with buildkit cache, minutes
# cold). Lives outside TestImageFilesystem so `-m 'not runtime'` keeps the
# fast image lane fast.
@pytest.mark.runtime
class TestCustomUidRebuild:
    """Rebuild with --build-arg WWW_DATA_UID/GID and verify the new UID
    actually owns /data. Regression guard: set-file-permissions only touches
    a hardcoded path list, so /data needs an explicit chown in the Dockerfile
    or the rebuilt image's www-data can't write to its own volume.
    """

    UID = "1000"
    GID = "1000"

    @pytest.fixture(scope="class")
    def image(self):
        ctx = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        tag = f"fs-uid{self.UID}-test"
        build_args = _rebuild_build_args(_inspect()["Config"].get("Labels") or {}) + [
            f"WWW_DATA_UID={self.UID}",
            f"WWW_DATA_GID={self.GID}",
        ]
        flags = [f for arg in build_args for f in ("--build-arg", arg)]
        r = subprocess.run(
            ["docker", "build", *flags, "-t", tag, ctx],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            pytest.fail(
                f"docker build failed (rc={r.returncode})\n"
                f"build args: {build_args}\n"
                f"--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}"
            )
        try:
            yield tag
        finally:
            subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)

    def test_www_data_user_remapped(self, image):
        r = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint=", image,
             "id", "-u", "www-data"],
            capture_output=True, text=True, check=True,
        )
        assert r.stdout.strip() == self.UID

    def test_data_dir_remapped(self, image):
        r = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint=", image,
             "stat", "-c", "%u:%g", "/data"],
            capture_output=True, text=True, check=True,
        )
        assert r.stdout.strip() == f"{self.UID}:{self.GID}"
