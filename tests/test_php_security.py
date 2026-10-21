import pytest


@pytest.mark.parametrize("version", [
    "8.2.35", "8.3.36", "8.4.27", "8.5.12", "8.4.28",
])
def test_patched_stable_release_accepted(assert_patched_php, version):
    assert_patched_php(version)


@pytest.mark.parametrize("version", [
    "8.2.34", "8.3.35", "8.4.26", "8.5.11",
])
def test_unpatched_release_rejected(assert_patched_php, version):
    with pytest.raises(AssertionError, match="is vulnerable"):
        assert_patched_php(version)


@pytest.mark.parametrize("version", ["8.1.34", "8.6.0", "9.0.0"])
def test_unreviewed_branch_rejected(assert_patched_php, version):
    with pytest.raises(AssertionError, match="unreviewed PHP branch"):
        assert_patched_php(version)


@pytest.mark.parametrize("version", ["8.4.27RC1", "8.4.27-dev", "", "8.4"])
def test_non_stable_release_rejected(assert_patched_php, version):
    with pytest.raises(AssertionError, match="expected a stable PHP release"):
        assert_patched_php(version)
