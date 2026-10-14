"""The Workato CM capture's CW Agentic support, offline: no browser, no Workato.

Run from runner/:  python3 -m unittest discover -s tests -t .
"""
import unittest
from unittest.mock import patch

from workato import sox_capture as sox


class BuiltinListTests(unittest.TestCase):
    def test_agentic_workspace_gets_the_agentic_list(self):
        for ws in ("CW Agentic", "development", " Development "):
            self.assertEqual(sox.workspace_label(ws), "CW Agentic")
            tabs = [s["tab"] for s in sox.builtin_sections(ws)]
            self.assertEqual(tabs, ["1. Agentic - All Genies", "2. Agentic - Common Utilities",
                                    "3. Procurement Assistant Genie", "4. Legal"])

    def test_production_keeps_the_classic_list(self):
        self.assertEqual(sox.workspace_label("Production"), "")
        self.assertIs(sox.builtin_sections("Production"), sox.SECTIONS)

    def test_agentic_list_captures_only_capture_y_rows(self):
        folders = {f["name"]: f for s in sox.builtin_sections("CW Agentic") for f in s["folders"]}
        genies = folders["0. Agentic | All Genies"]
        self.assertEqual(genies["fid"], "27857281")
        self.assertEqual(set(genies["known"]), {"71414498", "68409974", "69382829"})
        self.assertIn("72943334", genies["skip"])
        self.assertEqual(genies["groups"]["71414498"], "RevOps Genie")
        self.assertFalse(genies["assets_only"])
        captured = sum(len(f["known"]) for f in folders.values())
        self.assertEqual(captured, 12)  # 3 per folder, plus each folder's assets page

    def test_project_names_follow_the_workspace(self):
        self.assertEqual(sox.project_names("Development")[0], "1. Agentic - All Genies")
        self.assertEqual(sox.project_names("Production")[0], "1. Workday Payroll to NS GL")


class FakePage:
    def __init__(self, texts):
        self.texts, self.url, self.context = list(texts), "https://app.workato.com/", self

    @property
    def pages(self):
        return [self]

    def is_closed(self):
        return False

    def goto(self, url, **kw):
        self.url = url

    def evaluate(self, js):
        return self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]


class WorkspaceCheckTests(unittest.TestCase):
    def test_waits_without_a_prompt_until_the_workspace_shows(self):
        page = FakePage(["Projects Production", "Projects CW Agentic Development"])
        with patch.object(sox.time, "sleep"), patch("builtins.input", side_effect=AssertionError("prompted")):
            sox.ensure_workspace(page, "Development")

    def test_gives_up_instead_of_hanging(self):
        clock = iter(range(0, 10_000, 100))
        with patch.object(sox.time, "sleep"), patch.object(sox.time, "time", lambda: next(clock)), \
                patch.object(sox, "activate_browser"), self.assertRaises(SystemExit):
            sox.ensure_workspace(FakePage(["Projects Production"]), "Development", timeout_s=600)


class DeepDiscoveryTests(unittest.TestCase):
    def test_recipes_in_subfolders_and_later_pages_are_found(self):
        tree = {"1": ({"10": "top"}, {"2": "Sub A"}), "2": ({"20": "nested"}, {"3": "Sub B"}),
                "3": ({"30": "deeper"}, {})}

        class Cap(sox.Capturer):
            def __init__(self):
                self.at = "1"

            def goto(self, url, must_contain=None, retries=3):
                self.at = sox.FID_RE.search(url).group(1)

            def discover_recipes_paged(self, max_pages=10):
                return dict(tree[self.at][0])

            def discover_subfolders(self, fid):
                return dict(tree[str(fid)][1])

        self.assertEqual(Cap().discover_recipes_deep("1"), {"10": "top", "20": "nested", "30": "deeper"})


if __name__ == "__main__":
    unittest.main()
