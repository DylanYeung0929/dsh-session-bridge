#!/usr/bin/env python3
"""Offline unit tests for tools/appenv.py — no DSH app, no real cookie store."""
import base64
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))

import appenv  # noqa: E402

# A real-shaped dsh-auth cookie whose payload carries authority 127.0.0.1:51413.
TOKEN_51413 = ("v1.eyJ2ZXJzaW9uIjoxLCJhdXRob3JpdHkiOiIxMjcuMC4wLjE6NTE0MTMifQ"
               ".q4Z9test-signature")
# AES-128-CBC("peanuts"/PBKDF2, IV=16 spaces) of TOKEN_51413, generated with:
#   key = pbkdf2_hmac("sha1", b"peanuts", b"saltysalt", 1, 16)
#   openssl enc -aes-128-cbc -nosalt -K <key> -iv 2020...20
LINUX_V10_CIPHERTEXT = bytes.fromhex(
    "e004d16759d3e1041a38f9885aa43779df40bc5f753db2f211c0f053a7e3a4b1"
    "67d6f6524fd43c5056f00a7c713d7a818cea924db9831ed78fd1426b75d532ef"
    "6a2e72ce27ac0b047b0465839c8f1728db9d433aff039785762619b7e2863a47")


def make_token(authority: str, version: int = 1) -> str:
    payload = json.dumps({"version": version, "authority": authority},
                         separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
    return f"v1.{encoded}.test-signature"


def no_padding_token(payload: dict) -> str:
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload).encode()).decode().rstrip("=")
    return f"v1.{encoded}.sig"


def make_cookie_db(rows):
    """rows: (name, value, encrypted_value, host_key, expires_utc)."""
    handle, path = tempfile.mkstemp(prefix="appenv-cookies-", suffix=".sqlite")
    os.close(handle)
    import sqlite3
    con = sqlite3.connect(path)
    try:
        con.execute(
            "create table cookies (name text, value text, "
            "encrypted_value blob, host_key text, expires_utc integer)")
        con.executemany(
            "insert into cookies (name, value, encrypted_value, host_key, "
            "expires_utc) values (?, ?, ?, ?, ?)", rows)
        con.commit()
    finally:
        con.close()
    return path


class PlatformPathTest(unittest.TestCase):
    def test_platform_id_normalises_aliases(self):
        self.assertEqual(appenv.platform_id("darwin"), "darwin")
        self.assertEqual(appenv.platform_id("macos"), "darwin")
        self.assertEqual(appenv.platform_id("win32"), "win32")
        self.assertEqual(appenv.platform_id("windows"), "win32")
        self.assertEqual(appenv.platform_id("cygwin"), "win32")
        self.assertEqual(appenv.platform_id("linux"), "linux")
        self.assertEqual(appenv.platform_id("linux2"), "linux")
        self.assertEqual(appenv.platform_id(), appenv.platform_id(sys.platform))

    def test_macos_layout(self):
        env = {"HOME": "/Users/example"}
        base = "/Users/example/Library/Application Support/@deepseek-ai/dsh-desktop"
        self.assertEqual(appenv.app_data_dir("darwin", env), base)
        self.assertEqual(appenv.cookie_db_path("darwin", env),
                         base + "/Cookies")

    def test_macos_cli_candidates(self):
        self.assertEqual(
            appenv.dsh_cli_candidates("darwin", {"HOME": "/Users/example"}),
            ["/Applications/DeepSeek Harness.app/Contents/Resources/"
             "runtime/cli/bin/dsh", "dsh"])

    def test_linux_layout_uses_xdg_config_home(self):
        env = {"HOME": "/home/example", "XDG_CONFIG_HOME": "/xdg"}
        base = "/xdg/@deepseek-ai/dsh-desktop"
        self.assertEqual(appenv.app_data_dir("linux", env), base)
        self.assertEqual(appenv.cookie_db_path("linux", env), base + "/Cookies")

    def test_linux_layout_falls_back_to_dot_config(self):
        env = {"HOME": "/home/example"}
        base = "/home/example/.config/@deepseek-ai/dsh-desktop"
        self.assertEqual(appenv.app_data_dir("linux", env), base)

    def test_linux_cli_candidates(self):
        self.assertEqual(
            appenv.dsh_cli_candidates("linux", {"HOME": "/home/example"}),
            ["/opt/DeepSeek Harness/resources/runtime/cli/bin/dsh",
             "/usr/lib/DeepSeek Harness/resources/runtime/cli/bin/dsh",
             "/home/example/.local/share/DeepSeek Harness/resources/runtime/"
             "cli/bin/dsh",
             "dsh"])

    def test_windows_layout_uses_appdata(self):
        env = {"APPDATA": r"C:\Users\example\AppData\Roaming",
               "USERPROFILE": r"C:\Users\example"}
        base = r"C:\Users\example\AppData\Roaming\@deepseek-ai\dsh-desktop"
        self.assertEqual(appenv.app_data_dir("win32", env), base)
        self.assertEqual(appenv.cookie_db_path("win32", env),
                         base + r"\Cookies")

    def test_windows_layout_falls_back_without_appdata(self):
        env = {"USERPROFILE": r"C:\Users\example"}
        self.assertEqual(
            appenv.app_data_dir("win32", env),
            r"C:\Users\example\AppData\Roaming\@deepseek-ai\dsh-desktop")

    def test_windows_cli_candidates(self):
        env = {"APPDATA": r"C:\Users\example\AppData\Roaming",
               "USERPROFILE": r"C:\Users\example",
               "LOCALAPPDATA": r"C:\Users\example\AppData\Local",
               "PROGRAMFILES": r"C:\Program Files"}
        self.assertEqual(
            appenv.dsh_cli_candidates("win32", env),
            [r"C:\Users\example\AppData\Local\Programs\DeepSeek Harness"
             r"\resources\runtime\cli\bin\dsh.cmd",
             r"C:\Program Files\DeepSeek Harness\resources\runtime\cli"
             r"\bin\dsh.cmd",
             r"C:\Program Files (x86)\DeepSeek Harness\resources\runtime"
             r"\cli\bin\dsh.cmd",
             "dsh.exe"])

    def test_platform_argument_does_not_depend_on_host_os(self):
        # Joining must follow the *requested* platform's separators.
        self.assertIn("\\", appenv.app_data_dir("win32", {
            "APPDATA": r"C:\Users\example\AppData\Roaming"}))
        self.assertNotIn("\\", appenv.app_data_dir(
            "linux", {"HOME": "/home/example"}))

    def test_default_env_is_used_when_env_omitted(self):
        self.assertTrue(
            appenv.app_data_dir("darwin").endswith("@deepseek-ai/dsh-desktop"))


class PortsTest(unittest.TestCase):
    def test_default_ports_are_documented_then_drift_then_helper(self):
        self.assertEqual(appenv.default_ports(),
                         list(range(51413, 51421)) + [51499])

    def test_default_ports_are_unique_ints(self):
        ports = appenv.default_ports()
        self.assertEqual(len(ports), len(set(ports)))
        self.assertTrue(all(isinstance(port, int) for port in ports))
        self.assertIn(51413, ports)
        self.assertIn(51499, ports)


class JwtAuthorityTest(unittest.TestCase):
    def test_extracts_authority(self):
        self.assertEqual(appenv.jwt_authority(TOKEN_51413), "127.0.0.1:51413")

    def test_accepts_payload_without_base64_padding(self):
        token = no_padding_token({"authority": "127.0.0.1:51499",
                                  "version": 1})
        self.assertEqual(appenv.jwt_authority(token), "127.0.0.1:51499")

    def test_bad_inputs_return_none(self):
        bad_values = [
            "",
            "abc",
            "v1",
            "v1..sig",
            "v1.@@@.sig",
            "v1.eyJmb28iOiJiYXIifQ.sig",       # payload has no authority
            "v1." + base64.urlsafe_b64encode(b"[1,2,3]").decode() + ".sig",
            'v1.eyJhdXRob3JpdHkiOiAxMjN9.sig',  # authority is not a string
            "v1." + base64.urlsafe_b64encode(b"not json").decode() + ".sig",
            None,
            123,
        ]
        for value in bad_values:
            with self.subTest(value=value):
                self.assertIsNone(appenv.jwt_authority(value))


class AesPrimitiveTest(unittest.TestCase):
    def test_fips197_block_vector_encrypts_and_decrypts(self):
        key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        plain = bytes.fromhex("00112233445566778899aabbccddeeff")
        cipher = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
        self.assertEqual(appenv._aes_encrypt_block(key, plain), cipher)
        self.assertEqual(appenv._aes_decrypt_block(key, cipher), plain)

    def test_gcm_vector_matches_node_crypto(self):
        # Generated with Node: createCipheriv("aes-256-gcm", ...)
        key = bytes(range(32))
        nonce = bytes(range(12))
        payload = bytes.fromhex(
            "306bb87faa92b136ea22faa6d2861706eab3"
            "45845e6a3956150468dc466bdee98552")
        self.assertEqual(appenv._aes_gcm_decrypt(key, nonce, payload),
                         b"windows-gcm-cookie")

    def test_gcm_rejects_tampered_tag(self):
        key = bytes(range(32))
        nonce = bytes(range(12))
        payload = bytearray(bytes.fromhex(
            "306bb87faa92b136ea22faa6d2861706eab3"
            "45845e6a3956150468dc466bdee98552"))
        payload[-1] ^= 0xFF
        self.assertIsNone(appenv._aes_gcm_decrypt(key, nonce, bytes(payload)))


class DecryptCookieTest(unittest.TestCase):
    def test_linux_v10_vector_decrypts_to_the_original_token(self):
        blob = b"v10" + LINUX_V10_CIPHERTEXT
        self.assertEqual(appenv.decrypt_cookie(blob, "linux"), TOKEN_51413)
        self.assertEqual(appenv.jwt_authority(
            appenv.decrypt_cookie(blob, "linux")), "127.0.0.1:51413")

    def test_linux_garbage_returns_none(self):
        garbage = [b"v10", b"v10" + bytes(16), b"v10" + bytes(32),
                   b"v10" + b"short", b"v99" + bytes(16), b"random-bytes",
                   b"\x00" * 16]
        for blob in garbage:
            with self.subTest(blob=blob):
                self.assertIsNone(appenv.decrypt_cookie(blob, "linux"))

    def test_unknown_prefix_returns_none(self):
        self.assertIsNone(appenv.decrypt_cookie(b"v12" + bytes(32), "linux"))
        self.assertIsNone(appenv.decrypt_cookie(b"", "linux"))

    def test_non_bytes_input_returns_none(self):
        for value in (None, "v10abc", 123, object()):
            with self.subTest(value=value):
                self.assertIsNone(appenv.decrypt_cookie(value, "linux"))

    def test_macos_plaintext_cookie_is_not_decrypted(self):
        blob = b"v10" + LINUX_V10_CIPHERTEXT
        self.assertIsNone(appenv.decrypt_cookie(blob, "darwin"))

    def test_windows_path_is_best_effort_and_never_raises(self):
        blob = b"v10" + bytes(range(12)) + bytes.fromhex(
            "306bb87faa92b136ea22faa6d2861706eab3"
            "45845e6a3956150468dc466bdee98552")
        self.assertIsNone(appenv.decrypt_cookie(blob, "win32"))

    def test_linux_v11_without_keyring_is_safe(self):
        # v11 needs libsecret; either a string (keyring available) or None.
        result = appenv.decrypt_cookie(b"v11" + bytes(32), "linux")
        self.assertTrue(result is None or isinstance(result, str))


class ReadAuthCookiesTest(unittest.TestCase):
    def setUp(self):
        self.db = make_cookie_db([
            ("dsh-auth-old", TOKEN_51413, b"", "127.0.0.1", 100),
            ("dsh-auth-new", make_token("127.0.0.1:51499"), b"",
             "127.0.0.1", 200),
            ("dsh-auth-bad", "not-a-jwt", b"", "127.0.0.1", 300),
            ("dsh-auth-enc", "", b"v10" + LINUX_V10_CIPHERTEXT,
             "127.0.0.1", 400),
            ("dsh-auth-otherhost", TOKEN_51413, b"", "example.com", 500),
            ("dsh-auth-empty", "", b"", "127.0.0.1", 600),
        ])

    def tearDown(self):
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                os.unlink(self.db + suffix)
            except OSError:
                pass

    def test_linux_reads_plaintext_and_decrypts_newest_first(self):
        found = appenv.read_auth_cookies(self.db, platform="linux")
        self.assertEqual([name for name, _value, _port in found],
                         ["dsh-auth-enc", "dsh-auth-new", "dsh-auth-old"])
        self.assertEqual([port for _name, _value, port in found],
                         [51413, 51499, 51413])
        self.assertEqual(found[0][1], TOKEN_51413)

    def test_macos_uses_plaintext_value_only(self):
        found = appenv.read_auth_cookies(self.db, platform="darwin")
        self.assertEqual([name for name, _value, _port in found],
                         ["dsh-auth-new", "dsh-auth-old"])

    def test_missing_store_returns_empty_list(self):
        self.assertEqual(
            appenv.read_auth_cookies("/nonexistent/Cookies", "linux"), [])

    def test_corrupt_store_returns_empty_list(self):
        handle, path = tempfile.mkstemp(prefix="appenv-broken-")
        os.close(handle)
        try:
            with open(path, "wb") as fh:
                fh.write(b"definitely not sqlite")
            self.assertEqual(appenv.read_auth_cookies(path, "linux"), [])
        finally:
            os.unlink(path)

    def test_directory_path_returns_empty_list(self):
        directory = tempfile.mkdtemp(prefix="appenv-dir-")
        try:
            self.assertEqual(appenv.read_auth_cookies(directory, "linux"), [])
        finally:
            os.rmdir(directory)


if __name__ == "__main__":
    unittest.main()
