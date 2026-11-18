from next_version import bump


def test_feat_bumps_minor() -> None:
    assert bump("0.1.2", ["feat(dashboard): page vide"]) == "0.2.0"


def test_fix_bumps_patch() -> None:
    assert bump("1.2.3", ["fix: typo", "docs: x"]) == "1.2.4"


def test_breaking_before_1_bumps_minor() -> None:
    assert bump("0.3.0", ["feat!: nouveau contrat"]) == "0.4.0"


def test_breaking_bumps_major() -> None:
    assert bump("1.3.0", ["refactor: x\n\nBREAKING CHANGE: api"]) == "2.0.0"


def test_nothing_to_release() -> None:
    assert bump("0.1.0", ["docs: readme", "chore: deps"]) is None
