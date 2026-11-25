"""Tests du relais WoL : envoi injectable, aucun paquet réel."""

import pytest
import wol_relay as r

MAC = "aa:bb:cc:dd:ee:ff"
TOKEN = "t" * 32


def test_magic_packet_format():
    p = r.magic_packet(MAC)
    assert len(p) == 102
    assert p[:6] == b"\xff" * 6
    assert p[6:] == bytes.fromhex("aabbccddeeff") * 16


@pytest.mark.parametrize("bad", ["", "aa:bb", "zz:bb:cc:dd:ee:ff", "aabbccddeeff00"])
def test_magic_packet_rejects_bad_mac(bad):
    with pytest.raises(ValueError):
        r.magic_packet(bad)


def test_accepts_dash_separator():
    assert r.magic_packet("aa-bb-cc-dd-ee-ff") == r.magic_packet(MAC)


def test_wake_sends_once():
    sent = []
    status, _ = r.handle("POST", "/wake", f"Bearer {TOKEN}", MAC, TOKEN, sent.append)
    assert status == 200
    assert sent == [r.magic_packet(MAC)]


@pytest.mark.parametrize("auth", [None, "", "Bearer wrong", TOKEN, "Basic " + TOKEN])
def test_refuses_without_valid_token(auth):
    sent = []
    status, _ = r.handle("POST", "/wake", auth, MAC, TOKEN, sent.append)
    assert status == 401
    assert sent == []


def test_wrong_method_or_path():
    sent = []
    assert r.handle("GET", "/wake", f"Bearer {TOKEN}", MAC, TOKEN, sent.append)[0] == 405
    assert r.handle("POST", "/x", f"Bearer {TOKEN}", MAC, TOKEN, sent.append)[0] == 404
    assert sent == []


def test_send_failure_is_502():
    def boom(_):
        raise OSError("down")

    assert r.handle("POST", "/wake", f"Bearer {TOKEN}", MAC, TOKEN, boom)[0] == 502


def test_config_requires_env():
    with pytest.raises(SystemExit):
        r.load_config({})
    with pytest.raises(SystemExit):
        r.load_config({"WOL_TARGET_MAC": MAC, "WOL_RELAY_TOKEN": "short", "WOL_BIND": "10.8.0.2"})
    cfg = r.load_config({"WOL_TARGET_MAC": MAC, "WOL_RELAY_TOKEN": TOKEN, "WOL_BIND": "10.8.0.2"})
    assert cfg["bind"] == "10.8.0.2" and cfg["port"] == 8089


def test_refuses_public_bind():
    env = {"WOL_TARGET_MAC": MAC, "WOL_RELAY_TOKEN": TOKEN, "WOL_BIND": "0.0.0.0"}  # noqa: S104
    with pytest.raises(SystemExit):
        r.load_config(env)
