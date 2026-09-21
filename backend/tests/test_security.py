from app.core.security import (
    create_access_token,
    decode_token,
    hash_password,
    verify_password,
)


def test_password_hash_and_verify_roundtrip():
    hashed = hash_password("Sup3rSecret!")
    assert hashed != "Sup3rSecret!"
    assert verify_password("Sup3rSecret!", hashed)
    assert not verify_password("wrong-password", hashed)


def test_access_token_roundtrip():
    token = create_access_token("00000000-0000-0000-0000-000000000000")
    payload = decode_token(token)
    assert payload["sub"] == "00000000-0000-0000-0000-000000000000"
    assert payload["type"] == "access"
