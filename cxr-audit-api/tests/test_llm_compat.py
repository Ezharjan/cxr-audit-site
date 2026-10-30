"""Structured-output compatibility for small local models (fake clients, no network)."""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))
sys.path.insert(0, str(API_ROOT / "cxr_audit"))

import prompts
from helpers import apply_thinking_preference, extract_json_object, getLLMJSON

LUNIT_FALSE = {k: False for k in prompts.LunitFindings.model_fields}


def reply(content=None, parsed=None):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, parsed=parsed))])


class FakeCompletions:
    def __init__(self, parse_result=None, parse_error=None, contents=(), with_parse=True):
        self.calls = []
        self._parse_result, self._parse_error, self._contents = parse_result, parse_error, list(contents)
        if with_parse:
            self.parse = self._parse

    def _parse(self, **kwargs):
        self.calls.append(("parse", kwargs))
        if self._parse_error:
            raise self._parse_error
        return reply(parsed=self._parse_result)

    def create(self, **kwargs):
        self.calls.append(("create", kwargs))
        return reply(content=self._contents.pop(0))


def client(**kwargs):
    completions = FakeCompletions(**kwargs)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions)), completions


MESSAGES = [{"role": "system", "content": "sys"}, {"role": "user", "content": "SYNTHETIC report"}]


class GetLLMJSONTests(unittest.TestCase):
    def test_current_sdk_parse_method(self):
        c, calls = client(parse_result=prompts.cxrGrade(grade=3))
        self.assertEqual(getLLMJSON(MESSAGES, c, "qwen3:4b", prompts.cxrGrade), {"grade": 3})
        self.assertEqual([name for name, _ in calls.calls], ["parse"])
        self.assertIs(calls.calls[0][1]["response_format"], prompts.cxrGrade)

    def test_legacy_beta_parse_method(self):
        legacy = FakeCompletions(parse_result=prompts.cxrGrade(grade=2))
        c = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions(with_parse=False)),
                            beta=SimpleNamespace(chat=SimpleNamespace(completions=legacy)))
        self.assertEqual(getLLMJSON(MESSAGES, c, "m", prompts.cxrGrade), {"grade": 2})

    def test_fallback_strips_reasoning_and_fences(self):
        content = '<think>maybe {"grade": 6}?</think>\nAnswer:\n```json\n{"grade": 4}\n```'
        c, calls = client(parse_error=RuntimeError("unsupported"), contents=[content])
        self.assertEqual(getLLMJSON(MESSAGES, c, "m", prompts.cxrGrade), {"grade": 4})
        self.assertEqual([name for name, _ in calls.calls], ["parse", "create"])

    def test_fallback_rejects_wrong_types_and_uses_one_repair(self):
        c, calls = client(parse_error=RuntimeError(), contents=['{"grade": "3"}', '{"grade": 5}'])
        self.assertEqual(getLLMJSON(MESSAGES, c, "m", prompts.cxrGrade), {"grade": 5})
        self.assertEqual([name for name, _ in calls.calls], ["parse", "create", "create"])

    def test_invalid_after_repair_raises(self):
        for bad in ('{"grade": 9}', "no json here", "<think>unterminated {\"grade\": 3}"):
            with self.subTest(bad=bad):
                c, _ = client(parse_error=RuntimeError(), contents=[bad, bad])
                with self.assertRaises(ValueError):
                    getLLMJSON(MESSAGES, c, "m", prompts.cxrGrade)

    def test_strict_booleans_for_supplemental_findings(self):
        c, _ = client(parse_error=RuntimeError(), contents=[
            '{' + ", ".join(f'"{k}": "false"' for k in LUNIT_FALSE) + '}',
            '{' + ", ".join(f'"{k}": false' for k in LUNIT_FALSE) + '}',
        ])
        self.assertEqual(getLLMJSON(MESSAGES, c, "m", prompts.LunitFindings), LUNIT_FALSE)

    def test_parsed_none_falls_back(self):
        c, _ = client(parse_result=None, contents=['{"grade": 1}'])
        self.assertEqual(getLLMJSON(MESSAGES, c, "m", prompts.cxrGrade), {"grade": 1})


class ThinkingPreferenceTests(unittest.TestCase):
    def test_disabled_by_default(self):
        with patch.dict(os.environ, {"LLM_DISABLE_THINKING": ""}):
            self.assertIs(apply_thinking_preference(MESSAGES), MESSAGES)

    def test_no_think_appended_without_mutation(self):
        with patch.dict(os.environ, {"LLM_DISABLE_THINKING": "True"}):
            result = apply_thinking_preference(MESSAGES)
            self.assertTrue(result[-1]["content"].endswith("\n/no_think"))
            self.assertEqual(MESSAGES[-1]["content"], "SYNTHETIC report")
            self.assertEqual(apply_thinking_preference(result)[-1]["content"].count("/no_think"), 1)
            c, calls = client(parse_result=prompts.cxrGrade(grade=1))
            getLLMJSON(MESSAGES, c, "m", prompts.cxrGrade)
            self.assertTrue(calls.calls[0][1]["messages"][-1]["content"].endswith("/no_think"))


class ExtractTests(unittest.TestCase):
    def test_extract(self):
        self.assertEqual(extract_json_object('prefix {"a": 1} suffix'), {"a": 1})
        self.assertEqual(extract_json_object('<THINK>x</THINK>{"a": 2}'), {"a": 2})
        for bad in (None, "", "[1, 2]", "<think>{\"a\": 1}"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                extract_json_object(bad)


if __name__ == "__main__":
    unittest.main()
