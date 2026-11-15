"""vault: encrypted at rest, write-only from the phone, every replace keeps the old value, config "vault:NAME"."""
import _harness_env  # noqa: F401  throwaway config + state (must be first)
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "harness"))
import vault  # noqa: E402

D = tempfile.mkdtemp(prefix="vault_test_")
os.environ["HARNESS_VAULT_KEY"] = ""          # tests use key files they make themselves


def fresh():
    d = tempfile.mkdtemp(dir=D)
    return vault.Vault(path=os.path.join(d, "v", "vault.enc"), key_file=os.path.join(d, "k", "vault.key"))


class Store(unittest.TestCase):
    def test_set_get_list_and_encrypted_on_disk(self):
        v = fresh()
        out = v.set("MAIL_PASSWORD", "hunter2-very-secret")
        self.assertEqual(out["fingerprint"], vault.fingerprint("hunter2-very-secret"))
        self.assertEqual(v.get("MAIL_PASSWORD"), "hunter2-very-secret")
        self.assertEqual([e["name"] for e in v.list()], ["MAIL_PASSWORD"])
        self.assertNotIn("hunter2", json.dumps(v.list()))
        raw = open(v.path, "rb").read()
        self.assertNotIn(b"hunter2", raw); self.assertNotIn(b"MAIL_PASSWORD", raw)
        for p in (v.path, v.key_file):
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)

    def test_replace_needs_confirm_and_keeps_old(self):
        v = fresh()
        v.set("K", "one")
        with self.assertRaises(vault.Exists):
            v.set("K", "two")
        self.assertEqual(v.get("K"), "one")
        self.assertTrue(v.set("K", "one")["unchanged"])
        self.assertTrue(v.set("K", "two", replace=True)["replaced"])
        self.assertEqual(v.get("K"), "two")
        _, d = v._read()
        self.assertEqual(list(d["superseded"].values()), ["one"])

    def test_delete_keeps_old_value(self):
        v = fresh()
        v.set("K", "x"); v.delete("K")
        with self.assertRaises(vault.VaultError):
            v.get("K")
        self.assertEqual(list(v._read()[1]["superseded"].values()), ["x"])

    def test_bad_names_and_values(self):
        v = fresh()
        for n, val in (("../etc", "x"), ("a b", "x"), ("", "x"), ("ok", ""), ("ok", "two\nlines"), ("ok", "x" * 9000)):
            with self.assertRaises(vault.VaultError):
                v.set(n, val)

    def test_wrong_key_and_loose_key_file_refused(self):
        v = fresh(); v.set("K", "x")
        w = vault.Vault(path=v.path, key_file=v.key_file + ".other")
        w._key = vault.Vault(path="/x", key_file="/x")._key or __import__("cryptography.fernet").fernet.Fernet.generate_key()
        with self.assertRaises(vault.VaultError):
            w.get("K")
        os.chmod(v.key_file, 0o644)
        with self.assertRaises(vault.VaultError):
            v.get("K")

    def test_empty_vault_lists_nothing_without_a_key(self):
        self.assertEqual(fresh().list(), [])


class Cli(unittest.TestCase):
    def test_set_from_pipe_list_run(self):
        v = fresh()
        env = dict(os.environ, HARNESS_CONFIG=os.environ["HARNESS_CONFIG"])
        py = [sys.executable, os.path.join(HERE, "..", "harness", "vault.py")]
        cfg = os.path.join(D, "cli.toml")
        open(cfg, "w").write(open(os.environ["HARNESS_CONFIG"]).read() + f'\n[vault]\npath = "{v.path}"\nkey_file = "{v.key_file}"\n')
        env["HARNESS_CONFIG"] = cfg
        r = subprocess.run(py + ["set", "API"], input="s3cr3t-value\n", text=True, capture_output=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run(py + ["list"], text=True, capture_output=True, env=env)
        self.assertIn("API", r.stdout); self.assertNotIn("s3cr3t", r.stdout)
        r = subprocess.run(py + ["run", "--env", "TOKEN=API", "--", sys.executable, "-c",
                                 "import os; print(len(os.environ['TOKEN']))"], text=True, capture_output=True, env=env)
        self.assertEqual(r.stdout.strip(), "12")
        r = subprocess.run(py + ["set", "API"], input="other\n", text=True, capture_output=True, env=env)
        self.assertEqual(r.returncode, 1); self.assertIn("already exists", r.stderr)


class Config(unittest.TestCase):
    def test_vault_reference_resolves(self):
        import config
        v = fresh(); v.set("BOT", "123:abc")
        old = vault.Vault.__init__
        vault.Vault.__init__ = lambda self, *a, **k: old(self, path=v.path, key_file=v.key_file)
        try:
            self.assertEqual(config.secret("vault:BOT"), "123:abc")
            self.assertIsNone(config.secret("vault:NOPE"))
        finally:
            vault.Vault.__init__ = old
        os.environ["SOME_ENV_SECRET"] = "e"
        self.assertEqual(config.secret("SOME_ENV_SECRET"), "e")
        self.assertIsNone(config.secret(None))


class Stage(unittest.TestCase):
    """The phone form: admins only, fresh sign-in only, write-only, every write said."""
    @classmethod
    def setUpClass(cls):
        import test_stage_server as t
        cls.t, cls.s, cls.c = t, t.s, t.c
        cls.v = fresh()
        cls._old = vault.Vault.__init__
        old, v = cls._old, cls.v
        vault.Vault.__init__ = lambda self, *a, **k: old(self, path=v.path, key_file=v.key_file)
        cls.notes = []
        cls._note = cls.s._vault_note
        cls.s._vault_note = cls.notes.append

    @classmethod
    def tearDownClass(cls):
        vault.Vault.__init__ = cls._old
        cls.s._vault_note = cls._note

    def post(self, uid, body, **kw):
        return self.c.post("/api/vault/set", json=body, headers=self.t.H(uid, **kw))

    def test_admin_adds_replaces_with_confirm_never_reads(self):
        r = self.post(self.t.OWNER, {"name": "MAIL_PASSWORD", "value": "pw-one"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("pw-one", r.text)
        self.assertEqual(self.post(self.t.OWNER, {"name": "MAIL_PASSWORD", "value": "pw-two"}).status_code, 409)
        self.assertEqual(self.post(self.t.OWNER, {"name": "MAIL_PASSWORD", "value": "pw-two", "replace": True}).status_code, 200)
        self.assertEqual(self.v.get("MAIL_PASSWORD"), "pw-two")
        lst = self.c.get("/api/vault", headers=self.t.H(self.t.OWNER)).json()["entries"]
        self.assertIn("MAIL_PASSWORD", [e["name"] for e in lst]); self.assertNotIn("pw-", json.dumps(lst))
        self.assertTrue(any("added" in n for n in self.notes) and any("replaced" in n for n in self.notes))
        self.assertFalse(any("pw-" in n for n in self.notes))

    def test_refusals(self):
        self.assertEqual(self.post(self.t.USER_C, {"name": "X", "value": "y"}).status_code, 403)     # not an admin
        self.assertEqual(self.post(self.t.OWNER, {"name": "X", "value": "y"}, age=1000).status_code, 401)  # stale sign-in
        self.assertEqual(self.c.post("/api/vault/set", json={"name": "X", "value": "y"}).status_code, 401)
        self.assertEqual(self.post(self.t.OWNER, {"name": "../x", "value": "y"}).status_code, 400)
        self.assertEqual(self.c.get("/api/vault", headers=self.t.H(self.t.USER_C)).status_code, 403)
        self.assertEqual(self.c.get("/api/vault/MAIL_PASSWORD", headers=self.t.H(self.t.OWNER)).status_code, 404)
