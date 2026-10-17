# Model servers

lonoboard builds its list of model servers from three places:

1. `[[server]]` entries in the config.
2. The `[llama_models]` adapter, when its directory exists.
3. Auto-detection.

Duplicates on the same port are merged, and earlier sources win. The list is refreshed every
30 seconds, so new servers appear without a restart.

## Detection

For every listening TCP port, lonoboard reads the process name and command line. Processes it
recognises:

- `llama-server`
- `ollama`
- `vllm`
- LM Studio (`lms`)
- tabbyAPI
- koboldcpp
- sglang

Recognised processes, and anything on a port in `[detect] ports`, are then probed in this
order:

| check | means |
|-------|-------|
| `/api/version` and `/api/tags` answer | Ollama |
| `/props` answers | llama.cpp |
| `/metrics` contains `vllm:` | vLLM |
| `/v1/models` answers | OpenAI-compatible |

Processes owned by other users don't show their names. lonoboard still tries the well-known ports.

## What each kind shows

**llama.cpp**
- Model alias and file, parameter count, quant, context (and training context), modalities.
- Slots and busy slots, from `/slots`.
- A llama-server started with `--embeddings` or `--reranking` is shown, but not offered for
  chat.

**Ollama**
- Installed models with size and quant, and which are loaded.
- VRAM used by loaded models.
- Version.

**vLLM**
- Served model and max context.
- Running requests and token counters, from `/metrics`.
- Version.

**OpenAI-compatible**
- The models listed by `/v1/models`.
- Health and latency.
- Chat, if a model looks like a chat model.

**custom**
- Only `/health`.

## Runtime: what's running it

lonoboard works out how each server is run from its listening process:

- **systemd.** Read from the process's cgroup, for both user and system units. lonoboard shows the
  unit's state, enabled flag, uptime, restarts and pid, and tails `journalctl` for its log.
- **Docker.** The container that publishes the port, or whose processes own it. lonoboard shows the
  container state, image, uptime and restarts, and tails `docker logs`. Your user needs access
  to the Docker socket.
- **A plain process.** Uptime and memory from `/proc`, or `ps` on macOS.

GPU memory per process comes from `nvidia-smi` where it can be attributed.

## Usage: requests and tokens

Requests per hour and the top 10 come from the best signal each server gives:

| server | source | detail |
|--------|--------|--------|
| llama.cpp under systemd | the `eval time` lines in its journal | every request, with prompt and generated tokens and speeds |
| llama.cpp with `--metrics` | `llamacpp:*` counters | requests and tokens, no per-request detail |
| llama.cpp, otherwise | slot task ids changing | a request count only |
| Ollama | the GIN request lines in its journal | requests and durations |
| vLLM | `vllm:*` counters | requests and tokens |

History is kept for 48 hours in `~/.local/state/lonoboard/usage.json`. It survives restarts, and on
first start lonoboard reads back the last 24 hours of logs.

## Hardware

| vendor | source | notes |
|--------|--------|-------|
| NVIDIA | `nvidia-smi` | utilisation, temperature, power, memory. Systems with no separate GPU memory (GB10 / DGX Spark, Jetson) are treated as unified memory |
| AMD | `rocm-smi --json` | utilisation, temperature, power, VRAM |
| Apple Silicon | `ioreg` | GPU utilisation; memory is unified |
| none | | CPU and RAM only |

## Adding a source

Each kind is a function in `src/lonoboard/sources.py` that fills in a service record. See
[CONTRIBUTING.md](../CONTRIBUTING.md).
