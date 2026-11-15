"""app/services/catalogue_import/safe_fetch.py

SSRF-guarded HTTPS image fetcher.
Mandatory requirements from 01_ANTIGRAVITY_PLAN.md Section 8:
- HTTPS only (http:// refused before any network request or DNS lookup).
- DNS resolved explicitly and vetted against:
  * loopback (127.0.0.0/8, ::1)
  * private (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, fc00::/7)
  * link-local (169.254.0.0/16, fe80::/10, incl. 169.254.169.254)
  * CGNAT (100.64.0.0/10)
  * multicast (224.0.0.0/4, ff00::/8)
  * documentation/reserved (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24, 198.18.0.0/15, 240.0.0.0/4, ::)
- Connect directly to the vetted IP to eliminate DNS rebinding windows.
- TLS SNI sends original hostname; certificate verified against original hostname.
- <= 3 redirects, each re-vetted from scratch.
- 10-second timeout; stream read capped at 10 MB.
- Content-Type must be image/* AND magic bytes must match JPEG/PNG/WebP.
- Rewrite share links:
  * Google Drive: /file/d/{id}/view or /open?id={id} -> https://drive.google.com/uc?export=download&id={id}
  * Dropbox: ?dl=0 -> ?dl=1
- Generic User-Agent; never send cookies or auth headers.
"""

from __future__ import annotations

import http.client
import io
import ipaddress
import posixpath
import re
import socket
import ssl
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from PIL import Image

BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),    # CGNAT
    ipaddress.ip_network("127.0.0.0/8"),      # Loopback
    ipaddress.ip_network("169.254.0.0/16"),   # Link-local (incl. AWS/GCP metadata 169.254.169.254)
    ipaddress.ip_network("172.16.0.0/12"),    # Private
    ipaddress.ip_network("192.0.0.0/24"),     # IETF Protocol Assignments
    ipaddress.ip_network("192.0.2.0/24"),     # TEST-NET-1
    ipaddress.ip_network("192.168.0.0/16"),   # Private
    ipaddress.ip_network("198.18.0.0/15"),    # Benchmark
    ipaddress.ip_network("198.51.100.0/24"),  # TEST-NET-2
    ipaddress.ip_network("203.0.113.0/24"),   # TEST-NET-3
    ipaddress.ip_network("224.0.0.0/4"),      # Multicast
    ipaddress.ip_network("240.0.0.0/4"),      # Reserved
    ipaddress.ip_network("255.255.255.255/32"),
    # IPv6 ranges
    ipaddress.ip_network("::1/128"),          # Loopback
    ipaddress.ip_network("::/128"),           # Unspecified
    ipaddress.ip_network("64:ff9b::/96"),     # IPv4/IPv6 translation
    ipaddress.ip_network("100::/64"),         # Discard prefix
    ipaddress.ip_network("2001:db8::/32"),    # Documentation
    ipaddress.ip_network("fc00::/7"),         # Unique local
    ipaddress.ip_network("fe80::/10"),        # Link-local
    ipaddress.ip_network("ff00::/8"),         # Multicast
]

MAX_REDIRECTS = 3
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB
SOCKET_TIMEOUT = 10.0


class SSRFError(Exception):
    """Raised when a URL targets a forbidden, internal, or non-HTTPS destination."""
    pass


class PhotoFetchError(Exception):
    """Raised when an image fetch fails (timeout, 404, invalid type, corrupt data)."""
    pass


def is_ip_safe(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Check if an IP is a globally routable public address."""
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return False

    for net in BLOCKED_NETWORKS:
        if ip in net:
            return False

    return True


def rewrite_share_url(url: str) -> str:
    """Rewrite Google Drive and Dropbox share links to direct download URLs."""
    try:
        parsed = urlsplit(url)
    except Exception:
        return url

    netloc = parsed.netloc.lower()

    # Google Drive share links
    if "drive.google.com" in netloc:
        # Format 1: /file/d/{id}/view
        m = re.search(r"/file/d/([a-zA-Z0-9_-]+)", parsed.path)
        if m:
            file_id = m.group(1)
            return f"https://drive.google.com/uc?export=download&id={file_id}"
        # Format 2: /open?id={id}
        qs = parse_qs(parsed.query)
        if "id" in qs and qs["id"]:
            file_id = qs["id"][0]
            return f"https://drive.google.com/uc?export=download&id={file_id}"

    # Dropbox share links
    if "dropbox.com" in netloc:
        qs = parse_qs(parsed.query)
        if "dl" in qs:
            qs["dl"] = ["1"]
            new_query = urlencode(qs, doseq=True)
            return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, new_query, parsed.fragment))
        elif not parsed.query:
            return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "dl=1", parsed.fragment))

    return url


def validate_magic_bytes(data: bytes) -> str:
    """Validate that data begins with valid image magic bytes. Returns 'jpeg', 'png', or 'webp'."""
    if len(data) < 12:
        raise PhotoFetchError("Downloaded data too short to be an image")

    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"

    raise PhotoFetchError("Data does not match JPEG, PNG, or WebP magic bytes")


def safe_fetch_image(url: str, timeout: float = SOCKET_TIMEOUT) -> tuple[bytes, str]:
    """Fetch an image from an external URL with strict SSRF and size safeguards.

    Returns:
        tuple[bytes, str]: (raw_image_bytes, mime_type)
    """
    current_url = rewrite_share_url(url.strip())

    redirects_left = MAX_REDIRECTS

    while True:
        # Rule: HTTPS only. http:// refused BEFORE any network or DNS activity.
        parsed = urlsplit(current_url)
        if parsed.scheme.lower() != "https":
            raise SSRFError(f"Refusing scheme '{parsed.scheme}': only HTTPS URLs are permitted")

        hostname = parsed.hostname
        if not hostname:
            raise SSRFError("URL has no valid hostname")

        port = parsed.port or 443

        # Check if hostname itself is a literal IP
        try:
            literal_ip = ipaddress.ip_address(hostname)
            if not is_ip_safe(literal_ip):
                raise SSRFError(f"Access to IP address {literal_ip} is blocked for security reasons")
            vetted_ips = [literal_ip]
        except ValueError:
            # Not a literal IP, resolve DNS
            try:
                addrinfo = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
            except socket.gaierror as exc:
                raise PhotoFetchError(f"Could not resolve hostname '{hostname}': {exc}")

            vetted_ips = []
            for item in addrinfo:
                ip_str = item[4][0]
                try:
                    ip = ipaddress.ip_address(ip_str)
                    if not is_ip_safe(ip):
                        raise SSRFError(f"Resolved IP {ip} for host '{hostname}' is in a private or restricted range")
                    vetted_ips.append(ip)
                except ValueError:
                    continue

            if not vetted_ips:
                raise SSRFError(f"No valid public IP addresses resolved for '{hostname}'")

        # Connect directly to the vetted IP (prevents DNS rebinding)
        target_ip = str(vetted_ips[0])

        sock = None
        conn = None
        try:
            sock = socket.create_connection((target_ip, port), timeout=timeout)
            ssl_ctx = ssl.create_default_context()
            # server_hostname is original hostname for SNI and TLS cert validation
            tls_sock = ssl_ctx.wrap_socket(sock, server_hostname=hostname)

            conn = http.client.HTTPSConnection(hostname, port=port, timeout=timeout)
            conn.sock = tls_sock

            path = parsed.path or "/"
            if parsed.query:
                path = f"{path}?{parsed.query}"

            conn.request(
                "GET",
                path,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; JewelIndiaBot/1.0)",
                    "Accept": "image/jpeg,image/png,image/webp,image/*",
                },
            )

            resp = conn.getresponse()

            # Handle redirects (<= 3 redirects)
            if resp.status in (301, 302, 303, 307, 308):
                location = resp.getheader("Location")
                if not location:
                    raise PhotoFetchError(f"Redirect {resp.status} with no Location header")

                redirects_left -= 1
                if redirects_left < 0:
                    raise SSRFError(f"Exceeded maximum of {MAX_REDIRECTS} redirects")

                current_url = urljoin(current_url, location)
                current_url = rewrite_share_url(current_url)
                continue

            if resp.status != 200:
                raise PhotoFetchError(f"HTTP fetch returned status {resp.status} {resp.reason}")

            # Validate Content-Type
            content_type = resp.getheader("Content-Type", "")
            mime = content_type.split(";")[0].strip().lower()
            if not mime.startswith("image/"):
                raise PhotoFetchError(f"Invalid Content-Type '{content_type}': expected image/*")

            # Validate Content-Length if present
            cl_header = resp.getheader("Content-Length")
            if cl_header:
                try:
                    if int(cl_header) > MAX_IMAGE_BYTES:
                        raise PhotoFetchError(f"Image size {cl_header} bytes exceeds 10 MB limit")
                except ValueError:
                    pass

            # Stream response up to 10 MB limit
            chunks = []
            total = 0
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_IMAGE_BYTES:
                    raise PhotoFetchError(f"Image exceeded 10 MB limit while downloading ({total} bytes)")
                chunks.append(chunk)

            data = b"".join(chunks)

            # Validate magic bytes
            img_format = validate_magic_bytes(data)

            # Validate decode with PIL
            try:
                img = Image.open(io.BytesIO(data))
                img.verify()
            except Exception as exc:
                raise PhotoFetchError(f"Image data failed verification: {exc}")

            actual_mime = f"image/{img_format}"
            return data, actual_mime

        except (SSRFError, PhotoFetchError):
            raise
        except Exception as exc:
            raise PhotoFetchError(f"Failed to fetch image from {url}: {exc}")
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            elif sock:
                try:
                    sock.close()
                except Exception:
                    pass


class StorageSecurityError(ValueError):
    """Raised when an uploaded storage path is invalid, traverses directories, or violates tenant isolation."""
    pass


def validate_wholesaler_storage_path(path: str, user_id: str) -> str:
    """
    Validates and normalizes storage path in catalogue-imports bucket.
    Must start with '{user_id}/' and cannot contain '..' traversal segments.
    Raises StorageSecurityError if invalid.
    """
    if not path or not isinstance(path, str):
        raise StorageSecurityError("Invalid or empty storage path.")

    clean = path.strip()
    if clean.startswith("/"):
        raise StorageSecurityError("Storage path must be relative, not absolute.")

    raw_parts = clean.split("/")
    if ".." in raw_parts:
        raise StorageSecurityError("Path traversal ('..') detected in storage path.")

    norm_path = posixpath.normpath(clean)

    expected_prefix = f"{user_id}/"
    if not norm_path.startswith(expected_prefix):
        raise StorageSecurityError(
            f"Access denied: storage path '{norm_path}' does not belong to wholesaler '{user_id}'."
        )

    return norm_path

