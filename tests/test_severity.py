"""严重级别判定：通过 = 无阻断问题，不等于零建议。"""

import unittest
from unittest.mock import patch

import config
from multi_agent.severity import (
    blocking_findings,
    is_blocking,
    normalize_severity,
    suggestions,
    summarize,
)


class TestNormalizeSeverity(unittest.TestCase):
    def test_known_levels(self):
        self.assertEqual(normalize_severity("CRITICAL"), "critical")
        self.assertEqual(normalize_severity("  warning "), "warning")
        self.assertEqual(normalize_severity("info"), "info")

    def test_unknown_becomes_warning(self):
        """未知级别当阻断处理：宁多多修一轮，也不静默放过可能严重的问题。"""
        self.assertEqual(normalize_severity("high"), "warning")
        self.assertEqual(normalize_severity("严重"), "warning")
        self.assertEqual(normalize_severity(None), "warning")
        self.assertEqual(normalize_severity(""), "warning")


class TestBlocking(unittest.TestCase):
    def test_default_threshold(self):
        self.assertTrue(is_blocking({"severity": "critical"}))
        self.assertTrue(is_blocking({"severity": "warning"}))
        self.assertFalse(is_blocking({"severity": "info"}))

    def test_missing_severity_is_blocking(self):
        self.assertTrue(is_blocking({}))

    def test_split_and_summarize(self):
        findings = [
            {"severity": "critical"},
            {"severity": "info"},
            {"severity": "warning"},
            {"severity": "info"},
        ]
        self.assertEqual(len(blocking_findings(findings)), 2)
        self.assertEqual(len(suggestions(findings)), 2)

        snapshot = summarize(findings, 1)
        self.assertEqual(snapshot["total"], 4)
        self.assertEqual(snapshot["blocking"], 2)
        self.assertEqual(snapshot["critical"], 1)
        self.assertEqual(snapshot["warning"], 1)
        self.assertEqual(snapshot["info"], 2)
        self.assertEqual(snapshot["iteration"], 1)

    def test_none_and_empty(self):
        self.assertEqual(blocking_findings(None), [])
        self.assertEqual(suggestions(None), [])
        self.assertEqual(summarize(None)["total"], 0)


class TestThresholdConfig(unittest.TestCase):
    @patch.object(config, "BLOCKING_SEVERITIES", frozenset({"critical"}))
    def test_threshold_is_configurable(self):
        # 只把 critical 当阻断时，warning 也不阻断
        self.assertFalse(is_blocking({"severity": "warning"}))
        self.assertTrue(is_blocking({"severity": "critical"}))


if __name__ == "__main__":
    unittest.main()
