"""Mode C — gallery: prebuilt images / ASCII art / texts (review §3 / M-RES / C-5).

A curated, **server-controlled** asset set (no caller-supplied bytes → smallest
attack surface). Assets live in ``server/assets/`` with ``manifest.json`` listing
``{id, name, category(image|ascii|text), file|text, render params}``.

This module:
- loads + validates the manifest (ids unique, categories known, no path escape);
- ``list_presets(category?)`` and ``show_preset(id, slot?)`` as the model's image
  path (cheap ids, server holds bytes — review C-5);
- exposes the catalog as MCP **resources** (``gallery://presets/...``) for the
  app layer to register (review M-RES);
- a guarded ``fetch_image_url`` (SSRF guard + F-3 size caps) for model-friendly
  image display; raw ``display_image(image_base64)`` stays machine/passthrough.

Pure + stdlib (asset root + render callbacks injected) → hardware-free tests.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import socket
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlparse

from .. import safety

logger = logging.getLogger("ipixel_mcp.gallery")

CATEGORIES = ("image", "ascii", "text")
MAX_FETCH_BYTES = safety.MAX_IMAGE_BYTES


@dataclass(frozen=True)
class Preset:
    id: str
    name: str
    category: str
    file: Optional[str] = None  # relative path within the asset root
    text: Optional[str] = None
    render: dict = None  # type: ignore[assignment]

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "preview": self.text if self.text is not None else (self.file or ""),
        }


class Gallery:
    """Loads and serves the curated preset catalog from an asset root."""

    def __init__(self, asset_root: str) -> None:
        self._root = os.path.abspath(asset_root)
        self._presets: dict[str, Preset] = {}
        self._load()

    def _safe_path(self, rel: str) -> str:
        """Resolve a manifest file path, refusing any escape from the root (F-2)."""
        full = os.path.abspath(os.path.join(self._root, rel))
        if os.path.commonpath([self._root, full]) != self._root:
            raise safety.ValidationError("asset path escapes the asset root")
        return full

    def _load(self) -> None:
        manifest_path = os.path.join(self._root, "manifest.json")
        with open(manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for raw in data.get("presets", []):
            pid = raw.get("id")
            category = raw.get("category")
            if not pid or pid in self._presets:
                raise ValueError(f"invalid or duplicate preset id: {pid!r}")
            if category not in CATEGORIES:
                raise ValueError(f"unknown category for {pid!r}: {category!r}")
            if category == "text" and not raw.get("text"):
                raise ValueError(f"text preset {pid!r} missing 'text'")
            if category in ("image", "ascii") and not raw.get("file"):
                raise ValueError(f"{category} preset {pid!r} missing 'file'")
            if raw.get("file"):
                self._safe_path(raw["file"])  # validate now, read lazily
            self._presets[pid] = Preset(
                id=pid,
                name=raw.get("name", pid),
                category=category,
                file=raw.get("file"),
                text=raw.get("text"),
                render=raw.get("render") or {},
            )

    # -- reads ----------------------------------------------------------------

    def list_presets(self, category: Optional[str] = None) -> dict[str, Any]:
        if category is not None and category not in CATEGORIES:
            raise safety.ValidationError(
                f"category must be one of: {', '.join(CATEGORIES)}"
            )
        items = [
            p.public()
            for p in self._presets.values()
            if category is None or p.category == category
        ]
        return {"presets": sorted(items, key=lambda x: x["id"])}

    def get(self, preset_id: str) -> Preset:
        p = self._presets.get(preset_id)
        if p is None:
            raise safety.ValidationError(f"unknown preset id: {preset_id}")
        return p

    def load_text(self, preset: Preset) -> str:
        """Read an ascii/text preset's content (text inline, ascii from file)."""
        if preset.text is not None:
            return preset.text
        if preset.file is None:
            raise safety.ValidationError("preset has no renderable content")
        with open(self._safe_path(preset.file), "r", encoding="utf-8") as f:
            return f.read()

    def load_image_bytes(self, preset: Preset) -> bytes:
        if preset.category != "image" or not preset.file:
            raise safety.ValidationError("preset is not an image")
        with open(self._safe_path(preset.file), "rb") as f:
            return f.read()

    # -- MCP resources (review M-RES) -----------------------------------------

    def resources(self) -> list[dict[str, Any]]:
        """Catalog as MCP resource descriptors (uri/name/description/mimeType)."""
        out = []
        for p in self._presets.values():
            out.append(
                {
                    "uri": f"gallery://presets/{p.id}",
                    "name": p.name,
                    "description": f"{p.category} preset '{p.name}'",
                    # Image presets expose JSON *metadata* via read_resource (the
                    # actual pixels are rendered by show_preset), so the mime must
                    # reflect what's returned, not image/png (review NIT-2).
                    "mimeType": "application/json" if p.category == "image" else "text/plain",
                }
            )
        return out

    def read_resource(self, uri: str) -> str:
        """Return resource text for a ``gallery://presets/<id>`` URI."""
        prefix = "gallery://presets/"
        if not uri.startswith(prefix):
            raise safety.ValidationError("unknown resource uri")
        p = self.get(uri[len(prefix):])
        if p.category == "image":
            return json.dumps(p.public())
        return self.load_text(p)


# ---- SSRF-guarded image_url fetch (review C-5) ------------------------------

# (host) -> list[str] of resolved IPs. Injected so tests don't hit DNS.
Resolver = Callable[[str], "list[str]"]


def _default_resolver(host: str) -> "list[str]":
    infos = socket.getaddrinfo(host, None)
    return [str(i[4][0]) for i in infos]


def _is_public_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
        # IPv4-mapped IPv6 (e.g. ::ffff:127.0.0.1) reports is_private/is_loopback
        # as False but the OS connects to the mapped IPv4 — unmap before checking
        # so the SSRF guard isn't bypassed (PR review, security-high).
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def guard_image_url(url: str, *, resolver: Resolver = _default_resolver) -> str:
    """Validate an image URL against SSRF (review C-5). Returns the URL or raises.

    Enforces https only, and that every resolved address is a public unicast IP
    (blocks localhost, RFC1918, link-local 169.254/cloud-metadata, multicast).
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise safety.ValidationError("image_url must be https")
    host = parsed.hostname
    if not host:
        raise safety.ValidationError("image_url has no host")
    try:
        ips = resolver(host)
    except Exception as exc:  # noqa: BLE001
        raise safety.ValidationError("image_url host could not be resolved") from exc
    if not ips:
        raise safety.ValidationError("image_url host could not be resolved")
    for ip in ips:
        if not _is_public_ip(ip):
            raise safety.ValidationError("image_url resolves to a non-public address")
    return url


# (url) -> awaitable bytes. Injected; the default uses urllib lazily.
UrlFetcher = Callable[[str], Awaitable[bytes]]


async def fetch_image_url(
    url: str,
    fmt: str,
    *,
    resolver: Resolver = _default_resolver,
    fetcher: Optional[UrlFetcher] = None,
    frame_sizer: safety.FrameSizer = safety._pillow_frame_sizer,
) -> safety.DecodedImage:
    """Fetch + harden a model-supplied image URL (SSRF guard + F-3 caps)."""
    guard_image_url(url, resolver=resolver)
    if fetcher is None:
        fetcher = _default_url_fetcher
    data = await fetcher(url)
    if len(data) > MAX_FETCH_BYTES:
        raise safety.ValidationError("fetched image exceeds the size limit")
    return safety.decode_and_prepare_image(data, fmt, frame_sizer=frame_sizer)


async def _default_url_fetcher(url: str) -> bytes:
    """Fetch an https image with the connection PINNED to a validated IP.

    Closes the DNS-rebinding TOCTOU (PR review, security-medium): we resolve the
    host once, verify *that* address is a public unicast IP, then open the TLS
    connection to that exact IP (SNI/cert still validated against the hostname).
    A second, attacker-controlled resolution can't swap in a private/metadata IP,
    and redirects are refused.
    """
    import asyncio

    return await asyncio.to_thread(_pinned_https_get, url)


def _pinned_https_get(url: str) -> bytes:
    import http.client
    import socket
    import ssl

    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise safety.ValidationError("image_url must be https")
    host = parsed.hostname
    port = parsed.port or 443
    if not host:
        raise safety.ValidationError("image_url has no host")

    # Resolve ONCE; validate; connect to that exact address.
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise safety.ValidationError("image_url host could not be resolved") from exc
    addr = None
    for info in infos:
        ip = info[4][0]
        if _is_public_ip(ip):
            addr = (info[0], ip)
            break
    if addr is None:
        raise safety.ValidationError("image_url resolves to a non-public address")
    family, ip = addr

    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    ctx = ssl.create_default_context()
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(5)
    try:
        sock.connect((ip, port))
        tls = ctx.wrap_socket(sock, server_hostname=host)  # SNI + cert checked vs host
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.sock = tls
        conn.request("GET", path, headers={"Host": host, "User-Agent": "ipixel-mcp"})
        resp = conn.getresponse()
        if resp.status in (301, 302, 303, 307, 308):
            raise safety.ValidationError("redirects are not allowed for image_url")
        if resp.status != 200:
            raise safety.ValidationError(f"image fetch failed (HTTP {resp.status})")
        return resp.read(MAX_FETCH_BYTES + 1)
    finally:
        try:
            sock.close()
        except OSError:
            pass
