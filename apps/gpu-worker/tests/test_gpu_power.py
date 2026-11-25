"""gpu_power (#37) : réveil à la demande et veille après inactivité, pc-gpu et horloge simulés."""

from __future__ import annotations

import logging
import subprocess

import gpu_power
import pytest
from gpu_power import GpuPower, GpuUnavailable, Result

CHILD_TEXT = "dictée-secrète-de-l-enfant"


@pytest.fixture(autouse=True)
def _no_real_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Le vrai pc-gpu (donc le vrai PC 192.168.1.14) ne doit jamais être lancé par un test."""

    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("appel réel à subprocess : interdit dans les tests")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)


class FakePcGpu:
    """Simule la commande pc-gpu : `statuses` est consommé à chaque `status` (le dernier reste)."""

    def __init__(self, *statuses: str, wake_rc: int = 0, status_rc: int = 0) -> None:
        self.statuses = list(statuses)
        self.wake_rc = wake_rc
        self.status_rc = status_rc
        self.calls: list[str] = []
        self.timeouts: list[float] = []

    def __call__(self, args: list[str], timeout: float) -> Result:
        assert args[0].endswith("pc-gpu") and len(args) == 2
        verb = args[1]
        self.calls.append(verb)
        self.timeouts.append(timeout)
        if verb == "status":
            out = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            return Result(self.status_rc, out)
        if verb == "wake":
            return Result(self.wake_rc, "ok")
        return Result(0, "mise en veille")

    def count(self, verb: str) -> int:
        return self.calls.count(verb)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


def _power(pc: FakePcGpu, clock: Clock, idle_s: int = 600) -> GpuPower:
    return GpuPower(run=pc, clock=clock, idle_s=idle_s)


def test_reveil_a_la_premiere_dictee(clock: Clock) -> None:
    pc = FakePcGpu("endormi ou injoignable", "eveille")
    _power(pc, clock).ensure_awake()
    assert pc.count("wake") == 1
    assert pc.calls[-1] == "status"  # rend la main une fois le PC vu éveillé
    assert max(pc.timeouts) <= 120


def test_pc_deja_eveille_pas_de_wake(clock: Clock) -> None:
    pc = FakePcGpu("eveille")
    power = _power(pc, clock)
    power.ensure_awake()
    power.ensure_awake()
    assert pc.count("wake") == 0


@pytest.mark.parametrize("wake_rc", [1, 2])
def test_reveil_impossible_leve_gpu_unavailable(clock: Clock, wake_rc: int) -> None:
    pc = FakePcGpu("endormi ou injoignable", wake_rc=wake_rc)
    with pytest.raises(GpuUnavailable):
        _power(pc, clock).ensure_awake()
    assert pc.count("wake") == 1  # pas de boucle


def test_reveil_depassant_le_delai_leve_gpu_unavailable(clock: Clock) -> None:
    def timeout_run(args: list[str], timeout: float) -> Result:
        if args[1] == "wake":
            raise subprocess.TimeoutExpired(args, timeout)
        return Result(0, "endormi ou injoignable")

    with pytest.raises(GpuUnavailable):
        GpuPower(run=timeout_run, clock=clock).ensure_awake()


def test_reveil_sans_pc_eveille_apres_wake_leve_gpu_unavailable(clock: Clock) -> None:
    pc = FakePcGpu("endormi ou injoignable")  # wake « réussit » mais le PC reste muet
    with pytest.raises(GpuUnavailable):
        _power(pc, clock).ensure_awake()


def test_message_gpu_unavailable_sans_donnee_enfant(clock: Clock) -> None:
    def leaky(args: list[str], timeout: float) -> Result:
        raise OSError(CHILD_TEXT)

    with pytest.raises(GpuUnavailable) as exc:
        GpuPower(run=leaky, clock=clock).ensure_awake()
    assert CHILD_TEXT not in str(exc.value)


def test_veille_apres_inactivite(clock: Clock) -> None:
    pc = FakePcGpu("eveille")
    power = _power(pc, clock)
    power.job_started()
    power.job_finished()
    clock.advance(601)
    assert power.tick() is True
    assert pc.count("sleep") == 1


def test_pas_de_veille_avant_echeance(clock: Clock) -> None:
    pc = FakePcGpu("eveille")
    power = _power(pc, clock)
    power.job_started()
    power.job_finished()
    clock.advance(599)
    assert power.tick() is False
    assert pc.count("sleep") == 0


def test_jamais_de_veille_pendant_un_job(clock: Clock) -> None:
    pc = FakePcGpu("eveille")
    power = _power(pc, clock)
    power.job_started()
    clock.advance(2 * 3600)
    assert power.tick() is False
    assert pc.count("sleep") == 0


def test_jamais_de_veille_si_status_occupe(clock: Clock) -> None:
    pc = FakePcGpu("busy")
    power = _power(pc, clock)
    clock.advance(601)
    assert power.tick() is False
    assert pc.count("sleep") == 0


def test_plusieurs_jobs_simultanes_comptes(clock: Clock) -> None:
    pc = FakePcGpu("eveille")
    power = _power(pc, clock)
    power.job_started()
    power.job_started()
    power.job_finished()
    clock.advance(601)
    assert power.tick() is False  # un job tourne encore
    power.job_finished()
    clock.advance(601)
    assert power.tick() is True


def test_nouvelle_dictee_avant_echeance_relance_la_minuterie(clock: Clock) -> None:
    pc = FakePcGpu("eveille")
    power = _power(pc, clock)
    power.job_started()
    power.job_finished()
    clock.advance(300)
    power.job_started()
    clock.advance(400)
    assert power.tick() is False
    power.job_finished()  # la minuterie repart de zéro
    clock.advance(599)
    assert power.tick() is False
    assert pc.count("sleep") == 0


def test_statut_illisible_pas_de_veille_et_erreur_journalisee(
    clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(args: list[str], timeout: float) -> Result:
        raise OSError(CHILD_TEXT)

    power = GpuPower(run=broken, clock=clock)
    clock.advance(601)
    with caplog.at_level(logging.INFO, logger="gpu_power"):
        assert power.tick() is False
    assert "pc-gpu status" in caplog.text
    assert CHILD_TEXT not in caplog.text


@pytest.mark.parametrize(
    ("out", "rc"), [("???", 0), ("", 0), ("eveille", 1)], ids=["ambigu", "vide", "rc!=0"]
)
def test_statut_ambigu_ou_en_echec_pas_de_veille(clock: Clock, out: str, rc: int) -> None:
    pc = FakePcGpu(out, status_rc=rc)
    power = _power(pc, clock)
    clock.advance(601)
    assert power.tick() is False
    assert pc.count("sleep") == 0


def test_pc_deja_endormi_pas_de_sleep(clock: Clock) -> None:
    pc = FakePcGpu("endormi ou injoignable")
    power = _power(pc, clock)
    clock.advance(601)
    assert power.tick() is False
    assert pc.count("sleep") == 0


def test_echec_du_sleep_ne_leve_pas(clock: Clock) -> None:
    class Failing(FakePcGpu):
        def __call__(self, args: list[str], timeout: float) -> Result:
            res = super().__call__(args, timeout)
            return Result(2, "occupe : pas de veille") if args[1] == "sleep" else res

    pc = Failing("eveille")
    power = _power(pc, clock)
    clock.advance(601)
    assert power.tick() is False
    assert pc.count("sleep") == 1


def test_ensure_awake_repousse_la_veille(clock: Clock) -> None:
    pc = FakePcGpu("eveille")
    power = _power(pc, clock)
    clock.advance(500)
    power.ensure_awake()
    clock.advance(200)  # 700 s depuis la création, 200 s depuis la dictée
    assert power.tick() is False


@pytest.mark.parametrize("idle_s", [299, 601, 0, -1])
def test_idle_s_borne(clock: Clock, idle_s: int) -> None:
    with pytest.raises(ValueError, match="idle_s"):
        GpuPower(run=FakePcGpu("eveille"), clock=clock, idle_s=idle_s)


@pytest.mark.parametrize("idle_s", [300, 600])
def test_idle_s_bornes_acceptees(clock: Clock, idle_s: int) -> None:
    GpuPower(run=FakePcGpu("eveille"), clock=clock, idle_s=idle_s)


def test_runner_reel_sans_shell_ni_donnee(monkeypatch: pytest.MonkeyPatch) -> None:
    """Le runner par défaut : liste d'arguments, shell=False, délai transmis (subprocess simulé)."""
    seen: dict[str, object] = {}

    def fake_run(args: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        seen.update(args=args, **kw)
        return subprocess.CompletedProcess(args, 0, stdout="eveille\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    res = gpu_power.subprocess_runner(["pc-gpu", "status"], 120)
    assert res == Result(0, "eveille\n")
    assert seen["shell"] is False and seen["timeout"] == 120 and seen["args"] == ["pc-gpu", "status"]


def test_defaut_est_le_runner_subprocess(clock: Clock) -> None:
    """Sans injection, GpuPower appelle subprocess (ici interdit) : garde-fou de la fixture."""
    with pytest.raises(AssertionError, match="interdit"):
        GpuPower(clock=clock).ensure_awake()

