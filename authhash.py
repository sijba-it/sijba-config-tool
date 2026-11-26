"""Hashed auth tokens: `auth_token` in core.yaml / SIJBA_AUTH_TOKEN may hold a hash
instead of the secret, so a leaked config file or Secret does not leak the login.

    sha256$<hex>                        fast; only for long random tokens (>=128 bit)
    scrypt$<n>$<r>$<p>$<salt>$<hash>    slow + salted; for human-chosen passwords
                                        (salt and hash are urlsafe base64, no padding)

Clients still send the plain secret (`Authorization: Bearer <secret>`); Core hashes
what it receives and compares. Anything that is not in one of these formats is a
plain token, compared as before.

Generate one:  python -m core.authhash            (random token, prints secret + hash)
               python -m core.authhash --password (prompts, prints scrypt hash)
"""
from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import secrets
import sys

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
_PREFIXES = ("sha256$", "scrypt$")

# scrypt costs ~50 ms; every API call carries the token, so remember secrets that
# already verified (keyed by their sha256, never the secret itself).
_verified: dict[str, str] = {}
_VERIFIED_MAX = 256


def is_hash(value: str | None) -> bool:
    return bool(value) and value.startswith(_PREFIXES)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_sha256(secret: str) -> str:
    return "sha256$" + hashlib.sha256(secret.encode()).hexdigest()


def hash_scrypt(secret: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(secret.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(dk)}"


def verify(candidate: str, stored: str) -> bool:
    """True if `candidate` matches the hash in `stored`. Malformed hashes never match."""
    try:
        if stored.startswith("sha256$"):
            return hmac.compare_digest(hash_sha256(candidate).encode(), stored.encode())
        if stored.startswith("scrypt$"):
            key = hashlib.sha256(candidate.encode()).hexdigest()
            if _verified.get(key) == stored:
                return True
            _, n, r, p, salt, want = stored.split("$")
            dk = hashlib.scrypt(candidate.encode(), salt=_unb64(salt), n=int(n), r=int(r),
                                p=int(p), dklen=len(_unb64(want)), maxmem=256 * 1024 * 1024)
            ok = hmac.compare_digest(dk, _unb64(want))
            if ok:
                if len(_verified) >= _VERIFIED_MAX:
                    _verified.clear()
                _verified[key] = stored
            return ok
    except (ValueError, TypeError):
        return False
    return False


def main(argv: list[str]) -> int:
    if "--password" in argv:
        pw = getpass.getpass("Password: ")
        if pw != getpass.getpass("Again: "):
            print("passwords differ", file=sys.stderr)
            return 1
        if len(pw) < 12:
            print("password too short (min 12 characters)", file=sys.stderr)
            return 1
        print(hash_scrypt(pw))
        return 0
    secret = secrets.token_urlsafe(32)
    print(f"secret (give to clients, keep it out of the repo): {secret}")
    print(f"auth_token / SIJBA_AUTH_TOKEN value:               {hash_sha256(secret)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))


# ---- config-tool only: turn a plaintext auth_token in core.yaml into a hash ----

def harden_value(value) -> str | None:
    """Hash for a plaintext secret, or None if there is nothing to do (empty, already a
    hash, or a ${ENV} reference). Long secrets (>=32 chars, i.e. generated tokens) get the
    fast sha256 form, anything shorter - a human password - the salted scrypt form."""
    if not isinstance(value, str) or not value.strip() or is_hash(value) or "${" in value:
        return None
    value = value.strip()
    return hash_sha256(value) if len(value) >= 32 else hash_scrypt(value)


def harden_core_yaml(content: str) -> str:
    """core.yaml text with a plaintext `auth_token` replaced by its hash. Comments and
    layout are kept when `auth_token:` is a plain top-level line; anything else is
    re-dumped. Unparsable content is returned unchanged (validation reports it)."""
    import json
    import re
    import yaml
    try:
        data = yaml.safe_load(content) or {}
    except yaml.YAMLError:
        return content
    if not isinstance(data, dict):
        return content
    hashed = harden_value(data.get("auth_token"))
    if hashed is None:
        return content
    new, n = re.subn(r"(?m)^auth_token:[^\n]*$", "auth_token: " + json.dumps(hashed), content, count=1)
    if n:
        return new
    data["auth_token"] = hashed
    return yaml.safe_dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)
