#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""C-20261126-SPD1（提速方案 P1-4）：派工单「少往返工作法」回归测试。

覆盖两件事：
  1. 模块级常量 BUILD_METHOD_NOTE 存在，含 1–5 五条编号条目与「以做对为准」收尾句；
  2. execute_job 的 work_brief 拼装段里拼入了该常量，且顺序为 {map_md} → BUILD_METHOD_NOTE → ## 规格正本（只拼一处）。

用法：python -X utf8 scripts/test_build_method_note.py
"""
from __future__ import annotations

import inspect
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import factory_runner  # noqa: E402


class BuildMethodNoteConstant(unittest.TestCase):
    def test_constant_exists_and_is_text(self):
        note = getattr(factory_runner, "BUILD_METHOD_NOTE", None)
        self.assertIsInstance(note, str, "factory_runner.BUILD_METHOD_NOTE 必须是模块级字符串常量")
        self.assertTrue(note.strip(), "BUILD_METHOD_NOTE 不得为空")
        self.assertIn("少往返工作法", note)

    def test_five_numbered_items(self):
        note = factory_runner.BUILD_METHOD_NOTE
        items = re.findall(r"(?m)^([1-5])\. \*\*", note)
        self.assertEqual(items, ["1", "2", "3", "4", "5"],
                         "BUILD_METHOD_NOTE 必须含 1–5 五条编号条目（形如 `1. **…**`）")

    def test_accuracy_closing_sentence(self):
        self.assertIn("以做对为准", factory_runner.BUILD_METHOD_NOTE,
                      "BUILD_METHOD_NOTE 必须保留「以做对为准」收尾句")


class WorkBriefInjection(unittest.TestCase):
    def _brief_segment(self):
        src = inspect.getsource(factory_runner.execute_job)
        start = src.index("work_brief = (")
        end = src.index("atomic_write_text(brief_path", start)
        return src[start:end]

    def test_note_injected_once(self):
        seg = self._brief_segment()
        self.assertEqual(seg.count("BUILD_METHOD_NOTE"), 1,
                         "work_brief 拼装段只允许拼入 BUILD_METHOD_NOTE 一处")

    def test_order_map_note_spec(self):
        seg = self._brief_segment()
        i_map = seg.index("{map_md}")
        i_note = seg.index("BUILD_METHOD_NOTE")
        i_spec = seg.index("## 规格正本")
        self.assertLess(i_map, i_note, "BUILD_METHOD_NOTE 必须排在 {map_md} 之后")
        self.assertLess(i_note, i_spec, "BUILD_METHOD_NOTE 必须排在 ## 规格正本 之前")


if __name__ == "__main__":
    unittest.main()
