#!/usr/bin/env python3
"""appenv.py — where the DeepSeek Harness desktop app keeps its things.

Pure standard library (no third-party packages), so ``dshctl.py`` can import it
on any machine that has CPython and nothing else.

Verified facts (shipped Electron shell ``@deepseek-ai/dsh-desktop`` + a live
macOS install):

* ``app.getPath('userData')`` is Electron's per-user data dir, and the leaf
  directory is the npm package name ``@deepseek-ai/dsh-desktop``:
  - macOS:   ``~/Library/Application Support/@deepseek-ai/dsh-desktop``
  - Linux:   ``$XDG_CONFIG_HOME/@deepseek-ai/dsh-desktop``
             (``~/.config/@deepseek-ai/dsh-desktop`` when XDG_CONFIG_HOME is unset)
  - Windows: ``%APPDATA%\\@deepseek-ai\\dsh-desktop``
* The Chromium cookie store is ``<userData>/Cookies`` (SQLite, table
  ``cookies``).  The app mints ``dsh-auth-*`` cookies whose value is a compact
  token ``v1.<b64url payload>.<signature>`` with
  ``payload.authority = "127.0.0.1:<port>"``.  The desktop server binds an
  ephemeral port, so the live port MUST come from the cookie payload; the
  static ``default_ports()`` list is only a probing fallback.
* The bundled CLI sits at ``<install dir>/resources/runtime/cli/bin/dsh``
  (``dsh.cmd`` on Windows).  A ``dsh`` on ``PATH`` is always the last resort.

Crypto is deliberately *best effort*: every failure path returns ``None``,
nothing raises, and no value is ever fabricated.

* macOS: Chromium keeps cookie values in the plaintext ``value`` column, so
  ``decrypt_cookie`` returns ``None`` and callers should use ``value``.
* Linux: ``v10`` is AES-128-CBC, key = PBKDF2-HMAC-SHA1("peanuts",
  "saltysalt", 1 iteration, 16 bytes), IV = 16 spaces, PKCS#7 padding.
  ``v11`` needs the libsecret keyring; we try ``secret-tool`` and return
  ``None`` when no secret can be read (instead of guessing).
* Windows: ``v10`` is AES-256-GCM with a DPAPI-protected key from Chromium's
  ``Local State`` (``os_crypt.encrypted_key``).  The code path is implemented
  but *best effort*: it only runs on Windows and any DPAPI/GCM/format problem
  yields ``None``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import ntpath
import os
import posixpath
import shutil
import sqlite3
import subprocess
import sys

__all__ = [
    "platform_id",
    "app_data_dir",
    "cookie_db_path",
    "dsh_cli_candidates",
    "default_ports",
    "jwt_authority",
    "read_auth_cookies",
    "decrypt_cookie",
]

# The Electron app's npm package name is also its userData directory name.
APP_PACKAGE = "@deepseek-ai/dsh-desktop"
APP_VENDOR = "@deepseek-ai"
APP_DIR_NAME = "dsh-desktop"
APP_DISPLAY_NAME = "DeepSeek Harness"

# Ports documented in README.md / docs/PROTOCOL.md (`51413`), the near-by drift
# window, and dshctl.py's helper fallback (`DEFAULT_PORT = 51499`).  The live
# app picks ephemeral ports, so this is only a fallback probe order.
_DOC_PORT = 51413
_PORT_WINDOW = 7  # 51414..51420
_HELPER_PORT = 51499

COOKIE_HOST = "127.0.0.1"
COOKIE_PREFIX = "dsh-auth"


# --------------------------------------------------------------------------- #
# platform / filesystem layout
# --------------------------------------------------------------------------- #
def platform_id(platform: str | None = None) -> str:
    """Return ``"darwin"``, ``"win32"`` or ``"linux"``.

    ``platform`` allows callers and tests to reason about another OS without
    mutating ``sys.platform``.  Unknown POSIX platforms fall back to
    ``"linux"`` because the layout rules are the XDG ones.
    """
    raw = str(sys.platform if platform is None else platform).lower()
    if raw.startswith(("darwin", "mac", "osx")):
        return "darwin"
    if raw.startswith(("win", "cygwin", "msys")):
        return "win32"
    return "linux"


def _env(env: dict[str, str] | None) -> dict[str, str]:
    return os.environ if env is None else env


def _home(pid: str, env: dict[str, str]) -> str:
    if pid == "win32":
        profile = env.get("USERPROFILE")
        if profile:
            return profile
        drive, path = env.get("HOMEDRIVE"), env.get("HOMEPATH")
        if drive and path:
            return drive + path
        return ntpath.expanduser("~")
    home = env.get("HOME")
    return home or posixpath.expanduser("~")


def app_data_dir(platform: str | None = None, env: dict[str, str] | None = None) -> str:
    """Electron ``app.getPath('userData')`` for the DSH desktop app."""
    pid = platform_id(platform)
    environment = _env(env)
    home = _home(pid, environment)

    if pid == "darwin":
        return posixpath.join(home, "Library", "Application Support", APP_PACKAGE)
    if pid == "win32":
        roaming = environment.get("APPDATA")
        if not roaming:
            roaming = ntpath.join(home, "AppData", "Roaming")
        return ntpath.join(roaming, APP_VENDOR, APP_DIR_NAME)
    config = environment.get("XDG_CONFIG_HOME") or posixpath.join(home, ".config")
    return posixpath.join(config, APP_PACKAGE)


def cookie_db_path(platform: str | None = None, env: dict[str, str] | None = None) -> str:
    """Chromium SQLite cookie store inside ``app_data_dir()``."""
    pid = platform_id(platform)
    base = app_data_dir(pid, env)
    join = ntpath.join if pid == "win32" else posixpath.join
    return join(base, "Cookies")


def dsh_cli_candidates(platform: str | None = None,
                       env: dict[str, str] | None = None) -> list[str]:
    """Plausible bundled ``dsh`` binaries, most-likely first.

    Existence is deliberately *not* checked here: the caller may want to report
    every candidate it tried.  A plain ``dsh`` / ``dsh.exe`` (resolved through
    ``PATH``) is always the final fallback.
    """
    pid = platform_id(platform)
    environment = _env(env)
    home = _home(pid, environment)
    out: list[str] = []

    if pid == "darwin":
        out.append("/Applications/DeepSeek Harness.app/Contents/Resources/"
                   "runtime/cli/bin/dsh")
    elif pid == "win32":
        local = environment.get("LOCALAPPDATA") or ntpath.join(
            home, "AppData", "Local")
        program_files = environment.get("PROGRAMFILES") or r"C:\Program Files"
        program_files_x86 = environment.get("PROGRAMFILES(X86)") or \
            r"C:\Program Files (x86)"
        for base in (ntpath.join(local, "Programs"), program_files, program_files_x86):
            out.append(ntpath.join(base, APP_DISPLAY_NAME, "resources",
                                   "runtime", "cli", "bin", "dsh.cmd"))
    else:
        out.append("/opt/DeepSeek Harness/resources/runtime/cli/bin/dsh")
        out.append("/usr/lib/DeepSeek Harness/resources/runtime/cli/bin/dsh")
        out.append(posixpath.join(home, ".local", "share", APP_DISPLAY_NAME,
                                  "resources", "runtime", "cli", "bin", "dsh"))

    out.append("dsh.exe" if pid == "win32" else "dsh")
    return _dedup(out)


def default_ports() -> list[int]:
    """Fallback probe order: documented port, drift window, helper port."""
    ports = [_DOC_PORT]
    ports.extend(range(_DOC_PORT + 1, _DOC_PORT + 1 + _PORT_WINDOW))
    ports.append(_HELPER_PORT)
    return _dedup(ports)


# --------------------------------------------------------------------------- #
# cookie payloads
# --------------------------------------------------------------------------- #
def jwt_authority(cookie_value: str) -> str | None:
    """``'v1.<b64url payload>.<sig>'`` -> ``'127.0.0.1:51413'``.

    Same semantics as ``dshctl._jwt_authority``: any malformed input simply
    yields ``None``.  No signature verification — the caller only needs to know
    which local authority the cookie was minted for.
    """
    try:
        if not isinstance(cookie_value, str):
            return None
        payload = cookie_value.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        authority = json.loads(base64.urlsafe_b64decode(payload)).get("authority")
        if isinstance(authority, str) and authority:
            return authority
        return None
    except Exception:
        return None


def _port_of(authority: str | None) -> int | None:
    """``'127.0.0.1:51413'`` -> ``51413`` (``None`` for anything else)."""
    if not isinstance(authority, str):
        return None
    _host, sep, port = authority.rpartition(":")
    if not sep or not port.isdigit():
        return None
    number = int(port)
    if not 1 <= number <= 65535:
        return None
    return number


def read_auth_cookies(db_path: str | None = None,
                      platform: str | None = None) -> list[tuple[str, str, int]]:
    """Read live ``dsh-auth-*`` cookies as ``(name, value, port)`` tuples.

    Newest ``expires_utc`` first.  Cookies with an empty plaintext ``value`` are
    decrypted through :func:`decrypt_cookie` when possible; anything that cannot
    be recovered or whose payload carries no usable ``authority`` port is
    skipped rather than guessed at.

    A missing/corrupt/locked store yields ``[]`` — this function never raises.
    """
    pid = platform_id(platform)
    path = db_path or cookie_db_path(pid)
    rows = _read_cookie_rows(path)
    found: list[tuple[str, str, int]] = []
    for name, value, encrypted in rows:
        if not isinstance(name, str) or not name:
            continue
        plain = value if isinstance(value, str) else ""
        if not plain and isinstance(encrypted, (bytes, bytearray)) and encrypted:
            plain = decrypt_cookie(bytes(encrypted), pid) or ""
        if not plain:
            continue
        port = _port_of(jwt_authority(plain))
        if port is None:
            continue
        found.append((name, plain, port))
    return found


def _read_cookie_rows(path: str) -> list[tuple]:
    """Best-effort read of ``cookies``; every failure becomes ``[]``."""
    if not path or not os.path.exists(path):
        return []
    query = ("select name, value, encrypted_value from cookies "
             "where host_key=? and name like ? order by expires_utc desc")
    params = (COOKIE_HOST, COOKIE_PREFIX + "%")
    con = None
    try:
        # Mirror dshctl.py: an immutable read avoids locking out the live app.
        try:
            con = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
            return list(con.execute(query, params).fetchall())
        except sqlite3.Error:
            if con is not None:
                try:
                    con.close()
                except Exception:
                    pass
                con = None
            con = sqlite3.connect(path)
            return list(con.execute(query, params).fetchall())
    except Exception:
        return []
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# Chromium cookie decryption (best effort, always None on failure)
# --------------------------------------------------------------------------- #
def decrypt_cookie(encrypted: bytes, platform: str | None = None) -> str | None:
    """Decrypt a Chromium ``encrypted_value`` blob to UTF-8 text.

    Returns ``None`` — never raises — when the platform keeps values in
    plaintext (macOS), when the cipher/version is unsupported, or when any
    step (keyring/DPAPI/padding/UTF-8) fails.
    """
    try:
        if not isinstance(encrypted, (bytes, bytearray, memoryview)):
            return None
        blob = bytes(encrypted)
        if not blob:
            return None
        pid = platform_id(platform)
        if pid == "darwin":
            # Chromium on macOS stores values in the plaintext `value` column.
            return None
        if pid == "win32":
            plain = _decrypt_windows(blob)
        else:
            plain = _decrypt_linux(blob)
        if plain is None:
            return None
        return plain.decode("utf-8")
    except Exception:
        return None


def _decrypt_linux(blob: bytes) -> bytes | None:
    if blob.startswith(b"v10"):
        # The well-known Chromium fallback password: PBKDF2("peanuts").
        key = hashlib.pbkdf2_hmac("sha1", b"peanuts", b"saltysalt", 1, 16)
    elif blob.startswith(b"v11"):
        # v11 uses a libsecret-stored password as the PBKDF2 input.  If the
        # keyring is not readable we must not guess: return None.
        secret = _linux_keyring_secret()
        if not secret:
            return None
        key = hashlib.pbkdf2_hmac("sha1", secret.encode("utf-8"),
                                  b"saltysalt", 1, 16)
    else:
        return None
    return _aes_cbc_decrypt(key, blob[3:])


def _linux_keyring_secret() -> str | None:
    """Best-effort libsecret lookup via ``secret-tool`` (``None`` if absent)."""
    tool = shutil.which("secret-tool")
    if not tool:
        return None
    searches = (
        ["lookup", "application", "chromium"],
        ["lookup", "application", "chrome"],
        ["lookup", "xdg:schema", "chrome_libsecret_os_crypt_password_v2",
         "application", "chrome"],
    )
    for args in searches:
        try:
            proc = subprocess.run([tool, *args], capture_output=True, text=True,
                                  timeout=2)
        except Exception:
            continue
        secret = (proc.stdout or "").strip()
        if proc.returncode == 0 and secret:
            return secret
    return None


def _decrypt_windows(blob: bytes) -> bytes | None:
    """Windows ``v10`` = AES-256-GCM, key protected by DPAPI (best effort)."""
    if not blob.startswith(b"v10"):
        return None
    if not sys.platform.startswith("win"):
        # DPAPI only exists on Windows.  Refuse to invent a result elsewhere.
        return None
    key = _windows_os_crypt_key()
    if not key:
        return None
    if len(blob) < 3 + 12 + 16:  # prefix + nonce + tag
        return None
    nonce = blob[3:15]
    return _aes_gcm_decrypt(key, nonce, blob[15:])


def _windows_local_state_paths() -> list[str]:
    """Locations of Chromium's ``Local State`` on Windows (best effort)."""
    env = os.environ
    paths: list[str] = []
    # Electron's userData (normally %APPDATA%) is where Local State lives, but
    # some installs keep it under %LOCALAPPDATA%, so try both.
    for base in (env.get("APPDATA"), env.get("LOCALAPPDATA")):
        if not base:
            continue
        path = os.path.join(base, APP_VENDOR, APP_DIR_NAME, "Local State")
        if path not in paths:
            paths.append(path)
    return paths


def _windows_os_crypt_key() -> bytes | None:
    if not sys.platform.startswith("win"):
        return None
    for path in _windows_local_state_paths():
        try:
            with open(path, "rb") as handle:
                state = json.loads(handle.read().decode("utf-8"))
            blob = base64.b64decode(state["os_crypt"]["encrypted_key"])
        except Exception:
            continue
        if blob.startswith(b"DPAPI"):
            blob = blob[5:]
        key = _dpapi_unprotect(blob)
        if key:
            return key
    return None


def _dpapi_unprotect(blob: bytes) -> bytes | None:
    """``CryptUnprotectData`` via ctypes; ``None`` on any failure."""
    if not blob or not sys.platform.startswith("win"):
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        buffer = ctypes.create_string_buffer(blob, len(blob))
        data_in = DATA_BLOB(len(blob),
                            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
        data_out = DATA_BLOB()
        if not crypt32.CryptUnprotectData(
                ctypes.byref(data_in), None, None, None, None, 0,
                ctypes.byref(data_out)):
            return None
        try:
            return ctypes.string_at(data_out.pbData, data_out.cbData)
        finally:
            kernel32.LocalFree(data_out.pbData)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Minimal AES (FIPS-197) + CBC + GCM — stdlib only, no third-party crypto
# --------------------------------------------------------------------------- #
def _rotl8(value: int, shift: int) -> int:
    return ((value << shift) | (value >> (8 - shift))) & 0xFF


def _gmul(a: int, b: int) -> int:
    """Multiply two bytes in GF(2^8) modulo the AES polynomial 0x11B."""
    result = 0
    for _ in range(8):
        if b & 1:
            result ^= a
        high = a & 0x80
        a = (a << 1) & 0xFF
        if high:
            a ^= 0x1B
        b >>= 1
    return result


def _build_sbox() -> list[int]:
    sbox = [0] * 256
    for value in range(256):
        # Multiplicative inverse in GF(2^8), then the AES affine transform.
        inverse = 0
        if value:
            for candidate in range(1, 256):
                if _gmul(value, candidate) == 1:
                    inverse = candidate
                    break
        sbox[value] = (inverse ^ _rotl8(inverse, 1) ^ _rotl8(inverse, 2)
                       ^ _rotl8(inverse, 3) ^ _rotl8(inverse, 4) ^ 0x63) & 0xFF
    return sbox


SBOX = _build_sbox()
INV_SBOX = [0] * 256
for _index, _value in enumerate(SBOX):
    INV_SBOX[_value] = _index
del _index, _value


def _expand_key(key: bytes) -> tuple[list[list[int]], int]:
    length = len(key)
    if length not in (16, 24, 32):
        raise ValueError("AES key must be 16, 24 or 32 bytes")
    nk = length // 4
    rounds = nk + 6
    words = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    rcon = 1
    for i in range(nk, 4 * (rounds + 1)):
        temp = list(words[i - 1])
        if i % nk == 0:
            temp = [SBOX[byte] for byte in temp[1:] + temp[:1]]
            temp[0] ^= rcon
            rcon = _gmul(rcon, 2)
        elif nk > 6 and i % nk == 4:
            temp = [SBOX[byte] for byte in temp]
        words.append([words[i - nk][j] ^ temp[j] for j in range(4)])
    return words, rounds


def _add_round_key(state: list[int], words: list[list[int]], rnd: int) -> None:
    for column in range(4):
        word = words[4 * rnd + column]
        for row in range(4):
            state[4 * column + row] ^= word[row]


def _shift_rows(state: list[int]) -> list[int]:
    out = [0] * 16
    for column in range(4):
        for row in range(4):
            out[4 * column + row] = state[4 * ((column + row) % 4) + row]
    return out


def _inv_shift_rows(state: list[int]) -> list[int]:
    out = [0] * 16
    for column in range(4):
        for row in range(4):
            out[4 * column + row] = state[4 * ((column - row) % 4) + row]
    return out


def _mix_columns(state: list[int]) -> list[int]:
    out = [0] * 16
    for column in range(4):
        a0, a1, a2, a3 = state[4 * column:4 * column + 4]
        out[4 * column + 0] = _gmul(a0, 2) ^ _gmul(a1, 3) ^ a2 ^ a3
        out[4 * column + 1] = a0 ^ _gmul(a1, 2) ^ _gmul(a2, 3) ^ a3
        out[4 * column + 2] = a0 ^ a1 ^ _gmul(a2, 2) ^ _gmul(a3, 3)
        out[4 * column + 3] = _gmul(a0, 3) ^ a1 ^ a2 ^ _gmul(a3, 2)
    return out


def _inv_mix_columns(state: list[int]) -> list[int]:
    out = [0] * 16
    for column in range(4):
        a0, a1, a2, a3 = state[4 * column:4 * column + 4]
        out[4 * column + 0] = (_gmul(a0, 14) ^ _gmul(a1, 11)
                               ^ _gmul(a2, 13) ^ _gmul(a3, 9))
        out[4 * column + 1] = (_gmul(a0, 9) ^ _gmul(a1, 14)
                               ^ _gmul(a2, 11) ^ _gmul(a3, 13))
        out[4 * column + 2] = (_gmul(a0, 13) ^ _gmul(a1, 9)
                               ^ _gmul(a2, 14) ^ _gmul(a3, 11))
        out[4 * column + 3] = (_gmul(a0, 11) ^ _gmul(a1, 13)
                               ^ _gmul(a2, 9) ^ _gmul(a3, 14))
    return out


def _aes_encrypt_block(key: bytes, block: bytes) -> bytes:
    words, rounds = _expand_key(key)
    state = list(block)
    _add_round_key(state, words, 0)
    for rnd in range(1, rounds):
        state = [SBOX[byte] for byte in state]
        state = _shift_rows(state)
        state = _mix_columns(state)
        _add_round_key(state, words, rnd)
    state = [SBOX[byte] for byte in state]
    state = _shift_rows(state)
    _add_round_key(state, words, rounds)
    return bytes(state)


def _aes_decrypt_block(key: bytes, block: bytes) -> bytes:
    words, rounds = _expand_key(key)
    state = list(block)
    _add_round_key(state, words, rounds)
    for rnd in range(rounds - 1, 0, -1):
        state = _inv_shift_rows(state)
        state = [INV_SBOX[byte] for byte in state]
        _add_round_key(state, words, rnd)
        state = _inv_mix_columns(state)
    state = _inv_shift_rows(state)
    state = [INV_SBOX[byte] for byte in state]
    _add_round_key(state, words, 0)
    return bytes(state)


def _pkcs7_unpad(data: bytes) -> bytes | None:
    if not data:
        return None
    pad = data[-1]
    if not 1 <= pad <= 16 or len(data) < pad:
        return None
    if data[-pad:] != bytes([pad]) * pad:
        return None
    return data[:-pad]


def _aes_cbc_decrypt(key: bytes, ciphertext: bytes) -> bytes | None:
    """AES-CBC decryption with the Chromium Linux IV and PKCS#7 unpadding."""
    if not ciphertext or len(ciphertext) % 16:
        return None
    previous = b" " * 16
    plain = bytearray()
    for offset in range(0, len(ciphertext), 16):
        block = ciphertext[offset:offset + 16]
        decrypted = _aes_decrypt_block(key, block)
        plain.extend(left ^ right for left, right in zip(decrypted, previous))
        previous = block
    return _pkcs7_unpad(bytes(plain))


_GCM_R = 0xE1000000000000000000000000000000


def _gcm_multiply(x: int, y: int) -> int:
    """GF(2^128) multiplication as used by GHASH."""
    product = 0
    value = y
    for bit in range(128):
        if (x >> (127 - bit)) & 1:
            product ^= value
        if value & 1:
            value = (value >> 1) ^ _GCM_R
        else:
            value >>= 1
    return product


def _ghash(hash_key: int, data: bytes) -> int:
    digest = 0
    for offset in range(0, len(data), 16):
        block = data[offset:offset + 16]
        if len(block) < 16:
            block += b"\x00" * (16 - len(block))
        digest = _gcm_multiply(digest ^ int.from_bytes(block, "big"), hash_key)
    return digest


def _aes_gcm_decrypt(key: bytes, nonce: bytes, data: bytes) -> bytes | None:
    """AES-GCM with an empty AAD; ``data`` is ``ciphertext || 16-byte tag``."""
    if len(key) not in (16, 24, 32) or len(nonce) != 12 or len(data) < 16:
        return None
    ciphertext, tag = data[:-16], data[-16:]
    hash_key = int.from_bytes(_aes_encrypt_block(key, bytes(16)), "big")
    hashed = ciphertext + b"\x00" * (-len(ciphertext) % 16)
    hashed += (0).to_bytes(8, "big") + (len(ciphertext) * 8).to_bytes(8, "big")
    mask = _aes_encrypt_block(key, nonce + b"\x00\x00\x00\x01")
    expected = (_ghash(hash_key, hashed)
                ^ int.from_bytes(mask, "big")).to_bytes(16, "big")
    if not _constant_time_equal(expected, tag):
        return None
    plain = bytearray()
    counter = 2
    for offset in range(0, len(ciphertext), 16):
        keystream = _aes_encrypt_block(key, nonce + counter.to_bytes(4, "big"))
        chunk = ciphertext[offset:offset + 16]
        plain.extend(left ^ right for left, right in zip(chunk, keystream))
        counter += 1
    return bytes(plain)


def _constant_time_equal(left: bytes, right: bytes) -> bool:
    if len(left) != len(right):
        return False
    difference = 0
    for a, b in zip(left, right):
        difference |= a ^ b
    return difference == 0


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _dedup(values: list) -> list:
    """Order-preserving de-duplication (paths/ports appear once)."""
    seen = set()
    out = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


if __name__ == "__main__":  # tiny self-check, handy when run by hand
    print("platform:", platform_id())
    print("app data:", app_data_dir())
    print("cookies: ", cookie_db_path())
    for candidate in dsh_cli_candidates():
        print("cli:", "found " if os.path.exists(candidate) else "missing",
              candidate)
