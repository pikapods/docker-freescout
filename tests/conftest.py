import re

import pytest


@pytest.fixture
def assert_patched_php():
    def check(version):
        # CVE-2026-109618 / CVE-2026-109619. Require stable releases and
        # review the security floor explicitly before accepting another branch.
        minimum_patches = {(8, 2): 35, (8, 3): 36, (8, 4): 27, (8, 5): 12}
        match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
        assert match, f"expected a stable PHP release, got {version!r}"
        major, minor, patch = map(int, match.groups())
        branch = (major, minor)
        assert branch in minimum_patches, f"unreviewed PHP branch: {version}"
        minimum = minimum_patches[branch]
        assert patch >= minimum, (
            f"PHP {version} is vulnerable; require {major}.{minor}.{minimum}+"
        )

    return check


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "runtime: tests that boot the container against a real database "
        "(slow; require docker daemon). Skip with `-m 'not runtime'`.",
    )
