"""tests/catalogue_import/test_safe_fetch.py

Phase 5 gate tests:
SSRF tests prove no connection is ever opened to 169.254.169.254 / localhost / http://
(mock the socket layer and assert).
"""

from unittest.mock import MagicMock, patch
import pytest

from app.services.catalogue_import.safe_fetch import (
    PhotoFetchError,
    SSRFError,
    StorageSecurityError,
    is_ip_safe,
    rewrite_share_url,
    safe_fetch_image,
    validate_wholesaler_storage_path,
)
import ipaddress


def test_http_scheme_refused_before_any_socket():
    """http:// URLs are refused immediately without touching DNS or sockets."""
    with patch("socket.create_connection") as mock_conn, patch("socket.getaddrinfo") as mock_dns:
        with pytest.raises(SSRFError, match="only HTTPS URLs are permitted"):
            safe_fetch_image("http://example.com/photo.jpg")

        mock_conn.assert_not_called()
        mock_dns.assert_not_called()


def test_cloud_metadata_169_254_169_254_refused():
    """169.254.169.254 is link-local and blocked before opening any socket."""
    with patch("socket.create_connection") as mock_conn:
        with pytest.raises(SSRFError, match="blocked for security reasons"):
            safe_fetch_image("https://169.254.169.254/latest/meta-data/")

        mock_conn.assert_not_called()


def test_localhost_and_loopback_refused():
    """localhost / 127.0.0.1 / ::1 are blocked before opening any socket."""
    with patch("socket.create_connection") as mock_conn:
        # Literal 127.0.0.1
        with pytest.raises(SSRFError, match="blocked for security reasons"):
            safe_fetch_image("https://127.0.0.1/test.jpg")
        mock_conn.assert_not_called()

        # Literal ::1
        with pytest.raises(SSRFError, match="blocked for security reasons"):
            safe_fetch_image("https://[::1]/test.jpg")
        mock_conn.assert_not_called()

        # Hostname localhost resolving to 127.0.0.1
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
            with pytest.raises(SSRFError, match="private or restricted range"):
                safe_fetch_image("https://localhost/test.jpg")
            mock_conn.assert_not_called()


@pytest.mark.parametrize(
    "ip_str",
    [
        "10.0.0.1",
        "10.255.255.255",
        "172.16.0.1",
        "172.31.255.255",
        "192.168.0.1",
        "192.168.1.100",
        "100.64.0.1",       # CGNAT
        "100.127.255.255",  # CGNAT
        "169.254.1.1",      # Link local
        "224.0.0.1",        # Multicast
        "240.0.0.1",        # Reserved
        "0.0.0.0",
        "192.0.2.1",        # TEST-NET-1
        "198.51.100.1",     # TEST-NET-2
        "203.0.113.1",      # TEST-NET-3
    ],
)
def test_private_and_reserved_ips_blocked(ip_str):
    """Private and reserved IPv4 addresses are never connected to."""
    with patch("socket.create_connection") as mock_conn:
        with pytest.raises(SSRFError):
            safe_fetch_image(f"https://{ip_str}/test.jpg")
        mock_conn.assert_not_called()


def test_dns_resolving_to_private_ip_blocked():
    """A public-looking hostname that resolves to a private IP is blocked."""
    with patch("socket.create_connection") as mock_conn:
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("192.168.1.50", 443))]):
            with pytest.raises(SSRFError, match="private or restricted range"):
                safe_fetch_image("https://internal.mycompany.com/photo.png")
            mock_conn.assert_not_called()


def test_share_url_rewriting():
    """Google Drive and Dropbox links are correctly rewritten."""
    # Google Drive view link
    gdrive_in = "https://drive.google.com/file/d/1A2B3C4D5E6F/view?usp=sharing"
    gdrive_out = rewrite_share_url(gdrive_in)
    assert gdrive_out == "https://drive.google.com/uc?export=download&id=1A2B3C4D5E6F"

    # Google Drive open?id= link
    gdrive_open = "https://drive.google.com/open?id=XYZ789"
    assert rewrite_share_url(gdrive_open) == "https://drive.google.com/uc?export=download&id=XYZ789"

    # Dropbox ?dl=0
    dropbox_in = "https://www.dropbox.com/s/12345/jewel.jpg?dl=0"
    assert rewrite_share_url(dropbox_in) == "https://www.dropbox.com/s/12345/jewel.jpg?dl=1"


def test_redirect_to_internal_ip_blocked():
    """A redirect pointing to an internal IP or non-HTTPS URL is blocked."""
    # Mock connection that returns a 302 redirect to http://
    fake_conn = MagicMock()
    fake_resp = MagicMock()
    fake_resp.status = 302
    fake_resp.getheader.return_value = "http://169.254.169.254/secret"
    fake_conn.getresponse.return_value = fake_resp

    with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]), \
         patch("socket.create_connection", return_value=MagicMock()), \
         patch("ssl.create_default_context"), \
         patch("http.client.HTTPSConnection", return_value=fake_conn):

        with pytest.raises(SSRFError, match="only HTTPS URLs are permitted"):
            safe_fetch_image("https://example.com/redirect-to-secret")


def test_storage_path_cross_tenant_access_blocked():
    """Wholesaler A cannot access Wholesaler B's files in catalogue-imports bucket."""
    user_a = "e21f5582-1cac-4e33-8fc6-1c582edd655b"
    user_b = "fb20d512-d17b-4a3d-a793-9cce8a1eed5b"

    with pytest.raises(StorageSecurityError, match="does not belong to wholesaler"):
        validate_wholesaler_storage_path(f"{user_b}/imports/stock.xlsx", user_a)


def test_storage_path_traversal_blocked():
    """Paths containing '..' traversal segments are rejected."""
    user_a = "e21f5582-1cac-4e33-8fc6-1c582edd655b"
    user_b = "fb20d512-d17b-4a3d-a793-9cce8a1eed5b"

    with pytest.raises(StorageSecurityError, match="Path traversal"):
        validate_wholesaler_storage_path(f"{user_a}/../{user_b}/stock.xlsx", user_a)

    with pytest.raises(StorageSecurityError, match="relative, not absolute"):
        validate_wholesaler_storage_path(f"/{user_a}/stock.xlsx", user_a)


def test_storage_path_valid_wholesaler():
    """Valid path under wholesaler's own folder is accepted and normalized."""
    user_a = "e21f5582-1cac-4e33-8fc6-1c582edd655b"
    valid_path = f"{user_a}/uploads/sub/data.xlsx"
    assert validate_wholesaler_storage_path(valid_path, user_a) == valid_path

