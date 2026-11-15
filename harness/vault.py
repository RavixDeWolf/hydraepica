#!/usr/bin/env python3
"""vault.py - one encrypted place for your passwords and keys, so they never sit in the config or a .env file.

    vault.py set NAME            asks for the value (not echoed); from a pipe, reads one line of stdin
    vault.py list                names, fingerprints and dates; never values
    vault.py get NAME            prints the value (for scripts; it lands in your terminal, so mind who is watching)
    vault.py run --env VAR=NAME [--env ...] -- CMD ...   runs CMD with those secrets in its environment only
    vault.py delete NAME         removes it (the value is kept in the superseded list, like a replace)

Anywhere the config names an environment variable for a secret (bot_token_env, key_env), "vault:NAME" works too.
The stage's Settings has a write-only form for the owner: add or replace, never read.

Storage: <state_dir>/vault/vault.enc, Fernet (AES-128-CBC + HMAC-SHA256), written atomically with mode 600 under a
file lock. The key is a separate file, [vault] key_file (default ~/.config/harness/vault.key, made on first use,
mode 600), or $HARNESS_VAULT_KEY. Replacing or deleting a secret keeps the old value in "superseded" for rollback.

Where it stops: anything running as your user can read the key file, so this protects secrets at rest and from
being pasted into configs, logs and chats; it does not protect them from your own account. For more, keep the key
on a different disk or make it readable only by a separate user that runs the stage.
"""
import argparse
import fcntl
import getpass
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
MAX_VALUE = 8192
KEEP_SUPERSEDED = 200


class VaultError(Exception):
    pass


class Exists(VaultError):
    pass


def fingerprint(value):
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()[:16]


def _settings():
    try:
        import config
        return config.section("vault") or {}, config.STATE_DIR
    except (Exception, SystemExit):
        return {}, os.path.expanduser("~/.local/state/harness")


class Vault:
    def __init__(self, path=None, key_file=None, key=None):
        s, state = _settings()
        self.path = path or os.path.expanduser(s.get("path") or os.path.join(state, "vault", "vault.enc"))
        self.key_file = key_file or os.path.expanduser(s.get("key_file") or "~/.config/harness/vault.key")
        self._key = key or os.environ.get("HARNESS_VAULT_KEY")

    # ---- key + file ------------------------------------------------------------------------------------------
    def _fernet(self, create=False):
        from cryptography.fernet import Fernet
        if self._key:
            return Fernet(self._key.encode() if isinstance(self._key, str) else self._key)
        if not os.path.exists(self.key_file):
            if not create:
                raise VaultError(f"no vault key at {self.key_file} (it is made on the first set)")
            os.makedirs(os.path.dirname(self.key_file), exist_ok=True)
            fd = os.open(self.key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(Fernet.generate_key())
        if os.stat(self.key_file).st_mode & 0o077:
            raise VaultError(f"{self.key_file} is readable by others; chmod 600 it")
        with open(self.key_file, "rb") as f:
            return Fernet(f.read().strip())

    @contextmanager
    def _locked(self):
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        with open(self.path + ".lock", "a") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lk, fcntl.LOCK_UN)

    def _read(self, create=False):
        f = self._fernet(create=create)
        if not os.path.exists(self.path):
            return f, {"entries": {}, "superseded": {}}
        from cryptography.fernet import InvalidToken
        try:
            with open(self.path, "rb") as fh:
                return f, json.loads(f.decrypt(fh.read()))
        except InvalidToken:
            raise VaultError("the vault file does not open with this key (wrong key, or the file was changed)")

    def _write(self, f, data):
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(f.encrypt(json.dumps(data).encode()))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    # ---- operations ------------------------------------------------------------------------------------------
    def set(self, name, value, replace=False):
        """-> {"name", "fingerprint", "replaced", "unchanged"}. Raises Exists unless replace=True."""
        name, value = str(name).strip(), str(value).strip()
        if not NAME_RE.match(name):
            raise VaultError("name: letters, digits, _ . - only (max 80)")
        if not value or len(value) > MAX_VALUE or "\n" in value:
            raise VaultError(f"value: one line, 1-{MAX_VALUE} characters")
        with self._locked():
            f, d = self._read(create=True)
            old = d["entries"].get(name)
            if old and old["value"] == value:
                return {"name": name, "fingerprint": fingerprint(value), "replaced": False, "unchanged": True}
            if old and not replace:
                raise Exists(f"{name} already exists: confirm to replace it")
            if old:
                self._supersede(d, name, old["value"])
            d["entries"][name] = {"value": value, "modified": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            self._write(f, d)
        return {"name": name, "fingerprint": fingerprint(value), "replaced": bool(old), "unchanged": False}

    @staticmethod
    def _supersede(d, name, value):
        sup = d.setdefault("superseded", {})
        sup[f"{name}@{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"] = value
        for k in sorted(sup)[:-KEEP_SUPERSEDED]:
            del sup[k]

    def get(self, name):
        _, d = self._read()
        e = d["entries"].get(name)
        if not e:
            raise VaultError(f"no secret named {name}")
        return e["value"]

    def list(self):
        try:
            _, d = self._read()
        except VaultError:
            if not os.path.exists(self.path):
                return []
            raise
        return [{"name": n, "fingerprint": fingerprint(e["value"]), "modified": e.get("modified")}
                for n, e in sorted(d["entries"].items())]

    def delete(self, name):
        with self._locked():
            f, d = self._read()
            e = d["entries"].pop(name, None)
            if not e:
                raise VaultError(f"no secret named {name}")
            self._supersede(d, name, e["value"])
            self._write(f, d)
        return {"name": name, "deleted": True}


def resolve(spec, default_env=None):
    """A config secret reference -> its value, or None. "vault:NAME" reads the vault; anything else is an env var."""
    spec = spec or default_env
    if not spec:
        return None
    if str(spec).startswith("vault:"):
        return Vault().get(spec[6:])
    return os.environ.get(spec)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="vault.py", description="encrypted secrets for HydraEpica")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("set"); s.add_argument("name"); s.add_argument("--replace", action="store_true")
    sub.add_parser("list")
    g = sub.add_parser("get"); g.add_argument("name")
    d = sub.add_parser("delete"); d.add_argument("name")
    r = sub.add_parser("run"); r.add_argument("--env", action="append", default=[]); r.add_argument("argv", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    v = Vault()
    try:
        if a.cmd == "set":
            val = getpass.getpass(f"value for {a.name}: ") if sys.stdin.isatty() else sys.stdin.readline()
            try:
                out = v.set(a.name, val, replace=a.replace)
            except Exists as e:
                if not sys.stdin.isatty() or input(f"{e}. Replace? [y/N] ").strip().lower() != "y":
                    raise
                out = v.set(a.name, val, replace=True)
            print(json.dumps(out))
        elif a.cmd == "list":
            for e in v.list():
                print(f"{e['name']:<32} {e['fingerprint']}  {e['modified'] or ''}")
        elif a.cmd == "get":
            print(v.get(a.name))
        elif a.cmd == "delete":
            print(json.dumps(v.delete(a.name)))
        elif a.cmd == "run":
            cmd = a.argv[1:] if a.argv[:1] == ["--"] else a.argv
            if not cmd:
                raise VaultError("run: give a command after --")
            env = dict(os.environ)
            for pair in a.env:
                var, _, name = pair.partition("=")
                if not var or not name:
                    raise VaultError(f"--env wants VAR=NAME, got {pair!r}")
                env[var] = v.get(name)
            return subprocess.call(cmd, env=env)
    except VaultError as e:
        print(f"vault: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
