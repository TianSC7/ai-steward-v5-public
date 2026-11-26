# -*- coding: utf-8 -*-
"""V4.1：声明目标里的 + / 逗号必须拆成真实路径。"""
import unittest

import factory_runner as FR


class ExpandTargets(unittest.TestCase):
    def test_plus_string_splits_to_real_paths(self):
        job = {
            "kind": "code",
            "target": "scripts/v3_data.py+dashboard/v2.html+lines/registry.json",
            "targets": ["scripts/v3_data.py+dashboard/v2.html+lines/registry.json"],
        }
        self.assertEqual(
            FR.resolve_job_targets(job),
            ["scripts/v3_data.py", "dashboard/v2.html", "lines/registry.json"],
        )

    def test_comma_plus_backslash_and_dupes(self):
        job = {
            "kind": "code",
            "targets": [r"scripts\a.py+scripts/a.py", "scripts/b.py, scripts/c.py"],
        }
        self.assertEqual(
            FR.resolve_job_targets(job),
            ["scripts/a.py", "scripts/b.py", "scripts/c.py"],
        )


if __name__ == "__main__":
    unittest.main()
