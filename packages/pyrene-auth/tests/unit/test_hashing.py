"""Unit tests for argon2 password hash + verify."""

from __future__ import annotations

from pyrene_auth.hashing import hash_password, verify_password


def test_hash_and_verify_round_trip() -> None:
    hashed = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", hashed) is True


def test_verify_rejects_wrong_password() -> None:
    hashed = hash_password("correct horse battery staple")
    assert verify_password("wrong password", hashed) is False


def test_verify_rejects_malformed_hash() -> None:
    assert verify_password("anything", "not-a-real-hash") is False


def test_hash_is_not_plaintext() -> None:
    hashed = hash_password("plain")
    assert "plain" not in hashed
    assert hashed.startswith("$argon2")


def test_hashes_of_same_password_differ_due_to_salt() -> None:
    h1 = hash_password("same-password")
    h2 = hash_password("same-password")
    assert h1 != h2
    # Both must still verify.
    assert verify_password("same-password", h1)
    assert verify_password("same-password", h2)
