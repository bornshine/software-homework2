"""LLM 工厂：确认重试策略只在节点层实现（避免两层重试相乘）。"""

import unittest

import config
from multi_agent.llm_factory import _build


class TestBuild(unittest.TestCase):
    def test_sdk_retries_disabled_by_default(self):
        model = _build("test-model", "test-key", None)
        self.assertEqual(model.max_retries, config.LLM_HTTP_MAX_RETRIES)
        self.assertEqual(config.LLM_HTTP_MAX_RETRIES, 0)

    def test_timeout_comes_from_config(self):
        model = _build("test-model", "test-key", None)
        self.assertEqual(float(model.request_timeout), config.LLM_REQUEST_TIMEOUT)

    def test_node_level_attempts_configured(self):
        # 节点层负责重试，默认 3 次
        self.assertGreaterEqual(config.NODE_MAX_ATTEMPTS, 1)


if __name__ == "__main__":
    unittest.main()
