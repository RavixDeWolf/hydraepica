"""The skills shipped to every head: well-formed, and every command they teach really exists in this repo."""
import json
import os
import re
import sys
import tempfile
import unittest

import _harness_env  # noqa: F401  throwaway config + state

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS = os.path.join(ROOT, "harness", "skills")
sys.path.insert(0, os.path.join(ROOT, "harness"))


def skills():
    return {n: open(os.path.join(SKILLS, n, "SKILL.md")).read() for n in sorted(os.listdir(SKILLS))
            if os.path.isfile(os.path.join(SKILLS, n, "SKILL.md"))}


def code(text):
    """only the fenced command blocks: prose mentioning a tool is not a command."""
    return "\n".join(re.findall(r"```(?:bash|python)?\n(.+?)```", text, re.S))


def src(rel):
    return open(os.path.join(ROOT, rel)).read()


# lab words that must never reach a public skill, stored reversed so this file passes the same privacy scan
PRIVATE = "|".join(r"\b" + w[::-1] + r"\b" for w in ("var", "moclaialt", "tluav", "noiton", "civ"))


class WellFormed(unittest.TestCase):
    def test_the_five_are_there(self):
        self.assertEqual(set(skills()), {"stage", "telegram", "handoff", "self-edit", "ship-a-gate", "smaller-models"})

    def test_frontmatter_name_matches_folder_and_has_a_description(self):
        for name, text in skills().items():
            m = re.match(r"---\nname: (.+)\ndescription: (.+)\n---\n", text)
            self.assertIsNotNone(m, name)
            self.assertEqual(m.group(1).strip(), name)
            self.assertGreater(len(m.group(2)), 40, name)

    def test_no_lab_names_or_people(self):
        for name, text in skills().items():
            self.assertIsNone(re.search(PRIVATE, text, re.I), name)


class CommandsExist(unittest.TestCase):
    def test_every_script_path_exists(self):
        for name, text in skills().items():
            for rel in re.findall(r"~/harness/(\S+?\.py)", text):
                self.assertTrue(os.path.isfile(os.path.join(ROOT, rel)), f"{name}: {rel}")

    def test_tg_actions_are_real(self):
        tg = src("harness/tg/tg.py")
        for act in re.findall(r"tg\.py (\w+)", code(skills()["telegram"])):
            self.assertRegex(tg, rf'["\']{act}["\']', act)

    def test_stage_kinds_are_real(self):
        st = src("harness/stage/stage.py")
        for kind in re.findall(r"stage\.py --to <person> (\w+)", skills()["stage"]):
            self.assertIn(f'"{kind}"', st, kind)

    def test_render_kinds_are_real(self):
        r = src("harness/render/render.py")
        for kind in ("md", "mermaid", "html"):
            self.assertIn(f"render.py {kind}", r)

    def test_headsafe_subcommands_are_real(self):
        hs = src("safety/headsafe.py")
        for sub in re.findall(r"headsafe\.py (\w+)", skills()["self-edit"].replace("`python3", "`")):
            self.assertRegex(hs, rf'["\']{sub}["\']', sub)
        self.assertIn("ALIVE", hs)

    def test_manifest_keys_are_read_by_ship_check(self):
        sc = src("safety/ship_check.py")
        block = re.search(r"```json\n(.+?)```", skills()["ship-a-gate"], re.S).group(1)
        manifest = json.loads(block)
        read = lambda k: re.search(rf'(\.get\(|\[)"{re.escape(k)}"', sc)   # actually read, not just mentioned
        for key, val in manifest.items():
            self.assertTrue(read(key), key)
            for sub in (val if isinstance(val, dict) else {}):
                self.assertTrue(read(sub), f"{key}.{sub}")
        for word in ("SURVIVED", "NOT_APPLIED", "REFUSED", "SHIP_CHECK_MUTATING", "recover"):
            self.assertIn(word, sc)

    def test_handoff_flag_is_real(self):
        self.assertIn('"--to"', src("harness/tg/handoff.py"))


class Linking(unittest.TestCase):
    def test_links_every_skill_and_keeps_a_heads_own(self):
        import new_lane
        with tempfile.TemporaryDirectory() as d:
            own = os.path.join(d, ".claude", "skills", "stage")
            os.makedirs(own)
            done = new_lane.link_skills(d, SKILLS)
            self.assertEqual(set(done), {"telegram", "handoff", "self-edit", "ship-a-gate", "smaller-models"})
            self.assertFalse(os.path.islink(own))          # a head's own override is left alone
            for n in done:
                self.assertTrue(os.path.isfile(os.path.join(d, ".claude", "skills", n, "SKILL.md")))
            self.assertEqual(new_lane.link_skills(d, SKILLS), [])   # idempotent


if __name__ == "__main__":
    unittest.main()
