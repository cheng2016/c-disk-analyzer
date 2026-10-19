"""Cache cleanup must refuse anything outside the approved cache folders."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.caches import CACHE_RULES, UnsafePathError, assert_deletable, clean_cache_ids, delete_inside
from app.winpath import path_is_inside


def _rule(cache_id, kind, paths, risk="safe", names=None):
    rule = {"id": cache_id, "name": cache_id, "risk": risk, "kind": kind, "paths": paths, "note": ""}
    if names is not None:
        rule["names"] = names
    return rule


class CacheSafetyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_prefix_boundary(self):
        self.assertTrue(path_is_inside(r"C:\Windows\Temp", r"C:\Windows\Temp"))
        self.assertTrue(path_is_inside(r"C:\Windows\Temp\a", r"C:\Windows\Temp"))
        self.assertTrue(path_is_inside(r"c:\WINDOWS\Temp\A", r"C:\Windows\Temp"))
        self.assertFalse(path_is_inside(r"C:\Windows\Template", r"C:\Windows\Temp"))
        self.assertFalse(path_is_inside(r"C:\Windows", r"C:\Windows\Temp"))
        self.assertFalse(path_is_inside("C:\\", r"C:\Windows"))

    def test_catalog_templates_are_specific(self):
        ids = []
        for rule in CACHE_RULES:
            self.assertIn(rule["risk"], {"safe", "review"})
            self.assertRegex(rule["id"], r"^[a-z0-9_]+$")
            self.assertIn(rule["kind"], {"dir", "glob_dir", "files"})
            ids.append(rule["id"])
            for raw in rule["paths"]:
                self.assertNotIn("..", raw.replace("/", "\\").split("\\"))
                if rule["kind"] == "glob_dir":
                    self.assertEqual(raw.count("*"), 1)
                else:
                    self.assertNotIn("*", raw)
        self.assertEqual(len(ids), len(set(ids)))
        review = {rule["id"] for rule in CACHE_RULES if rule["risk"] == "review"}
        self.assertTrue({"windows_update", "maven_repo", "wechat_data"} <= review)

    def test_review_catalog_id_does_not_walk(self):
        with patch("app.caches.os.scandir", side_effect=AssertionError("walked")), \
             patch("app.caches.os.unlink", side_effect=AssertionError("unlinked")):
            result = clean_cache_ids(["windows_update"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["freed_bytes"], 0)

    def test_unknown_and_path_ids_are_rejected(self):
        self.assertFalse(clean_cache_ids(["not_in_catalog"])["ok"])
        self.assertFalse(clean_cache_ids([r"C:\Windows\Temp"])["ok"])
        self.assertFalse(clean_cache_ids(["../secret"])["ok"])

    def test_mixed_request_deletes_nothing(self):
        cache = self.root / "cache"
        cache.mkdir()
        junk = cache / "a.txt"
        junk.write_text("keep", encoding="utf-8")
        rules = [
            _rule("temp_box", "dir", [str(cache)]),
            _rule("keep_box", "dir", [str(cache)], risk="review"),
        ]
        result = clean_cache_ids(["temp_box", "keep_box"], rules=rules)
        self.assertFalse(result["ok"])
        self.assertTrue(junk.exists())
        self.assertEqual(junk.read_text(encoding="utf-8"), "keep")

    def test_clears_contents_not_the_folder(self):
        cache = self.root / "cache"
        nested = cache / "sub"
        nested.mkdir(parents=True)
        junk = nested / "a.txt"
        junk.write_text("hello", encoding="utf-8")
        sibling = self.root / "keep"
        sibling.mkdir()
        photo = sibling / "photo.txt"
        photo.write_text("keep", encoding="utf-8")
        result = clean_cache_ids(["temp_box"], rules=[_rule("temp_box", "dir", [str(cache)])])
        self.assertTrue(result["ok"], result)
        self.assertFalse(junk.exists())
        self.assertFalse(nested.exists())
        self.assertTrue(cache.exists())
        self.assertTrue(photo.exists())
        self.assertGreater(result["freed_bytes"], 0)

    def test_parent_segment_rejected(self):
        cache = self.root / "cache"
        cache.mkdir()
        secret = self.root / "secret.txt"
        secret.write_text("keep", encoding="utf-8")
        result = clean_cache_ids(["evil"], rules=[_rule("evil", "dir", [str(cache / "..")])])
        self.assertFalse(result["ok"])
        self.assertTrue(secret.exists())

    def test_delete_inside_dotdot_raises(self):
        cache = self.root / "cache"
        cache.mkdir()
        secret = self.root / "secret.txt"
        secret.write_text("keep", encoding="utf-8")
        with patch("app.caches.os.unlink", side_effect=AssertionError("unlinked")):
            with self.assertRaises(UnsafePathError):
                delete_inside(cache / "..", [cache])
        self.assertTrue(secret.exists())

    def test_windows_directory_is_not_listed_or_deleted(self):
        with patch("app.caches.os.scandir", side_effect=AssertionError("listed")), \
             patch("app.caches.os.unlink", side_effect=AssertionError("unlinked")):
            with self.assertRaises(UnsafePathError):
                delete_inside(Path(r"C:\Windows"), [Path(r"C:\Windows")])

    def test_shallow_path_rejected(self):
        with self.assertRaises(UnsafePathError):
            assert_deletable(Path(r"C:\Temp"), [Path(r"C:\Temp")], mode="dir")

    def test_glob_does_not_touch_profile_files(self):
        profile = self.root / "User Data" / "Default"
        cache = profile / "Cache"
        cache.mkdir(parents=True)
        (cache / "a.bin").write_bytes(b"abc")
        login = profile / "Login Data"
        login.write_text("secret", encoding="utf-8")
        outside = self.root / "secret.txt"
        outside.write_text("no", encoding="utf-8")
        pattern = str(self.root / "User Data" / "*" / "Cache")
        result = clean_cache_ids(["chrome_box"], rules=[_rule("chrome_box", "glob_dir", [pattern])])
        self.assertTrue(result["ok"], result)
        self.assertFalse((cache / "a.bin").exists())
        self.assertTrue(cache.exists())
        self.assertEqual(login.read_text(encoding="utf-8"), "secret")
        self.assertTrue(outside.exists())

    def test_file_rule_keeps_neighbors(self):
        folder = self.root / "Explorer"
        folder.mkdir()
        (folder / "thumbcache_32.db").write_text("t", encoding="utf-8")
        (folder / "important.db").write_text("keep", encoding="utf-8")
        rules = [_rule("thumbs", "files", [str(folder)], names=["thumbcache_*.db"])]
        result = clean_cache_ids(["thumbs"], rules=rules)
        self.assertTrue(result["ok"], result)
        self.assertFalse((folder / "thumbcache_32.db").exists())
        self.assertEqual((folder / "important.db").read_text(encoding="utf-8"), "keep")

    def test_two_wildcards_rejected(self):
        pattern = str(self.root / "a" / "*" / "*" / "Cache")
        result = clean_cache_ids(["badglob"], rules=[_rule("badglob", "glob_dir", [pattern])])
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
