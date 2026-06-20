from ipixel_mcp.logging_utils import redact_bytes, redact_hex


def test_redact_bytes_hides_content_F13():
    secret = b"super-secret-frame-bytes-\x00\x01\x02"
    out = redact_bytes(secret)
    assert "len=" in out and "sha256=" in out
    # the raw bytes must NOT appear in the redacted summary
    assert "super-secret" not in out
    assert b"super-secret".hex() not in out


def test_redact_bytes_stable_fingerprint():
    a = redact_bytes(b"abc")
    b = redact_bytes(b"abc")
    assert a == b
    assert redact_bytes(b"abc") != redact_bytes(b"abd")


def test_redact_bytes_non_bytes():
    assert "non-bytes" in redact_bytes("a string")


def test_redact_hex_hides_content():
    out = redact_hex("deadbeef" * 8)
    assert "chars=" in out and "sha256=" in out
    assert "deadbeef" not in out


def test_redact_hex_non_str():
    assert "non-str" in redact_hex(123)
