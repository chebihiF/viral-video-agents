"""Tests de l'éval storyboard : jamais d'appel Ollama (réseau bloqué)."""

import json
from collections.abc import Callable
from pathlib import Path

import pytest
import storyboard_eval as ev


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("appel réseau interdit dans les tests")

    monkeypatch.setattr(ev.urllib.request, "urlopen", boom)


def test_reproductible(capsys: pytest.CaptureFixture[str]) -> None:
    """Scénario : deux exécutions, même rapport et même taux."""
    assert ev.main(["--dry-run"]) == 0
    first = capsys.readouterr().out
    assert ev.main(["--dry-run"]) == 0
    assert capsys.readouterr().out == first
    assert "conformes 11/12" in first
    assert "mix-12" in first


def test_seuil_code_non_nul_et_liste(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Scénario : taux < 90 % -> code non nul et cas en échec listés."""
    rec = tmp_path / "rec.jsonl"
    cases = ev.read_jsonl(ev.CASES)
    fp = ev.read_jsonl(ev.RECORDED)[0]["schema_sha256"]
    lines = [{"id": c["id"], "schema_sha256": fp, "response": "pas du json"} for c in cases[:3]]
    lines += [r for r in ev.read_jsonl(ev.RECORDED) if r["id"] in {c["id"] for c in cases[3:]}]
    rec.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    assert ev.main(["--dry-run", "--recorded", str(rec)]) == 1
    out = capsys.readouterr().out
    assert "ECHEC" in out
    for c in cases[:3]:
        assert c["id"] in out


def test_dry_run_sans_ollama() -> None:
    """Scénario : --dry-run passe sans appeler Ollama (urlopen piégé par la fixture)."""
    assert ev.main(["--dry-run"]) == 0


def test_mode_reel_sans_contrat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(ev, "CONTRACT", tmp_path / "absent.schema.json")
    assert ev.main([]) == 2
    err = capsys.readouterr().err
    assert "schéma absent" in err
    assert ev.main(["--dry-run"]) == 0  # le schéma de test prend le relais


def test_dry_run_n_utilise_jamais_le_vrai_contrat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    other = tmp_path / "storyboard.schema.json"
    other.write_text('{"type": "object", "required": ["zzz"]}', encoding="utf-8")
    monkeypatch.setattr(ev, "CONTRACT", other)
    assert ev.main(["--dry-run"]) == 0
    # --schema explicite : empreinte différente -> code 2, pas de vert trompeur
    assert ev.main(["--dry-run", "--schema", str(other)]) == 2


def test_empreinte_differente_code_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rows = ev.read_jsonl(ev.RECORDED)
    rows[0]["schema_sha256"] = "000000000000"
    rec = tmp_path / "rec.jsonl"
    rec.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    assert ev.main(["--dry-run", "--recorded", str(rec)]) == 2
    assert "ré-enregistrer avec --record" in capsys.readouterr().err
    for r in rows:
        del r["schema_sha256"]
    rec.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    assert ev.main(["--dry-run", "--recorded", str(rec)]) == 2


@pytest.mark.parametrize("value", ["-0.1", "1.5", "nan"])
def test_seuil_hors_bornes(value: str) -> None:
    assert ev.main(["--dry-run", "--threshold", value]) == 2


def test_reponses_malformees_code_propre(tmp_path: Path) -> None:
    fp = ev.read_jsonl(ev.RECORDED)[0]["schema_sha256"]
    cases = ev.read_jsonl(ev.CASES)
    rec = tmp_path / "rec.jsonl"
    rows = [{"id": c["id"], "schema_sha256": fp, "response": 42} for c in cases]
    rec.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    assert ev.main(["--dry-run", "--recorded", str(rec)]) == 1  # tous en échec, pas de crash
    rec.write_text("[1, 2]\n", encoding="utf-8")
    assert ev.main(["--dry-run", "--recorded", str(rec)]) == 2


def test_reponse_ollama_non_objet(monkeypatch: pytest.MonkeyPatch) -> None:
    class Resp:
        def __enter__(self) -> "Resp":
            return self

        def __exit__(self, *_a: object) -> None:
            return None

        def read(self) -> bytes:
            return b"[1, 2]"

    monkeypatch.setattr(ev.urllib.request, "urlopen", lambda *_a, **_k: Resp())
    monkeypatch.setattr(ev, "CONTRACT", ev.TEST_SCHEMA)
    assert ev.main([]) == 1  # TypeError attrapé : cas en échec, pas de traceback


def test_mode_reel_appelle_ollama_avec_temperature_0_et_seed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sent: list[dict[str, object]] = []
    good = ev.read_jsonl(ev.RECORDED)[0]["response"]

    def fake_ask(
        dictee: str, system: str, schema: dict[str, object], url: str, model: str, seed: int
    ) -> str:
        sent.append({"seed": seed, "model": model, "url": url, "system": bool(system)})
        return str(good)

    monkeypatch.setattr(ev, "ask_ollama", fake_ask)
    monkeypatch.setattr(ev, "CONTRACT", ev.TEST_SCHEMA)
    assert ev.main(["--record", str(tmp_path / "r.jsonl")]) == 0
    assert len(sent) == 12
    assert sent[0] == {"seed": 42, "model": "qwen3:8b", "url": ev.OLLAMA_URL, "system": True}


def test_cas_valides() -> None:
    cases = ev.read_jsonl(ev.CASES)
    assert len(cases) >= 10
    assert len({c["id"] for c in cases}) == len(cases)
    assert {"darija_latin", "darija_arabe", "francais", "melange"} <= {c["lang"] for c in cases}


def test_prompt_sans_entete() -> None:
    body = ev.load_system_prompt()
    assert body.startswith("Tu es") and "version:" not in body


SCHEMA = {
    "type": "object",
    "required": ["a"],
    "properties": {
        "a": {
            "type": "array",
            "minItems": 1,
            "maxItems": 2,
            "items": {"type": "string", "minLength": 2},
        },
        "k": {"enum": ["x", "y"]},
    },
}


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ({"a": ["ab"]}, True),
        ({}, False),
        ({"a": []}, False),
        ({"a": ["ab", "cd", "ef"]}, False),
        ({"a": ["a"]}, False),
        ({"a": [1]}, False),
        ({"a": ["ab"], "k": "z"}, False),
        ({"a": ["ab"], "k": "x"}, True),
        ([], False),
    ],
)
def test_validate(value: object, ok: bool) -> None:
    assert (ev.validate(value, SCHEMA) == []) is ok


def test_mot_cle_non_gere_echoue_franchement() -> None:
    with pytest.raises(ev.ConfigError):
        ev.check_schema({"type": "object", "oneOf": []})


def test_mot_cle_non_gere_dans_propriete_optionnelle_absente(tmp_path: Path) -> None:
    schema = {
        "type": "object",
        "properties": {"opt": {"type": "object", "properties": {"x": {"pattern": "a"}}}},
        "additionalProperties": False,
    }
    assert ev.validate({}, schema) == []  # la valeur ne visite jamais « opt »
    with pytest.raises(ev.ConfigError, match="pattern"):
        ev.check_schema(schema)
    path = tmp_path / "s.json"
    path.write_text(json.dumps(schema), encoding="utf-8")
    assert ev.main(["--dry-run", "--schema", str(path)]) == 2


def test_additional_properties_booleen_seulement() -> None:
    with pytest.raises(ev.ConfigError):
        ev.check_schema({"type": "object", "additionalProperties": {"type": "string"}})


def test_reproductible_mode_reel(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Scénario « éval reproductible » en mode réel : Ollama simulé, deux lancements identiques."""
    cases = ev.read_jsonl(ev.CASES)
    recorded = {r["id"]: r["response"] for r in ev.read_jsonl(ev.RECORDED)}
    by_dictee = {c["dictee"]: recorded[c["id"]] for c in cases}

    def fake_ask(
        dictee: str, system: str, schema: dict[str, object], url: str, model: str, seed: int
    ) -> str:
        assert seed == ev.SEED
        return str(by_dictee[dictee])

    monkeypatch.setattr(ev, "ask_ollama", fake_ask)
    monkeypatch.setattr(ev, "CONTRACT", ev.TEST_SCHEMA)
    code1 = ev.main([])
    out1 = capsys.readouterr().out
    code2 = ev.main([])
    out2 = capsys.readouterr().out
    assert code1 == code2
    assert out1 == out2
    assert "conformes 11/12" in out1


def test_seuil_bornes_exactes() -> None:
    """Scénario « seuil » : 90 % exact passe, en dessous échoue (pas d'arrondi)."""
    schema = ev.load_schema(dry_run=False, explicit=ev.TEST_SCHEMA)[0]
    ok_raw = str(ev.read_jsonl(ev.RECORDED)[0]["response"])  # conforme au schéma de test

    def responder_factory(n_ok: int) -> Callable[[dict[str, object]], str]:
        def responder(case: dict[str, object]) -> str:
            return ok_raw if int(str(case["id"])[1:]) < n_ok else "{}"

        return responder

    cases_10 = [{"id": f"c{i}"} for i in range(10)]
    rep_ok = ev.run(cases_10, responder_factory(9), schema, ev.THRESHOLD)
    assert rep_ok["taux"] == 0.9 and rep_ok["succes"] is True
    cases_9 = [{"id": f"c{i}"} for i in range(9)]
    rep_ko = ev.run(cases_9, responder_factory(8), schema, ev.THRESHOLD)
    assert rep_ko["taux"] < 0.9 and rep_ko["succes"] is False
    assert [f["id"] for f in rep_ko["echecs"]] == ["c8"]


def test_timeout_compte_comme_echec_sans_crash() -> None:
    """Cas limite « timeout » : une erreur réseau/timeout est un cas en échec listé, pas un crash."""
    schema = ev.load_schema(dry_run=False, explicit=ev.TEST_SCHEMA)[0]

    def responder(case: dict[str, object]) -> str:
        if case["id"] == "c1":
            raise TimeoutError("timed out")
        return str(ev.read_jsonl(ev.RECORDED)[0]["response"])

    cases = [{"id": "c0"}, {"id": "c1"}]
    rep = ev.run(cases, responder, schema, ev.THRESHOLD)
    assert rep["succes"] is False
    assert [f["id"] for f in rep["echecs"]] == ["c1"]
    assert "appel échoué" in rep["echecs"][0]["erreurs"][0]
