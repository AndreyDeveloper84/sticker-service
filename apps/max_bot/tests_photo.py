import socket
from unittest import mock

from django.test import SimpleTestCase

from apps.max_bot import photo
from apps.max_bot.photo import (
    PhotoDownloadError,
    PhotoTooLargeError,
    download_photo,
    extract_photo_url,
    safe_hostname,
)

PUBLIC_ADDRINFO = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


def _mock_download(chunks, status=200):
    """Patch photo.httpx.Client with a fake streaming client."""
    stream_resp = mock.MagicMock()
    stream_resp.__enter__.return_value = stream_resp
    stream_resp.status_code = status
    stream_resp.iter_bytes.return_value = iter(chunks)

    client = mock.MagicMock()
    client.__enter__.return_value = client
    client.stream.return_value = stream_resp

    return mock.patch.object(photo.httpx, "Client", return_value=client)


class ExtractPhotoUrlTests(SimpleTestCase):
    def test_payload_url(self):
        attachment = {"type": "image", "payload": {"url": "https://cdn.test/a.jpg"}}
        self.assertEqual(extract_photo_url(attachment), "https://cdn.test/a.jpg")

    def test_legacy_photos_dict_takes_last_candidate(self):
        attachment = {
            "type": "image",
            "payload": {
                "photos": {
                    "small": {"url": "https://cdn.test/s.jpg"},
                    "large": {"url": "https://cdn.test/l.jpg"},
                }
            },
        }
        self.assertEqual(extract_photo_url(attachment), "https://cdn.test/l.jpg")

    def test_legacy_photos_list(self):
        attachment = {
            "type": "image",
            "payload": {"photos": [{"url": "https://cdn.test/1.jpg"}, {"url": "https://cdn.test/2.jpg"}]},
        }
        self.assertEqual(extract_photo_url(attachment), "https://cdn.test/2.jpg")

    def test_malformed_attachment_returns_none(self):
        self.assertIsNone(extract_photo_url({}))
        self.assertIsNone(extract_photo_url({"type": "image"}))
        self.assertIsNone(extract_photo_url({"type": "image", "payload": {"url": ""}}))
        self.assertIsNone(extract_photo_url("not-a-dict"))


class PhotoSsrfTests(SimpleTestCase):
    def test_rejects_http_scheme(self):
        with self.assertRaises(PhotoDownloadError):
            download_photo("http://cdn.example.com/a.jpg")

    def test_rejects_file_scheme(self):
        with self.assertRaises(PhotoDownloadError):
            download_photo("file:///etc/passwd")

    def test_rejects_localhost(self):
        with self.assertRaises(PhotoDownloadError):
            download_photo("https://localhost/a.jpg")

    def test_rejects_loopback_ip(self):
        with self.assertRaises(PhotoDownloadError):
            download_photo("https://127.0.0.1/a.jpg")

    def test_rejects_ipv6_loopback(self):
        with self.assertRaises(PhotoDownloadError):
            download_photo("https://[::1]/a.jpg")

    def test_rejects_private_ip(self):
        with self.assertRaises(PhotoDownloadError):
            download_photo("https://10.0.0.5/a.jpg")
        with self.assertRaises(PhotoDownloadError):
            download_photo("https://192.168.1.10/a.jpg")

    def test_rejects_link_local_metadata_ip(self):
        with self.assertRaises(PhotoDownloadError):
            download_photo("https://169.254.169.254/latest/meta-data")

    def test_rejects_hostname_resolving_to_private_ip(self):
        with mock.patch.object(photo.socket, "getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.0.1", 443))
        ]):
            with self.assertRaises(PhotoDownloadError):
                download_photo("https://evil.example.com/a.jpg")

    def test_empty_and_garbage_urls(self):
        for bad in ["", "notaurl", "https://", "ftp://cdn.example.com/a.jpg"]:
            with self.assertRaises(PhotoDownloadError):
                download_photo(bad)


class PhotoDownloadTests(SimpleTestCase):
    def test_downloads_bytes_within_cap(self):
        with mock.patch.object(photo.socket, "getaddrinfo", return_value=PUBLIC_ADDRINFO):
            with _mock_download([b"abc", b"def"]) as client_cls:
                content = download_photo("https://cdn.example.com/a.jpg")
        self.assertEqual(content, b"abcdef")

    def test_oversized_image_aborts_stream(self):
        with mock.patch.object(photo, "MAX_PHOTO_BYTES", 100):
            with mock.patch.object(photo.socket, "getaddrinfo", return_value=PUBLIC_ADDRINFO):
                with _mock_download([b"x" * 60, b"x" * 60]):
                    with self.assertRaises(PhotoTooLargeError):
                        download_photo("https://cdn.example.com/big.jpg")

    def test_cdn_error_status(self):
        with mock.patch.object(photo.socket, "getaddrinfo", return_value=PUBLIC_ADDRINFO):
            with _mock_download([], status=500):
                with self.assertRaises(PhotoDownloadError):
                    download_photo("https://cdn.example.com/a.jpg")

    def test_safe_hostname_never_returns_query(self):
        self.assertEqual(safe_hostname("https://cdn.example.com/a.jpg?token=secret"), "cdn.example.com")
        self.assertEqual(safe_hostname(":::"), "<redacted>")
