import json
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401
from lonoboard import config, network, runtime, sources
from lonoboard.collector import parse_gin, parse_timing, platform_label


class TestTimingLogs(unittest.TestCase):
    LINES = [
        "1792203821.896949 h run[1]: 396.22.768.011 I slot print_timing: id  0 | task 13 |        eval time =   43569.86 ms /  3000 tokens (   14.53 ms per token,    68.83 tokens per second)",
        "1792203821.896949 h run[1]: 396.22.768.006 I slot print_timing: id  0 | task 13 | prompt eval time =     451.38 ms /    24 tokens (   18.81 ms per token,    53.17 tokens per second)",
        "1792203900.100000 h run[1]: 400.00.000.000 I slot print_timing: id  1 | task 14 | prompt eval time =     10.00 ms /    5 tokens (   2.00 ms per token,    500.00 tokens per second)",
    ]

    def test_pairs_prompt_and_eval_by_task_in_either_order(self):
        reqs = parse_timing(self.LINES)
        self.assertEqual(len(reqs), 2)
        self.assertEqual(reqs[0], {"at": 1792203821, "tokens": 3000, "gen_tps": 68.83, "prompt_tokens": 24, "prompt_tps": 53.17})
        self.assertEqual(reqs[1]["tokens"], None)          # embeddings: prompt only
        self.assertEqual(reqs[1]["prompt_tps"], 500.0)

    def test_ollama_gin_lines(self):
        lines = ['Oct 17 00:12:30 h ollama[9]: [GIN] 2026/10/17 - 00:12:30 | 200 |  2.512345s |  127.0.0.1 | POST     "/api/chat"',
                 'Oct 17 00:12:31 h ollama[9]: [GIN] 2026/10/17 - 00:12:31 | 200 |     512µs |  127.0.0.1 | GET      "/api/tags"',
                 'Oct 17 00:12:33 h ollama[9]: [GIN] 2026/10/17 - 00:12:33 | 200 |  850.5ms |  127.0.0.1 | POST     "/v1/chat/completions"']
        got = parse_gin(lines)
        self.assertEqual([p for _, p, _ in got], ["/api/chat", "/v1/chat/completions"])
        self.assertAlmostEqual(got[1][2], 0.8505, places=3)


class TestMetrics(unittest.TestCase):
    def test_sums_label_series(self):
        m = sources.parse_metrics(helpers.VLLM["GET /metrics"][1])
        self.assertEqual(m["vllm:num_requests_running"], 3.0)
        self.assertEqual(m["vllm:request_success_total"], 5.0)


class TestSystemParsers(unittest.TestCase):
    def test_cgroup_user_unit(self):
        unit, user, container = runtime.parse_cgroup(
            "0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-llama.slice/llama@qwen.service")
        self.assertEqual((unit, user, container), ("llama@qwen.service", True, None))

    def test_cgroup_system_unit_and_docker(self):
        self.assertEqual(runtime.parse_cgroup("0::/system.slice/ollama.service")[:2], ("ollama.service", False))
        self.assertEqual(runtime.parse_cgroup("0::/system.slice/docker-5f2c1e9a7b3d0011.scope")[2], "5f2c1e9a7b3d")

    def test_tailscale_serve_status(self):
        serve = json.loads("""{"TCP": {"23": {"TCPForward": "127.0.0.1:2323"}, "443": {"HTTPS": true}, "7777": {"HTTP": true}},
          "Web": {"m.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:11000"}}},
                  "m.ts.net:7777": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:7777"}}}}}""")
        got = network.parse_serve(serve, "m")
        self.assertEqual(got[11000], ["https://m.ts.net"])
        self.assertEqual(got[7777], ["http://m:7777", "http://m.ts.net:7777"])
        self.assertEqual(got[2323], ["m:23"])

    def test_platform_label(self):
        self.assertEqual(platform_label({"vendor": "nvidia", "name": "NVIDIA GeForce RTX 3090", "count": 2}, None), "2x rtx 3090")
        self.assertEqual(platform_label({"vendor": "nvidia", "name": "NVIDIA GB10", "count": 1}, None), "nvidia gb10")
        self.assertEqual(platform_label(None, "Intel(R) Core(TM) i7"), "intel core i7")

    def test_params_from_ollama(self):
        self.assertEqual(sources._params_b("8.0B"), 8.0)
        self.assertEqual(sources._params_b("137M"), 0.14)


class TestConfig(unittest.TestCase):
    def write(self, text):
        f = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        f.write(text)
        f.close()
        return f.name

    def test_defaults_without_a_file(self):
        cfg = config.load("/nonexistent/lonoboard.toml")
        self.assertEqual(cfg["board"]["port"], 7777)
        self.assertIsNone(cfg["_path"])

    def test_file_overrides_and_labels(self):
        cfg = config.load(self.write('[board]\nport = 9000\n[labels]\n11000 = "DGX Dashboard"\n'
                                     '[[server]]\nurl = "http://x:1"\nkind = "ollama"\n'))
        self.assertEqual(cfg["board"]["port"], 9000)
        self.assertEqual(cfg["labels"], {11000: "DGX Dashboard"})
        self.assertEqual(cfg["server"][0]["kind"], "ollama")

    def test_bad_kind_is_an_error(self):
        with self.assertRaises(config.ConfigError):
            config.load(self.write('[[server]]\nurl = "http://x:1"\nkind = "gpt"\n'))

    def test_exposure(self):
        self.assertEqual(config.exposure("127.0.0.1"), "local")
        self.assertEqual(config.exposure("100.64.0.7"), "tailnet")
        self.assertEqual(config.exposure("0.0.0.0"), "network")

    def test_network_without_password_refused(self):
        cfg = helpers.config(listen="0.0.0.0")
        with self.assertRaises(config.ConfigError):
            config.check_exposure(cfg)
        cfg["board"]["password"] = "s3cret"
        config.check_exposure(cfg)

    def test_password_from_env(self):
        import os
        os.environ["LONOBOARD_PASSWORD"] = "from-env"
        try:
            self.assertEqual(config.load("/nonexistent")["board"]["password"], "from-env")
        finally:
            del os.environ["LONOBOARD_PASSWORD"]


class TestLlamaModels(unittest.TestCase):
    def test_reads_conf_files(self):
        d = Path(tempfile.mkdtemp())
        (d / "qwen.conf").write_text("# Qwen: chat. POST /v1/chat/completions\nPORT=30000\nARGS=(\n  -m /m/q.gguf\n  --alias q\n)\n")
        (d / "tts.conf").write_text("# TTS server\nPORT=30006\nCMD=(python server.py --port \"$PORT\")\n")
        targets = {t["name"]: t for t in sources.read_llama_models(d, "llama@{name}.service")}
        self.assertEqual(targets["qwen"]["url"], "http://127.0.0.1:30000")
        self.assertEqual(targets["qwen"]["args"], ["-m", "/m/q.gguf", "--alias", "q"])
        self.assertEqual(targets["qwen"]["unit"], "llama@qwen.service")
        self.assertEqual(targets["tts"]["kind"], "custom")
        self.assertEqual(targets["tts"]["command"], ["python", "server.py", "--port", "30006"])


if __name__ == "__main__":
    unittest.main()
