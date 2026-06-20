import asyncio
import os

import pytest

from ipixel_mcp.modes import gallery as gallery_mod
from ipixel_mcp.modes.gallery import Gallery, guard_image_url, fetch_image_url
from ipixel_mcp.safety import ValidationError

ASSET_ROOT = os.path.join(os.path.dirname(__file__), "..", "assets")


def test_loads_shipped_manifest():
    g = Gallery(ASSET_ROOT)
    presets = g.list_presets()["presets"]
    ids = {p["id"] for p in presets}
    assert {"ascii-heart", "ascii-smile", "text-brb", "text-on-air"} <= ids


def test_filter_by_category():
    g = Gallery(ASSET_ROOT)
    texts = g.list_presets("text")["presets"]
    assert all(p["category"] == "text" for p in texts)
    with pytest.raises(ValidationError):
        g.list_presets("bogus")


def test_get_and_load_text():
    g = Gallery(ASSET_ROOT)
    p = g.get("text-brb")
    assert g.load_text(p) == "BRB"
    ascii_p = g.get("ascii-smile")
    assert ":-)" in g.load_text(ascii_p)


def test_unknown_preset():
    g = Gallery(ASSET_ROOT)
    with pytest.raises(ValidationError):
        g.get("nope")


def test_resources_exposed():
    g = Gallery(ASSET_ROOT)
    res = g.resources()
    uris = {r["uri"] for r in res}
    assert "gallery://presets/text-brb" in uris
    assert g.read_resource("gallery://presets/text-brb") == "BRB"
    with pytest.raises(ValidationError):
        g.read_resource("http://evil")


def test_manifest_validation_rejects_bad(tmp_path):
    (tmp_path / "manifest.json").write_text(
        '{"presets":[{"id":"x","category":"text"}]}'  # missing text
    )
    with pytest.raises(ValueError):
        Gallery(str(tmp_path))


def test_manifest_rejects_duplicate_id(tmp_path):
    (tmp_path / "manifest.json").write_text(
        '{"presets":[{"id":"x","category":"text","text":"a"},'
        '{"id":"x","category":"text","text":"b"}]}'
    )
    with pytest.raises(ValueError):
        Gallery(str(tmp_path))


# --- SSRF guard --------------------------------------------------------------

def test_guard_blocks_non_https():
    with pytest.raises(ValidationError):
        guard_image_url("http://example.com/a.png", resolver=lambda h: ["1.2.3.4"])


def test_guard_blocks_private_ip():
    for ip in ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1"]:
        with pytest.raises(ValidationError):
            guard_image_url("https://evil.example/a.png", resolver=lambda h, ip=ip: [ip])


def test_guard_allows_public():
    url = guard_image_url("https://example.com/a.png", resolver=lambda h: ["8.8.8.8"])
    assert url.startswith("https://")


def test_guard_blocks_mixed_resolution():
    # one public + one private => blocked (DNS rebinding defense)
    with pytest.raises(ValidationError):
        guard_image_url(
            "https://example.com/a.png", resolver=lambda h: ["8.8.8.8", "127.0.0.1"]
        )


def test_fetch_image_url_happy(monkeypatch):
    async def scenario():
        async def fetcher(url):
            return b"\x89PNG\r\n" + b"\x00" * 32

        decoded = await fetch_image_url(
            "https://example.com/a.png", "png",
            resolver=lambda h: ["8.8.8.8"],
            fetcher=fetcher,
            frame_sizer=lambda data, fmt: [(16, 16)],
        )
        assert decoded.width == 16

    asyncio.run(scenario())


def test_fetch_image_url_size_cap():
    async def scenario():
        async def fetcher(url):
            return b"x" * (gallery_mod.MAX_FETCH_BYTES + 1)

        with pytest.raises(ValidationError):
            await fetch_image_url(
                "https://example.com/a.png", "png",
                resolver=lambda h: ["8.8.8.8"],
                fetcher=fetcher,
                frame_sizer=lambda data, fmt: [(16, 16)],
            )

    asyncio.run(scenario())
