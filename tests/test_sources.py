import unittest

import helpers
from lonoboard import sources
from lonoboard.util import stream_chat


class TestSources(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.llama = helpers.FakeServer(helpers.LLAMACPP)
        cls.ollama = helpers.FakeServer(helpers.OLLAMA)
        cls.vllm = helpers.FakeServer(helpers.VLLM)
        cls.openai = helpers.FakeServer(helpers.OPENAI)

    @classmethod
    def tearDownClass(cls):
        for s in (cls.llama, cls.ollama, cls.vllm, cls.openai):
            s.close()

    def test_identify(self):
        self.assertEqual(sources.identify(self.llama.url), "llama.cpp")
        self.assertEqual(sources.identify(self.ollama.url), "ollama")
        self.assertEqual(sources.identify(self.vllm.url), "vllm")
        self.assertEqual(sources.identify(self.openai.url), "openai")
        self.assertIsNone(sources.identify("http://127.0.0.1:9"))

    def test_llamacpp(self):
        s = sources.describe({"name": "q", "kind": "llama.cpp", "url": self.llama.url})
        self.assertEqual(s["health"], "ok")
        self.assertEqual(s["version"], "llama.cpp abc1234")
        m = s["model"]
        self.assertEqual((m["id"], m["context"], m["slots"], m["slots_busy"], m["params_b"]), ("qwen3-8b", 32768, 4, 1, 8.19))
        self.assertEqual(s["chat"]["models"], ["qwen3-8b"])

    def test_llamacpp_embeddings_flag_means_no_chat(self):
        s = sources.describe({"name": "e", "kind": "llama.cpp", "url": self.llama.url, "args": ["-m", "e.gguf", "--embeddings"]})
        self.assertIn("/v1/embeddings", s["endpoints"])
        self.assertIsNone(s["chat"])

    def test_ollama(self):
        s = sources.describe({"name": "ollama", "kind": "ollama", "url": self.ollama.url})
        self.assertEqual(s["version"], "ollama 0.12.6")
        self.assertEqual([m["id"] for m in s["models"]], ["llama3.1:8b", "nomic-embed-text:latest"])
        self.assertTrue(s["models"][0]["loaded"])
        self.assertEqual(s["model"]["loaded_count"], 1)
        self.assertEqual(s["memory_mib"]["gpu"], 6654289920 // 2**20)

    def test_vllm_busy_from_metrics(self):
        s = sources.describe({"name": "v", "kind": "vllm", "url": self.vllm.url})
        self.assertEqual(s["version"], "vllm 0.10.2")
        self.assertEqual(s["model"]["slots_busy"], 3)
        self.assertEqual(s["model"]["context"], 32768)

    def test_unreachable(self):
        s = sources.describe({"name": "x", "kind": "llama.cpp", "url": "http://127.0.0.1:9"})
        self.assertEqual(s["health"], "unreachable")
        self.assertIsNone(s["chat"])

    def test_api_key_header(self):
        import os
        os.environ["LONOBOARD_TEST_KEY"] = "sk-test"
        try:
            sources.describe({"name": "o", "kind": "openai", "url": self.openai.url, "api_key_env": "LONOBOARD_TEST_KEY"})
            self.assertEqual(self.openai.requests[-1][3].get("Authorization"), "Bearer sk-test")
        finally:
            del os.environ["LONOBOARD_TEST_KEY"]

    def test_stream_chat(self):
        events = list(stream_chat(self.llama.url, "qwen3-8b", [{"role": "user", "content": "hi"}]))
        self.assertEqual([k for k, _ in events], ["reasoning", "content", "content", "stats"])
        self.assertEqual("".join(v for k, v in events if k == "content"), "Hello there")
        self.assertEqual(events[-1][1], (2, 42.0))

    def test_stream_chat_error(self):
        events = list(stream_chat("http://127.0.0.1:9", "m", []))
        self.assertEqual(events[-1][0], "error")


if __name__ == "__main__":
    unittest.main()
