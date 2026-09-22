# -*- coding: utf-8 -*-
"""Rotation tests for generate_cn_forums.rotate_fresh — 已推送跳过、名次顺延、递补保底。"""
import sys
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "scripts"))

from generate_cn_forums import rotate_fresh, BACKFILL_FLOOR


def make_item(source, tid, replies=10, views=1000):
    return {
        "id": f"{source}:{tid}",
        "source": source,
        "source_name": source,
        "board": source,
        "title": f"topic {tid}",
        "url": f"https://example.com/{source}/{tid}",
        "published": "2026-09-22T10:00:00+08:00",
        "summary": "",
        "replies": replies,
        "views": views,
        "theme": "value",
    }


class RotateFreshTests(unittest.TestCase):
    def test_delivered_ids_are_skipped_and_rank_promoted(self):
        """已推送的剔除，下一位候选顶上——保证每天有新内容。"""
        items = [make_item("xueqiu", i) for i in range(1, 6)]  # 5 条候选
        delivered = {f"xueqiu:{i}" for i in (1, 2, 3)}          # 前 3 条已推送
        fresh, stats = rotate_fresh(items, {}, delivered, max_per_board=15)
        got = [it["id"] for it in fresh]
        self.assertEqual(got, ["xueqiu:4", "xueqiu:5"])
        self.assertEqual(stats["skipped_delivered"], 3)
        self.assertEqual(stats["backfilled"], 0)

    def test_backfill_kicks_in_below_floor(self):
        """板块新鲜量不足保底线时，递补池按序补齐（不为凑满配额硬凑）。"""
        items = [make_item("xueqiu", 1)]  # 仅 1 条新鲜主池候选
        backfill = {"xueqiu": [make_item("xueqiu", i, replies=1, views=80) for i in (2, 3, 4)]}
        fresh, stats = rotate_fresh(items, backfill, set(), max_per_board=15)
        got = [it["id"] for it in fresh]
        # 1 条主池 + 递补到 floor=5：还需 4 条，但递补池只有 3 条全上
        self.assertEqual(got, ["xueqiu:1", "xueqiu:2", "xueqiu:3", "xueqiu:4"])
        self.assertEqual(stats["backfilled"], 3)

    def test_backfill_not_used_when_floor_met(self):
        """新鲜量已达保底线时不启用递补，避免低互动噪音挤进来。"""
        items = [make_item("nga", i) for i in range(1, 6)]  # 恰好 5 条 = floor
        backfill = {"nga": [make_item("nga", 99, replies=1)]}
        fresh, stats = rotate_fresh(items, backfill, set(), max_per_board=15)
        self.assertEqual(len(fresh), 5)
        self.assertEqual(stats["backfilled"], 0)

    def test_max_per_board_cap(self):
        """新鲜候选超过每板上限时截断，防 payload 膨胀。"""
        items = [make_item("hupu", i) for i in range(1, 21)]  # 20 条
        fresh, stats = rotate_fresh(items, {}, set(), max_per_board=15)
        self.assertEqual(len(fresh), 15)
        self.assertEqual([it["id"] for it in fresh][:2], ["hupu:1", "hupu:2"])

    def test_backfill_pool_delivered_items_excluded(self):
        """递补池里的已推送项同样剔除，不因门槛放宽而重复出现。"""
        items = [make_item("xueqiu", 1)]
        backfill = {"xueqiu": [make_item("xueqiu", 2, replies=1), make_item("xueqiu", 3, replies=1)]}
        delivered = {"xueqiu:2"}
        fresh, stats = rotate_fresh(items, backfill, delivered, max_per_board=15)
        got = [it["id"] for it in fresh]
        self.assertEqual(got, ["xueqiu:1", "xueqiu:3"])
        self.assertEqual(stats["backfilled"], 1)

    def test_empty_input_is_safe(self):
        """全部已推送且无递补时输出为空、不报错（诚实空态）。"""
        items = [make_item("jisilu", 1)]
        fresh, stats = rotate_fresh(items, {}, {"jisilu:1"}, max_per_board=15)
        self.assertEqual(fresh, [])
        self.assertEqual(stats["skipped_delivered"], 1)

    def test_floor_constant_sane(self):
        """保底线是 1..15 之间的合理值。"""
        self.assertGreaterEqual(BACKFILL_FLOOR, 1)
        self.assertLessEqual(BACKFILL_FLOOR, 15)


if __name__ == "__main__":
    unittest.main()
