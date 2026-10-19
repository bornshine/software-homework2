"""意图识别：规则优先 → 模型兜底 → 最后退回 code。"""

import unittest
from unittest.mock import patch

import config
from multi_agent.intent import (
    MODE_CHAT,
    MODE_CODE,
    classify_intent,
    llm_classify,
    rule_classify,
)
from tests.helpers import BoomModel, ScriptedModel


class TestRuleClassify(unittest.TestCase):
    def test_capability_question_is_chat(self):
        self.assertEqual(rule_classify("你能做什么"), MODE_CHAT)

    def test_self_intro_is_chat(self):
        self.assertEqual(rule_classify("你是谁？"), MODE_CHAT)

    def test_what_is_question_is_chat(self):
        self.assertEqual(rule_classify("什么是回文？"), MODE_CHAT)

    def test_code_task_is_code(self):
        self.assertEqual(rule_classify("写一个函数 median(values)"), MODE_CODE)

    def test_code_flavoured_question_is_still_code(self):
        # “怎么写快排”既像提问又要代码：代码信号优先
        self.assertEqual(rule_classify("怎么写一个快排函数"), MODE_CODE)

    def test_ambiguous_returns_none(self):
        self.assertIsNone(rule_classify("把这个流程讲一下"))

    def test_empty_is_chat(self):
        self.assertEqual(rule_classify("   "), MODE_CHAT)


class TestLlmClassify(unittest.TestCase):
    def test_accepts_code(self):
        self.assertEqual(llm_classify("随便", ScriptedModel(["code"])), MODE_CODE)

    def test_accepts_uppercase_and_padding(self):
        self.assertEqual(llm_classify("随便", ScriptedModel(["  CHAT  "])), MODE_CHAT)

    def test_rejects_unexpected_output(self):
        self.assertIsNone(llm_classify("随便", ScriptedModel(["我不确定"])))

    @patch.object(config, "NODE_RETRY_BASE_DELAY", 0.0)
    def test_returns_none_when_model_down(self):
        self.assertIsNone(llm_classify("随便", BoomModel("挂了")))


class TestClassifyIntent(unittest.TestCase):
    def test_rules_decide_without_calling_llm(self):
        model = BoomModel("不该被调用")
        self.assertEqual(classify_intent("你能做什么", model), MODE_CHAT)
        self.assertEqual(model.calls, 0)

    def test_model_decides_when_rules_unsure(self):
        self.assertEqual(
            classify_intent("把这个流程讲一下", ScriptedModel(["chat"])), MODE_CHAT
        )

    @patch.object(config, "NODE_RETRY_BASE_DELAY", 0.0)
    def test_falls_back_to_code_when_model_fails(self):
        self.assertEqual(classify_intent("把这个流程讲一下", BoomModel()), MODE_CODE)

    def test_no_llm_falls_back_to_code(self):
        self.assertEqual(classify_intent("把这个流程讲一下"), MODE_CODE)


if __name__ == "__main__":
    unittest.main()
