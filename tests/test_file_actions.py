import os
import tempfile
import unittest
from pathlib import Path

from app.actions import (
    command_is_launchable,
    delete_listed_files,
    file_block_reason,
    split_uninstall,
)
from app.service import Analyzer
from app.winpath import norm_key


class FileGuardTests(unittest.TestCase):
    def test_refuses_path_that_is_not_in_the_scan(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "keep.txt"
            target.write_text("stay", encoding="utf-8")
            result = delete_listed_files([str(target)], set())
            self.assertFalse(result["deleted"])
            self.assertTrue(target.exists())
            self.assertEqual(result["rejected"][0]["reason"], "不在本次扫描结果里")

    def test_refuses_windows_even_when_allowlisted(self):
        path = r"C:\Windows\System32\notepad.exe"
        self.assertTrue(os.path.isfile(path))
        before = os.path.getsize(path)
        result = delete_listed_files([path], {norm_key(path)})
        self.assertFalse(result["deleted"])
        self.assertEqual(os.path.getsize(path), before)
        self.assertIn("Windows", result["rejected"][0]["reason"])

    def test_refuses_pagefile(self):
        reason = file_block_reason(r"C:\pagefile.sys")
        self.assertIn("系统文件", reason)

    def test_deletes_only_the_allowed_file(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "gone.txt"
            sibling = Path(folder) / "stay.txt"
            target.write_text("bye", encoding="utf-8")
            sibling.write_text("stay", encoding="utf-8")
            result = delete_listed_files([str(target), str(sibling)], {norm_key(target)})
            self.assertFalse(target.exists())
            self.assertTrue(sibling.exists())
            self.assertEqual(len(result["deleted"]), 1)

    def test_uninstall_command_keeps_original_arguments(self):
        args = split_uninstall(r'"C:\Program Files\App\uninstall.exe" /S')
        self.assertEqual(args[0], r"C:\Program Files\App\uninstall.exe")
        self.assertEqual(args[1:], ["/S"])
        self.assertIn("不存在", command_is_launchable(args))
        msiexec = split_uninstall(r"MsiExec.exe /I{ABC}")
        self.assertEqual(msiexec[0], "MsiExec.exe")
        self.assertEqual(command_is_launchable(msiexec), "")


class SnapshotActionTests(unittest.TestCase):
    def test_preview_rejects_a_path_outside_the_lists(self):
        analyzer = Analyzer.__new__(Analyzer)
        analyzer._snap = {"top_files": [], "recent_files": {"30": []}, "software": []}
        analyzer._lock = __import__("threading").Lock()
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "nope.txt"
            target.write_text("x", encoding="utf-8")
            preview = analyzer.preview_files([str(target)])
            self.assertFalse(preview["ok"])
            self.assertTrue(target.exists())

    def test_uninstall_preview_does_not_launch(self):
        analyzer = Analyzer.__new__(Analyzer)
        analyzer._snap = {
            "software": [
                {
                    "id": "abc",
                    "name": "样例软件",
                    "install_location": r"D:\apps\sample",
                    "uninstall_string": r'"D:\apps\sample\uninstall.exe"',
                }
            ]
        }
        analyzer._lock = __import__("threading").Lock()
        missing = analyzer.preview_uninstall([{"name": "别的软件", "install_location": ""}])
        self.assertFalse(missing["ok"])
        found = analyzer.preview_uninstall([{"id": "abc", "name": "样例软件", "install_location": r"D:\apps\sample"}])
        self.assertTrue(found["ok"])
        self.assertEqual(found["items"][0]["name"], "样例软件")


if __name__ == "__main__":
    unittest.main()
