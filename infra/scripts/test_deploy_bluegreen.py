"""deploy.sh blue/green (#25) : faux docker/curl dans le PATH, appels enregistrés, aucun vrai conteneur."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
IMG = "ghcr.io/example/vva-dashboard"

# Journalise « docker <args> » ; FAKE_UNHEALTHY=<tag> : exec échoue pour ce tag ; FAKE_LEGACY=1 : ancien conteneur.
# exec renvoie la version lue dans IMAGE_TAG (= tag demandé par deploy.sh).
FAKE_DOCKER = """#!/usr/bin/env bash
echo "docker $*" >> "$FAKE_LOG"
case "$*" in
  *" up "*) echo "$IMAGE_TAG" > "$FAKE_LOG.served" ;;
esac
case "$*" in
  *" exec "*)
    [ "${IMAGE_TAG:-}" = "${FAKE_UNHEALTHY:-none}" ] && exit 1
    echo '{"version":"'"$IMAGE_TAG"'"}' ;;
  *" ps -q dashboard_"*)
    for sl in $FAKE_RUNNING; do case "$*" in *"dashboard_$sl") echo "id-$sl" ;; esac; done ;;
  "ps -a -q"*) [ "${FAKE_LEGACY:-0}" = "1" ] && echo legacy123 ;;
esac
exit 0
"""
# Public : sert le dernier tag démarré (« up ») ; FAKE_BAD=<tag> : ce tag répond sans page valide.
# FAKE_BAD_PAGE=<tag> : /healthz correct mais la page d'accueil est invalide.
FAKE_CURL = """#!/usr/bin/env bash
echo "curl $*" >> "$FAKE_LOG"
v="$(cat "$FAKE_LOG.served" 2>/dev/null)"
[ "$v" = "${FAKE_BAD:-none}" ] && exit 22
case "$*" in
  */healthz)
    if [ -n "${FAKE_ALT_OLD:-}" ]; then # round-robin : une réponse sur deux vient de l'ancien slot
      n=$(( $(cat "$FAKE_LOG.n" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$FAKE_LOG.n"
      [ $((n % 2)) -eq 1 ] && v="$FAKE_ALT_OLD"
    fi
    echo '{"version":"'"$v"'"}' ;;
  *) [ "$v" = "${FAKE_BAD_PAGE:-none}" ] && echo "erreur" || echo "Agents Dashboard" ;;
esac
"""


def _fake(bin_dir: Path, name: str, body: str) -> None:
    (bin_dir / name).write_text(body)
    (bin_dir / name).chmod(0o755)


@pytest.fixture
def sb(tmp_path: Path) -> dict[str, Any]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake(bin_dir, "docker", FAKE_DOCKER)
    _fake(bin_dir, "curl", FAKE_CURL)
    _fake(bin_dir, "sleep", "#!/usr/bin/env bash\nexit 0\n")  # tests rapides
    state = tmp_path / "state"
    state.mkdir()
    (state / "proxy.env").write_text("DASHBOARD_HOST=dashboard.test\n")
    log = tmp_path / "docker.log"
    log.write_text("")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "VVA_STATE_DIR": str(state),
        "FAKE_LOG": str(log),
        "VVA_PROBE": "0",
        "FAKE_RUNNING": "",
    }
    return {"state": state, "env": env, "log": log}


def run(sb: dict[str, Any], version: str = "2.0.0", **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["bash", str(DEPLOY / "deploy.sh"), IMG, version],  # noqa: S607
        env={**sb["env"], **extra},
        capture_output=True,
        text=True,
        check=False,
    )


def calls(sb: dict[str, Any]) -> list[str]:
    return str(sb["log"].read_text()).splitlines()


def idx(lines: list[str], *needles: str) -> int:
    for i, line in enumerate(lines):
        if all(n in line for n in needles):
            return i
    raise AssertionError(f"absent : {needles}\n" + "\n".join(lines))


def seed(sb: dict[str, Any], slot: str, good: str) -> None:
    (sb["state"] / "slot").write_text(slot + "\n")
    sb["env"]["FAKE_RUNNING"] = slot
    (sb["state"] / "last_good").write_text(good + "\n")


def test_success_starts_new_slot_before_stopping_old(sb: dict[str, Any]) -> None:
    seed(sb, "blue", "1.0.0")
    res = run(sb)
    assert res.returncode == 0, res.stdout + res.stderr
    lines = calls(sb)
    assert idx(lines, "up -d", "dashboard_green") < idx(lines, "stop", "dashboard_blue")
    assert idx(lines, "exec", "dashboard_green") < idx(lines, "stop", "dashboard_blue")
    assert (sb["state"] / "slot").read_text().strip() == "green"
    assert (sb["state"] / "last_good").read_text().strip() == "2.0.0"
    assert "INGEST" not in res.stdout + res.stderr


def test_unhealthy_new_slot_never_stops_old(sb: dict[str, Any]) -> None:
    seed(sb, "blue", "1.0.0")
    res = run(sb, FAKE_UNHEALTHY="2.0.0")
    assert res.returncode != 0
    lines = calls(sb)
    assert not any("stop" in x and "dashboard_blue" in x for x in lines)
    assert any("stop" in x and "dashboard_green" in x for x in lines)  # slot raté nettoyé
    assert (sb["state"] / "slot").read_text().strip() == "blue"
    assert (sb["state"] / "last_good").read_text().strip() == "1.0.0"


def test_public_smoke_failure_rolls_back_to_old_tag(sb: dict[str, Any]) -> None:
    seed(sb, "blue", "1.0.0")
    res = run(sb, FAKE_BAD_PAGE="2.0.0")  # healthz OK, page KO
    assert res.returncode != 0
    assert "Rollback" in res.stdout
    lines = calls(sb)
    stop_old = idx(lines, "stop", "dashboard_blue")
    rb = idx(lines[stop_old:], "up -d", "dashboard_blue") + stop_old  # ancien tag, ancien slot
    assert idx(lines[rb:], "stop", "dashboard_green") >= 0  # puis arrêt du nouveau
    assert "Rollback OK" in res.stdout
    assert (sb["state"] / "last_good").read_text().strip() == "1.0.0"
    assert (sb["state"] / "slot").read_text().strip() == "blue"


def test_public_healthz_awaited_three_times_before_stopping_old(sb: dict[str, Any]) -> None:
    seed(sb, "blue", "1.0.0")
    res = run(sb)
    assert res.returncode == 0, res.stdout + res.stderr
    lines = calls(sb)
    stop_old = idx(lines, "stop", "dashboard_blue")
    before = [x for x in lines[:stop_old] if x.startswith("curl") and x.endswith("/healthz")]
    assert len(before) >= 3


def test_public_round_robin_alternating_versions_passes(sb: dict[str, Any]) -> None:
    seed(sb, "blue", "1.0.0")
    res = run(sb, FAKE_ALT_OLD="1.0.0")
    assert res.returncode == 0, res.stdout + res.stderr
    assert (sb["state"] / "slot").read_text().strip() == "green"


def test_public_never_serves_new_version_keeps_old(sb: dict[str, Any]) -> None:
    seed(sb, "blue", "1.0.0")
    res = run(sb, FAKE_BAD="2.0.0")
    assert res.returncode != 0
    assert not any("stop" in x and "dashboard_blue" in x for x in calls(sb))
    assert (sb["state"] / "last_good").read_text().strip() == "1.0.0"


def test_slot_file_absent_with_both_slots_present(sb: dict[str, Any]) -> None:
    (sb["state"] / "last_good").write_text("1.0.0\n")
    res = run(sb, FAKE_RUNNING="blue green")
    assert res.returncode == 0, res.stdout + res.stderr
    lines = calls(sb)
    # new = blue (pas d'indice) ; green, présent mais != new, est arrêté
    assert idx(lines, "up -d", "dashboard_blue") < idx(lines, "stop", "dashboard_green")


def test_failed_rollback_is_explicit_and_leaves_no_slot_hint(sb: dict[str, Any]) -> None:
    seed(sb, "blue", "1.0.0")
    res = run(sb, FAKE_BAD_PAGE="2.0.0", FAKE_UNHEALTHY="1.0.0")
    assert res.returncode != 0
    assert "::error::Rollback vers 1.0.0 KO" in res.stdout
    assert not (sb["state"] / "slot").exists()
    assert any("--pull missing" in x for x in calls(sb))


def test_migration_from_single_legacy_container(sb: dict[str, Any]) -> None:
    (sb["state"] / "last_good").write_text("1.0.0\n")
    res = run(sb, FAKE_LEGACY="1")
    assert res.returncode == 0, res.stdout + res.stderr
    lines = calls(sb)
    assert idx(lines, "up -d", "dashboard_blue") < idx(lines, "docker stop", "legacy123")
    assert idx(lines, "docker stop", "legacy123") < idx(lines, "docker rm legacy123")
    assert (sb["state"] / "slot").read_text().strip() == "blue"


def test_compose_has_retry_middleware_and_single_label_set() -> None:
    text = (DEPLOY / "compose.yaml").read_text()
    assert "retry.attempts=3" in text
    assert text.count("vva-dashboard.rule=") == 1


def test_compose_has_two_slots_sharing_traefik_service() -> None:
    text = (DEPLOY / "compose.yaml").read_text()
    assert "dashboard_blue:" in text and "dashboard_green:" in text
    assert text.count("loadbalancer.server.port=8000") == 1  # défini une fois (ancre)
