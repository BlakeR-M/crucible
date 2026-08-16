<!-- Research document. Compiled 2026-08-16 against live sources. -->

> **What this is.** The sovereign-deployment question, answered with numbers
> rather than with reassurance: what runs fully locally on owned hardware, what
> it costs in quality, and what matching a frontier model actually costs in
> capital.
>
> It exists because "yes, it can run air-gapped" is the single most common
> claim made to Australian government buyers and the least often costed. The
> summary is in the project README; this is the working.
>
> Model names, benchmark figures and prices below are as at **16 August 2026**
> and will age. Where sources disagree, both numbers are given rather than one
> being chosen.

---

# Sovereign / Fully-Local Model Stack for an Agent Orchestration System
### Research report — compiled 16 August 2026
**Target hardware:** Windows PC, NVIDIA RTX 5080 (16 GB GDDR7, 960 GB/s), 64 GB+ system RAM
**Baseline being replaced:** OpenAI `gpt-5` (planner) / `gpt-5-mini` (worker) over the Chat Completions API
**Constraint that drives everything:** no data leaves the boundary

---

## 0. Executive summary

| Role in the orchestrator | Cloud today | Local equivalent (16 GB) | Local equivalent (scaled, on-prem) |
|---|---|---|---|
| Planner / reasoner | `gpt-5` | **Qwen3.6-35B-A3B** MoE at IQ4/Q3_K_XL with `--n-cpu-moe` | **GLM-5.2** (753B-A40B) or **DeepSeek V4-Flash** (284B-A13B) |
| High-volume worker | `gpt-5-mini` | **Qwen3.5-9B** at Q4_K_M/Q5_K_M, or **gpt-oss-20b** MXFP4 | **Qwen3.6-27B** dense FP8, or **Nemotron 3 Super** (120B-A12B) |
| Adversarial verifier | `gpt-5` (2nd pass) | **gpt-oss-20b** — deliberately a *different family* from the planner | **Nemotron 3 Ultra GenRM** or a cross-family pairing |
| Serving | OpenAI API | **llama.cpp `llama-server`** (dev) → **vLLM** (real) | **vLLM** or **SGLang**; TensorRT-LLM only when frozen |

**The honest headline:** on a single 16 GB card you can reach roughly `gpt-5-mini` class capability for worker tasks and *most* of it for planning. You cannot reach `gpt-5` class. The gap is not uniform — it is small for *judging* (1–2 points) and large for *generating a correct hard fix* (13+ points, see §7). A sovereign deployment that genuinely matches `gpt-5` on this workload needs 2× 96 GB-class GPUs minimum and lands around **USD 30–45k of hardware**, or **USD 370–450k** for an 8-GPU HGX node if the agency wants headroom and concurrency.

---

## 1. The open-weight model landscape as of August 2026

### 1.1 What changed in the last 12 months

Three things matter for this document:

1. **The Qwen line went through two more generations.** Qwen3.5 shipped February 2026, Qwen3.6 in April 2026. Both are Apache 2.0. The architecture moved to a hybrid **Gated DeltaNet + sparse MoE** design that materially improves the "small active parameter count, large total parameter count" tradeoff — which is exactly what a 16 GB card needs.
2. **Dense models got competitive again at 27B.** Qwen3.6-27B beats the previous-generation 397B MoE flagship on coding, on one GPU.
3. **The frontier open-weight models got enormous.** GLM-5.2 (753B), DeepSeek V4-Pro (1.6T), Kimi K3 (2.8T). These are the models that close the gap to `gpt-5`, and none of them run on consumer hardware. This is the crux of the sovereign cost argument in §5.

### 1.2 (a) Planning / reasoning candidates

| Model | Total / Active params | Context | Licence | Released | Key scores |
|---|---|---|---|---|---|
| **Qwen3.6-35B-A3B** | 35B / 3B | 262,144 native (→1.01M YaRN) | Apache 2.0 | Apr 2026 | SWE-bench Verified **73.4**, SWE-bench Pro 49.5, GPQA 86.0, LiveCodeBench v6 80.4, MMLU-Pro 85.2 |
| Qwen3.5-35B-A3B | 35B / 3B | 262,144 (→1.01M) | Apache 2.0 | Feb 2026 | SWE-bench Verified 69.2, GPQA-D 84.2, MMLU-Pro 85.3, **IFEval 91.9**, LiveCodeBench v6 74.6, AIME 2026 93.33 |
| **Qwen3.6-27B** (dense) | 27B / 27B | 128K–262K (sources disagree, see below) | Apache 2.0 | 21–22 Apr 2026 | SWE-bench Verified **77.2** (vendor/aggregator) vs **68.9** (third party) — *see caveat* |
| gpt-oss-120b | 117B / 5.1B | 131,072 | Apache 2.0 | 5 Aug 2025 | GPQA 80.1; ≈ o4-mini on core reasoning; single 80 GB GPU |
| GLM-5.2 | 753B / ~40B | 1M | MIT | 13–16 Jun 2026 | **SWE-bench Pro 62.1** (beats GPT-5.5 at 58.6), Terminal-Bench 2.1 81.0 |
| DeepSeek V4-Flash | 284B / 13B | 1M | MIT | 24 Apr 2026 | V4-Pro-Max: SWE-bench Verified 80.6 |
| DeepSeek V4-Pro | 1.6T / 49B | 1M | MIT | 24 Apr 2026 | as above; NIST CAISI: capabilities lag frontier by **~8 months** |
| Nemotron 3 Super | 120.6B / 12.7B | 1M | NVIDIA Open Model | 11 Mar 2026 | AA Intelligence Index 36; 2.2× throughput of gpt-oss-120b at 8K in / 64K out |
| Llama 4 Scout / Maverick | 109B/17B; 400B/17B | 10M / 1M | Llama Community | Apr 2025 | Ageing. Behemoth (2T) shelved, never released |
| Devstral 2 (Mistral) | 123B dense | — | Apache 2.0 | 2026 | SWE-bench Verified 72.2 |

**Caveat you should carry into the document:** Qwen3.6-27B's SWE-bench Verified figure is reported as **77.2** by [BenchLM](https://benchlm.ai/best/local-llm) and [BuildFastWithAI](https://www.buildfastwithai.com/blogs/qwen3-6-27b-review-2026), and as **68.9** by [Local AI Master](https://localaimaster.com/models/qwen-3-6-27b). Context length is likewise given as 128K by one and 262K by others. Treat 77.2 as an optimistic aggregator number and 68.9 as an independently-run number; the truth for *your* workload will be found by running your own harness, not by picking a side.

**Recommendation for the planner role on 16 GB:** Qwen3.6-35B-A3B. The MoE shape is the whole argument — 35B of knowledge with 3B of compute per token means it generates at roughly 8B-model speed while reasoning at something closer to 30B-model quality, and the routed experts can be pushed to system RAM when VRAM runs out (§4.4).

### 1.3 (b) Cheap high-volume worker candidates

| Model | Params | Q4 VRAM | Licence | Notable |
|---|---|---|---|---|
| **Qwen3.5-9B** | 9B dense-hybrid (32 layers, 4096 hidden) | ~6.5 GB (4-bit) / ~13 GB (8-bit) | Apache 2.0 | Beats gpt-oss-120b on MMLU-Pro (82.5 v 80.8), GPQA-D (81.7 v 80.1), **IFEval (91.5 v 88.9)**, AA-LCR long-context (63.0 v 50.7) |
| **gpt-oss-20b** | 20.9B total / 3.61B active, native MXFP4 | ~13.8–14 GB | Apache 2.0 | ≈ o3-mini class; explicitly designed for 16 GB edge devices |
| Nemotron 3 Nano (Omni) | ~30B / ~3B (64 experts, top-2) | ~15–16 GB at Q4_K_M | NVIDIA Open Model | 128K ctx; native audio/video/image input; ~75 tok/s on a 5080 |
| Qwen3.5-4B / 2B | 4B / 2B | ~5.5 / ~3.5 GB | Apache 2.0 | For classification, routing, extraction — see §7 warning on small-model quantisation |
| Gemma 4 12B | 12B | ~8 GB | Gemma terms | 67 tok/s on 5080; multimodal |
| LFM2 24B-A2B | 24B / 2B | ~14 GB | Liquid | Fastest thing that fills 16 GB: ~106 tok/s |

**Recommendation for the worker role:** Qwen3.5-9B at **Q5_K_M or Q8_0**, not Q4. It is small enough that the 8-bit weights fit (~13 GB) with room for KV cache, and small models suffer disproportionately from aggressive quantisation (§2.4). Its IFEval of 91.5 is the single most relevant number for a worker in an orchestration system — worker failures in agent pipelines are overwhelmingly instruction-following and format failures, not knowledge failures.

### 1.4 (c) Adversarial verification candidates

This is the role where the model choice is least about raw capability and most about **decorrelation**. The consensus in the 2026 literature and tooling is that a critic drawn from a *different* training distribution catches errors a same-family critic systematically misses:

> "Agents from a different model family performing independent critique … catching correlated training-data errors that same-family review tends to miss when replicas fail similarly." — [Cross-Model Adversarial Review](https://codex.danielvaughan.com/2026/03/28/cross-model-adversarial-review/), March 2026

The formal version of this is the **Refute-or-Promote** stage-gated adversarial multi-agent methodology ([arXiv:2604.19049](https://arxiv.org/pdf/2604.19049)), which uses adversarial stage gates specifically to raise precision in LLM-assisted defect discovery.

Practical local pairings, in order of preference:

1. **Qwen3.6-35B-A3B (planner) ↔ gpt-oss-20b (verifier)** — different labs, different data, different architecture family. Both fit the 16 GB budget if run sequentially, or the verifier can live on CPU/second card.
2. **Qwen3.6-35B-A3B ↔ Nemotron 3 Nano** — also cross-family, and Nemotron is NVIDIA-tuned so it runs fastest of the three on this hardware.
3. At scale: **NVIDIA-Nemotron-3-Ultra-550B-A55B-GenRM** — a purpose-built *generative reward model*, released 4 June 2026 under the Linux Foundation OpenMDW-1.1 licence. Given a conversation and two candidate responses it emits per-response helpfulness scores and a ranking score, and it accepts **user-defined principles** to condition the judgement. For a defence customer that wants the verification rubric to be inspectable and version-controlled, a GenRM with explicit principles is a materially better story than "we asked a chat model to be critical."

---

## 2. Quantisation: what it actually costs you

### 2.1 The best available measured data (Qwen3.5, GGUF)

Unsloth published a KL-divergence sweep across quantisers on Qwen3.5. KLD measures how far the quantised model's output *distribution* has moved from the BF16 original — it is the better metric because, as they note, perplexity lets over- and under-shoots cancel out.

| Quantiser | Quant | Size (GB) | Perplexity | Mean KLD | KLD 99.9th pct |
|---|---|---|---|---|---|
| Unsloth | IQ2_XXS | 9.09 | 7.72 | 0.185 | 4.22 |
| Unsloth | Q2_K_XL | 12.04 | 7.04 | 0.097 | 2.91 |
| Unsloth | IQ3_XXS | 13.12 | 6.78 | 0.050 | 1.53 |
| Unsloth | Q3_K_M | 15.54 | 6.73 | 0.032 | 0.97 |
| Unsloth | **MXFP4_MOE** | 18.17 | 6.60 | **0.027** | 0.78 |
| Unsloth | **Q4_K_M** | 18.49 | 6.61 | **0.019** | 0.55 |
| Unsloth | Q4_K_XL | 19.17 | 6.59 | 0.014 | 0.41 |
| Unsloth | Q5_K_XL | 23.22 | 6.55 | 0.007 | 0.24 |
| Unsloth | Q6_K_XL | 28.22 | 6.54 | 0.004 | 0.14 |
| Unsloth | Q8_K_XL | 36.04 | 6.54 | 0.003 | 0.10 |
| bartowski | Q4_K_M | 19.77 | 6.61 | 0.018 | 0.58 |
| bartowski | Q5_K_M | 23.11 | 6.58 | 0.011 | 0.35 |
| AesSedai | Q5_K_M | 24.45 | 6.54 | 0.006 | 0.21 |

Source: [Unsloth — Qwen3.5 GGUF benchmarks](https://unsloth.ai/docs/models/qwen3.5/gguf-benchmarks)

**Three things fall out of that table:**

- **Q4_K_M → Q5_K_M roughly halves mean KLD** (0.019 → 0.011) for about 25% more disk and VRAM. On a card where 5 GB is the difference between fitting and not, that is a real trade, not a free one.
- **Q5_K_M → Q8 halves it again** (0.011 → 0.003) but costs 55% more size. Q8 is near-lossless and almost never worth it above ~9B on a 16 GB card.
- **MXFP4 is worse than Q4_K_M at a *larger* file size** (0.027 vs 0.019 mean KLD, 18.17 GB vs 18.49 GB — near-identical size, 40% worse divergence). Unsloth's explicit advice: *avoid MXFP4 formats; Q4_K variants perform better at similar bit depths.* The exception is gpt-oss, which was **trained** in MXFP4 — there, MXFP4 *is* the reference precision and converting it to Q4_K gains nothing.

### 2.2 Task-level (not just perplexity) evidence

Perplexity and KLD are proxies. The direct benchmark evidence:

- **Gemma 3 27B, MMLU 5-shot** — Q4_K_XL (Unsloth dynamic, 15.64 GB) scored **71.47%** versus Google's own QAT model at 71.07% (17.2 GB) and the full QAT baseline at 70.64%. Q3_K_XL 70.87%, Q2_K_XL 68.70%. ([Unsloth Dynamic 2.0](https://unsloth.ai/docs/basics/unsloth-dynamic-2.0-ggufs))
- **Gemma 3 12B** — Q4_0 QAT 67.07% vs bfloat16 67.15%. A 0.08 point drop at 4-bit.
- **Qwen3.5-9B** — Q4_K_M scored **89.2%** instruction-level strict accuracy against the official BF16 figure of **91.5%**. A 2.3-point penalty.
- **Community WikiText-2 quality retention vs F16** (Llama 4 Scout 17B): Q4_K_M ≈ 98%, Q5_K_M ≈ 99%, Q8_0 ≈ 99.8%.

### 2.3 GGUF vs AWQ vs GPTQ vs FP8 vs NVFP4

| Format | Bits | Where it runs | Accuracy | Notes |
|---|---|---|---|---|
| **GGUF (K-quants, IQ-quants)** | 2–8 | llama.cpp, Ollama, LM Studio | See table above | Only format with mature **CPU/GPU hybrid offload**. Essential for MoE-on-16GB. |
| **AWQ** | 4 | vLLM, SGLang, TensorRT-LLM | ~1–2% better than GPTQ on benchmarks; better throughput (weight layout suits INT4 GEMM kernels) | The default INT4 choice for vLLM on Ada/Hopper/Ampere |
| **GPTQ** | 4 | vLLM, SGLang, TRT-LLM | Can beat AWQ on *real-world* tasks by 2.9 and 0.8 points with good calibration | Calibration-set sensitive |
| **FP8 (E4M3)** | 8 | Hopper, Ada, Blackwell — hardware accelerated | Near-lossless | **The right default on any datacentre GPU.** Halves KV cache with `--kv-cache-dtype fp8` |
| **NVFP4** | 4 | Blackwell only (B200, RTX PRO 6000, RTX 50-series) | Better than MXFP4 at the same bit depth; natively accelerated | Attractive for prefill-heavy coding-agent traffic. See [FAAR](https://arxiv.org/pdf/2603.22370) and [layer-wise FP4 sensitivity analysis](https://arxiv.org/pdf/2603.08747) |

The canonical accuracy/throughput trade-off paper is still **["Give Me BF16 or Give Me Death"? Accuracy-Performance Trade-Offs in LLM Quantization](https://arxiv.org/pdf/2411.02355)** — worth citing directly in the documentation, because a government reviewer will ask "how do you know quantisation didn't break it" and that paper is the standard answer.

### 2.4 The rule that matters most for a worker tier

**Small models degrade far more than large ones.** From a systematic on-device evaluation ([arXiv:2505.15030](https://arxiv.org/html/2505.15030v5)):

| Model size | GSM8K @ q5_k | GSM8K @ q3_k | Drop |
|---|---|---|---|
| 1.5B | 59.14% | 46.47% | **−12.7 pts** |
| 14B | 89.08% | 89.01% | −0.07 pts |

And fp16 → q4_k on the 1.5B: 60.80% → 52.77%, an 8-point loss.

**Practical consequence for this architecture:** do not put a 2B–4B model at Q4 in a high-volume worker slot and expect the JSON to come back clean. Either run the small model at Q8 (it is only a few GB) or use a 9B at Q4/Q5. The 9B at Q4 is a better worker than a 4B at Q8 in almost every case.

---

## 3. Inference stacks compared

| | Ollama | llama.cpp (`llama-server`) | LM Studio | vLLM | SGLang | TensorRT-LLM |
|---|---|---|---|---|---|---|
| **OpenAI `/v1/chat/completions`** | Yes | Yes | Yes | Yes | Yes | Yes (`trtllm-serve`, since 0.9.0) |
| **Tool / function calling** | Yes, native | Yes | Yes | Yes | Yes | Yes |
| **Structured output (JSON schema)** | Yes via `response_format` — but *enforcement is the weak spot* | Yes (GBNF grammars) | Yes — more complete than Ollama | Yes since v0.8.5; **xgrammar** default backend | Yes | Yes |
| **Continuous batching** | No (sequential) | Partial | No | **Yes (PagedAttention)** | **Yes (RadixAttention)** | Yes |
| **Concurrency at 128 users** | ~484 tok/s aggregate, OOM ~40 users | n/a | n/a | **8,033 tok/s**, 180+ concurrent, p99 <2s | comparable to better | best |
| **p99 latency @ 50 concurrent** | **24.7 s** | n/a | n/a | **2.8 s** | ~ | ~ |
| **CPU/RAM offload for oversized models** | Yes | **Yes — best in class (`--n-cpu-moe`)** | Yes | Limited | Limited | No |
| **Model swap at runtime** | Yes | Yes | Yes | One model per process | One per process | **No** — engine is compiled per GPU + dtype |
| **Cold start** | seconds | seconds | seconds | **~62 s** | ~60 s | **10–30 min compile** (one-off), then seconds |
| **Windows native** | Yes | Yes | Yes | WSL2 / Linux only | Linux | Linux |
| **Setup difficulty** | trivial | moderate | trivial | moderate–hard | moderate–hard | hard |

**Measured throughput deltas:**
- vLLM vs Ollama at 128 concurrent: **8,033 vs 484 tok/s — 16.6×**. At 8 concurrent: 187 vs 82 tok/s. At 1 concurrent: 38–71 vs 45–62 tok/s (Ollama is *fine* single-user). ([runaihome, 11 May 2026](https://runaihome.com/blog/vllm-vs-ollama-when-each-wins-2026/))
- SGLang vs vLLM on an 8B with ShareGPT traffic: **16,200 vs 12,500 tok/s (+29%)**, driven by RadixAttention prefix reuse (prefix hit rate 18% → 71%, TTFT 1.4 s → 380 ms). At 70B the gap narrows to 3–5%.
- TensorRT-LLM vs vLLM: **30–50% better throughput for batch serving**, at the cost of ahead-of-time compilation, non-portable engines, and no runtime model/quant swapping.

### 3.1 The Blackwell / RTX 5080 practical warning

This is the part most guides skip and it will cost you a day if it is not in the documentation.

- vLLM needs **CUDA 12.8+** for Blackwell `sm_120`. Older wheels will not compile kernels for the 5080.
- **FlashAttention 3 does not work on Blackwell yet** — set `VLLM_FLASH_ATTN_VERSION=2`.
- Under WSL2, **2.6.3 stable does not expose Blackwell's native FP8 tensor cores to compute workloads** ([microsoft/WSL#40333](https://github.com/microsoft/WSL/issues/40333)). WSL2 **2.7.0** fixes CUDA graph capture on `sm_120`. If the documentation recommends vLLM on Windows, it must specify the WSL version.
- There is a known bad CUDA release: **avoid CUDA 13.2 — it produces gibberish output on Qwen3.6; use 13.1 or 13.3.**

### 3.2 Recommended stack progression

1. **Develop on `llama.cpp` / `llama-server` or Ollama.** Both speak the OpenAI API. Your orchestrator's base URL changes and nothing else does. Ollama's single-user throughput is within noise of vLLM's, and it is the only realistic Windows-native option.
2. **Move to vLLM the moment you have more than ~5 concurrent agent turns.** This is the threshold in every benchmark set. An orchestration system with parallel sub-agents crosses it immediately.
3. **Use SGLang instead of vLLM if your agent prompts share long system prefixes** — which orchestration systems almost always do. A fixed 4,000-token system prompt across 20 agents is exactly the case RadixAttention was built for.
4. **Reach for TensorRT-LLM only when the model, the GPU, and the quantisation are all frozen** — i.e. a production accreditation boundary, not a development machine. The 10–30 minute per-config compile and the non-portable engines are the cost of the last 30–50%.

**Do not skip the structured-output backend.** Failure rates for JSON conformance, measured:

| Method | Failure rate |
|---|---|
| Prompt constraint only | 5–10% |
| "JSON mode" | 2–5% |
| Grammar-constrained structured outputs (xgrammar/GBNF/Outlines) | **<0.1%** |

For an agent system where a malformed tool call kills a whole run, that is the difference between working and not. Cost: constrained decoding carries a **3.6×–8.2× latency penalty** in the worst measured cases ([arXiv:2605.02363](https://arxiv.org/html/2605.02363v1)), though xgrammar's caching brings typical overhead far below that.

---

## 4. Realistic tokens/sec on a 5080-class card

### 4.1 The card

RTX 5080: **16 GB GDDR7, 960 GB/s, 256-bit bus, ~15.5 GB usable** for models after driver/display overhead.

### 4.2 Measured / reported figures

| Model | Quant | VRAM | tok/s | Source & confidence |
|---|---|---|---|---|
| Llama 3.1 8B | Q4_K_M | ~5.5 GB | **~132** | [Local AI Master](https://localaimaster.com/blog/rtx-5090-vs-5080-local-ai), Mar/Aug 2026 — high |
| Llama 3.1 8B | Q5_K_M | ~8 GB | ~81 | [modelfit](https://modelfit.io/gpu/rtx-5080/), 13 Aug 2026 — medium |
| Qwen3.5 9B | Q4_K_M | ~7 GB | **~85** | modelfit — medium |
| Qwen3.5 9B | Q8_0 | ~10.7 GB | **~53** | modelfit — medium |
| Gemma 4 12B | Q4_K_M | ~8 GB | ~67 | modelfit — medium |
| Qwen3 14B / Qwen2.5-Coder 14B | Q4_K_M | ~11 GB | **~58** | modelfit — medium |
| Qwen 2.5 14B | Q4_K_M | ~9.5 GB | ~85 | Local AI Master — medium |
| **gpt-oss-20b** | MXFP4 (native) | ~13.8 GB | **~79** (modelfit) / **~140** (Ollama bench) | **conflicting — see note** |
| Nemotron 3 Nano Omni ~30B-A3B | Q4_K_M | ~15–16 GB | **~75** | [Compute Market](https://www.compute-market.com/blog/nemotron-3-nano-omni-local-hardware-guide-2026), 13 May 2026 — medium |
| LFM2 24B-A2B | Q4_K_M | ~14 GB | **~106** | modelfit — medium |
| Qwen 2.5 32B / DeepSeek-R1 32B | Q4_K_M | ~22 GB (spills 6 GB) | **~6–20** | Local AI Master / modelfit — the spill penalty |

**On the gpt-oss-20b discrepancy:** the ~140 tok/s figure comes from short-prompt Ollama benchmark runs and is quoted against both a 4080 and a 5080 in different write-ups; modelfit's ~79 tok/s is a more conservative figure with realistic context. **Document it as "roughly 80–140 tok/s depending on context length and harness"** rather than picking one. The 5080 is memory-bandwidth-bound at 960 GB/s, and a 3.61B-active MoE at 4-bit theoretically tops out well above 140 tok/s, so both numbers are physically plausible; the difference is prompt length and KV pressure.

### 4.3 Size-class rules of thumb on this card

> 7B ≈ 105 tok/s · 14B ≈ 58 tok/s · 20B MoE (3.6B active) ≈ 89 tok/s — all fully resident.
> 32B dense ≈ 6 tok/s once it spills to system RAM.
> — [modelfit RTX 5080 profile](https://modelfit.io/gpu/rtx-5080/), 13 Aug 2026

**KV cache budget:** +~0.5 GB at 16K context, +~2.0 GB at 64K. Both fit alongside the recommended models. Do not plan a 128K-context agent on this card without accepting a large throughput loss (a 27B at IQ3_XXS drops from 45 t/s at 19K to **9.6 t/s at 128K**).

**The single most important number in this section:** a model that fits entirely in VRAM is **3–11× faster** than one that offloads. gpt-oss-20b at 139.93 tok/s versus gpt-oss-120b with heavy CPU offload at 12.64 tok/s on the same box is the canonical illustration. Design the local tier around *fitting*, not around *the biggest model you can technically load*.

### 4.4 The MoE offload technique — how 35B fits in 16 GB

`llama.cpp`'s `--n-cpu-moe N` moves the **routed expert FFN weights of the first N layers to system RAM**. Attention, KV cache, the router, shared experts and norms all stay on the GPU. Because only ~3B of 35B parameters activate per token, and the hot experts stay resident, the penalty is far smaller than naive layer offload.

**Critical framing for the documentation, because it is widely misreported:** `--n-cpu-moe` **does not make a fitting model faster.** It makes a *non-fitting* model *possible*. The famous "5× speedup" is only ever measured against the alternative of generic layer offload. Once the model fits, every increment of `--n-cpu-moe` costs throughput monotonically.

Measured sweep on Qwen3.6-35B-A3B, IQ4_NL ([openclawdc, 29 Jul 2026](https://openclawdc.com/blog/llama-cpp-moe-offload-flags-explained/)):

| GPU | `--n-cpu-moe` | tok/s |
|---|---|---|
| RTX 5090 32GB | 20 | 54 |
| RTX 5090 32GB | 16 | 64.5 |
| RTX 5090 32GB | 12 | **69.4** |
| RTX 5090 32GB | 11 (VRAM spill) | 27.5 |
| RTX 4080 16GB | (unstated) | **~60** |
| RTX 3060 12GB @ 64K ctx | (unstated) | 51–53 |

Tuning procedure for a 16 GB card: **start at `--n-cpu-moe 20`**, step down in increments of 2, measure tok/s at each stop, and stop one step before the OOM. If you OOM, cut `--ctx-size` first (16384 → 8192) before raising `--n-cpu-moe`.

Independently, on a 16 GB card (RTX 4080, i7-14700, 64 GB DDR5) with llama.cpp ([glukhov](https://www.glukhov.org/llm-performance/benchmarks/best-llm-on-16gb-vram-gpu/)):

| Model | Quant | Context | VRAM | tok/s |
|---|---|---|---|---|
| **Qwen3.6-35B-A3B** | UD-IQ3_XXS | 19K | 13.8 GB | **147.5** |
| GLM-4.7-Flash-REAP-23B | IQ4_XS | 32K | 14.4 GB | 122.0 |
| Qwen3.5-27B (dense) | UD-IQ3_XXS | 19K | 12.9 GB | 45.3 |
| Qwen3.5-27B (dense) | UD-IQ3_XXS | 128K | — | 9.6 |

**That first row is the answer to the whole 16 GB question.** A 35B-parameter, 73.4-SWE-bench-Verified model, fully resident in 13.8 GB, at 147 tok/s. The cost is IQ3_XXS quantisation — mean KLD around 0.050, roughly 2.6× the divergence of Q4_K_M. That is a real quality cost and §7 discusses where it bites.

### 4.5 16 GB tier — the three-model recommended set

| Role | Model | Quant | VRAM | Expected tok/s |
|---|---|---|---|---|
| Planner | Qwen3.6-35B-A3B | UD-IQ3_XXS or UD-Q3_K_XL | 13.8–16.6 GB | 60–147 |
| Worker | Qwen3.5-9B | Q5_K_M / Q8_0 | 9–13 GB | 53–85 |
| Verifier | gpt-oss-20b | MXFP4 (native) | 13.8 GB | 79–140 |

These cannot all be resident simultaneously on one 16 GB card. Options: (a) sequential swap via Ollama/LM Studio's model manager, ~5–15 s per swap; (b) planner on GPU, worker on CPU (a 9B at Q4 runs 8–15 tok/s on a modern 16-core CPU — acceptable for background workers); (c) accept the swap cost and design the orchestrator's phases around it. **Option (c) is usually right** — planning, working and verifying are naturally sequential phases in most agent loops.

---

## 5. What a scaled sovereign deployment looks like

### 5.1 The sizing arithmetic to put in the document

```
VRAM_weights = num_params × bytes_per_param
  FP16/BF16 = 2 bytes | FP8/INT8 = 1 byte | INT4 = 0.5 bytes

VRAM_total ≈ (VRAM_weights + KV_cache) × 1.3–1.5   ← activations + framework overhead
```

Worked reference: a 70B model at 128K context in FP16 needs **~42.9 GB of KV cache alone**, on top of 140 GB of weights. This is why "70B needs 2× 80 GB cards" and not "70B needs 140 GB."

### 5.2 Four deployment tiers, with real prices

| Tier | Hardware | VRAM | Capital cost (USD) | What it runs | Concurrency |
|---|---|---|---|---|---|
| **T0 — Dev workstation** | 1× RTX 5080 | 16 GB | ~$1,000 (card) | 9B–35B-A3B quantised | 1–3 agents |
| **T0.5 — Appliance** | NVIDIA DGX Spark (GB10) | 128 GB unified @ 273 GB/s | **$4,699** (was $3,999 at Oct 2025 launch; Feb 2026 memory-driven rise) | ~200B params at low precision; 120B coding model at 35–80 tok/s | Bandwidth-bound; strong at high batch, weak single-stream (70B capped ~2.7 tok/s) |
| **T1 — Departmental** | 1–2× RTX PRO 6000 Blackwell 96 GB | 96–192 GB | **$14,000–16,000 per card** (MSRP $16,000 Aug 2026, up from $13,250 Jun 2026; Newegg $13,998, B&H $15,499) | Llama-3.3-70B Q4 at ~30–45 tok/s single-request under vLLM; native FP4 | 10–40 concurrent |
| **T2 — Agency production** | 8× HGX H200 | 8× 141 GB = 1,128 GB | **$320,000–420,000**, typical **$370,000** | Any open-weight model up to ~400B at FP8; >3,800 tok/s on 70B FP8 per GPU | Hundreds |
| **T3 — Frontier-parity** | 8× HGX B200 | 8× 180 GB | **$400,000–500,000**, typical **$450,000** | DeepSeek V4-Flash (284B-A13B), GLM-5.2 with offload; NVFP4 native | Hundreds+ |

Individual GPU street prices, Aug 2026: **H200 $30–40k** (list $40–55k), **B200 $30–40k in 8-GPU volume but $45–50k in singles**. Note the allocation reality — most Blackwell production is absorbed by hyperscalers; institutional buyers face waitlists and enterprise-only channels. Lead times: H100 SXM5 **2–6 weeks**, H200 **4–8 weeks**.

### 5.3 Throughput you can promise at each tier

| GPU | Model / precision | Single-request | 100 concurrent |
|---|---|---|---|
| H100 SXM5 80 GB | Llama-3.3-70B FP8, ~512 in / ~256 out | ~120 tok/s | **~2,400 tok/s** |
| H200 141 GB | 70B FP8 | ~43% faster decode than H100 (bandwidth) | **>3,800 tok/s** |
| L40S 48 GB | 32–34B class, 864 GB/s | — | Limited to smaller workloads |
| RTX PRO 6000 96 GB | 70B Q4_K_M / FP4 | 30–45 tok/s | FP4 doubles throughput vs FP8 |

**On L40S specifically:** it appears in a lot of procurement documents because it is PCIe, 300W, and passively cooled — easy to rack in an existing agency data centre. But at 48 GB and 864 GB/s it tops out around 32–34B models. For a system that wants a 70B+ planner it is the wrong card; two RTX PRO 6000s cost similar and give 192 GB.

### 5.4 Total cost of ownership and break-even

Three-year TCO for **one** 8× H100 server:

| Line | 3-year cost |
|---|---|
| Hardware depreciation | $350,000–450,000 |
| Staff (0.5–1 FTE) | $225,000–300,000 |
| Power | $31,500–32,000 |
| Colocation | $36,000–72,000 |
| Networking / storage / maintenance | balance |
| **Total** | **$712,000–948,000** |

Break-even: an 8× H100 cluster pays back against Azure on-demand in **~3.7 months at 90% utilisation**. Per-token, on-prem runs **$0.11 per million tokens** versus **$0.89** on Azure — an 8× gap. Across the literature the break-even utilisation threshold sits at **50–83% sustained**; below that, cloud wins on pure economics.

**Australian SMB reality check** ([Automata AI, Sydney, June 2026](https://www.automataai.com.au/blog/the-real-cost-of-self-hosting-an-open-source-llm-in-australia)): a self-hosted open-source LLM costs an Australian SMB **~$80,000/year minimum**, and **~$160,000/year** once you count a full engineer and proper redundancy. Owned GPU server: **$40,000+ capital outlay** plus power, cooling and rack space. Cloud GPU instances: **$3–12/hour**, i.e. **$26,000–105,000/year per node**. Budget **0.5–1 FTE per cluster** for drivers, firmware, hardware failures and cluster ops.

**For the defence audience, this is the argument to lead with:** for a government agency the economics are *not* the case for sovereign deployment — the compliance boundary is. Frame the cost honestly and let the boundary carry the decision. An agency spending $370k on an HGX H200 node is not saving money against `gpt-5` API calls at typical volumes; it is buying the ability to process data that legally cannot leave the boundary at all.

### 5.5 The compliance frame

The technical stack is only half the sovereign story. What an Australian government or defence buyer actually needs:

- **PSPF compliance** and **ISM technical controls**, with national security classification handling.
- **An IRAP assessment by an ASD-endorsed assessor**, and deployment on **PROTECTED-certified infrastructure** — typically the agency's own data centre, or a cloud platform holding a current ASD PROTECTED certification.
- **Data sovereignty, not merely data residency.** The distinction the market now draws: sensitive information stored onshore *and processed on Australian infrastructure, governed by Australian law, operated under Australian control.* Residency alone does not satisfy it.
- **Defence-specific governance:** the Department of Defence released *Policy Settings for Responsible Use of Artificial Intelligence in Defence* in **March 2026**, a dedicated framework sitting outside the DTA's whole-of-government AI policy.

The standard air-gapped stack is straightforward and worth stating plainly because it reassures: **Linux + NVIDIA drivers/CUDA, vLLM or NVIDIA NIM for serving, Prometheus/Grafana for monitoring** — all installable inside an air-gapped enclave with no egress.

**Licence hygiene matters here too.** For a defence customer, the licence column in §1.2 is not trivia. Apache 2.0 (Qwen, gpt-oss) and MIT (GLM-5.2, DeepSeek V4) are clean. Llama's community licence carries EU multimodal restrictions and a headcount clause. NVIDIA's Open Model License and the Linux Foundation OpenMDW-1.1 (Nemotron 3 Ultra) each need their own read. **Country-of-origin will also be raised** — Qwen (Alibaba), GLM (Zhipu), DeepSeek and Kimi are all Chinese-origin weights. The weights are inspectable and run air-gapped, which is the technically correct answer, but expect the question and answer it in the document rather than waiting to be asked. NIST's CAISI has published an evaluation of DeepSeek V4 Pro; citing an allied-government evaluation is more persuasive than asserting the point yourself.

---

## 6. Reference architecture for the sovereign build

```
                    OpenAI-compatible /v1/chat/completions
                                   │
                    ┌──────────────┴──────────────┐
                    │   Orchestrator (unchanged)  │
                    └──────────────┬──────────────┘
                                   │  base_url swap only
        ┌──────────────────────────┼──────────────────────────┐
        │                          │                          │
   PLANNER                     WORKER                    VERIFIER
   Qwen3.6-35B-A3B         Qwen3.5-9B                gpt-oss-20b
   IQ3_XXS / Q3_K_XL       Q5_K_M / Q8_0             MXFP4 native
   ~14–17 GB               ~9–13 GB                  ~14 GB
   60–147 tok/s            53–85 tok/s               79–140 tok/s
        │                          │                          │
        └──── grammar-constrained JSON (xgrammar / GBNF) ──────┘
                       <0.1% schema failure rate
```

**The migration is a base-URL change plus a grammar backend.** Every stack in §3 exposes `/v1/chat/completions`. What breaks in practice is not the transport, it is:

- **Tool-call schema strictness** — OpenAI's strict mode has no exact local equivalent; you get it back via xgrammar/Outlines/GBNF, which is *better* (<0.1% failure) but must be wired up explicitly per-request.
- **Reasoning-effort parameters** — gpt-oss has adjustable reasoning levels; Qwen3.5/3.6 use `--chat-template-kwargs '{"enable_thinking": true|false}'`. Neither maps to OpenAI's parameter names. The orchestrator needs a thin adapter layer.
- **Sampling defaults.** Qwen3.5/3.6 are sensitive here. Vendor-recommended: thinking mode general `temp 1.0, top_p 0.95, top_k 20`; thinking mode **precise coding** `temp 0.6, top_p 0.95, top_k 20`; non-thinking general `temp 0.7, top_p 0.8, top_k 20`. Max output 32,768 tokens. Using OpenAI's defaults here will make the model look worse than it is.

---

## 7. The honest quality gap

This section is the one that earns trust with a government reviewer. Overstate local capability once and the whole document is discounted.

### 7.1 Baseline to beat

| | SWE-bench Verified | GPQA | AIME 2025 | Price |
|---|---|---|---|---|
| **gpt-5** | **74.9%** | 88.4% (89.4 thinking) | 94.6% | — |
| **gpt-5-mini** | **48.0%** | 68.7 | — | $0.125/M in, $1.00/M out, 400K ctx |

### 7.2 Where local is genuinely at parity

**Judging and verification.** This is the good news and it is the load-bearing finding for the adversarial-verification lane. On reasoning LLM-judge accuracy: **GPT-4.1 75.4%, Gemini-2.5-Pro 78.2%** — against which **Kimi-K2-0711 is 0.2 points behind and gpt-oss-120b-low is 1.5 points behind.** Critiquing an artefact is a much easier task than producing one, and open weights have effectively closed that gap.

**Instruction following.** Kimi K2.5 leads IFEval at **94.0**, Qwen3.5 at **92.6**, Nemotron Ultra 89.5. Qwen3.5-9B alone posts **91.5**. There is no meaningful instruction-following deficit at the top of the open-weight field.

**Knowledge and long context.** Qwen3.5-9B beats gpt-oss-120b on MMLU-Pro (82.5 v 80.8), GPQA-D (81.7 v 80.1), and long-context retrieval (AA-LCR 63.0 v 50.7; LongBench v2 55.2 v 48.2) — at one-thirteenth the parameters.

### 7.3 Where local genuinely falls short — for *this* workload

**(a) Adversarial code review — the most directly relevant measurement available.**

[Foundation Models as Oracles for Refactoring Correctness Detection](https://arxiv.org/abs/2605.02096) tested zero-shot detection of 226 real refactoring-introduced bugs across 47 refactoring types in Java IDEs:

| Model | Accuracy |
|---|---|
| **GPT-5.4** (closed) | **93.8%** |
| Gemini-3.1-Pro-Preview (closed) | best overall |
| **Gemma-4-31B** (open, ~24 GB at Q4) | best open-weight |
| **gpt-oss-20b** (open, runs on your 5080) | **80.5%** |

**That is a ~13-point gap on exactly the task the verification tier exists to do.** Framed operationally: for every 100 subtly-broken changes the pipeline sees, `gpt-5`-class catches ~94 and the locally-runnable verifier catches ~80. **Roughly one in five defects that the cloud verifier would catch, the local verifier will wave through.**

The mitigation is architectural rather than model-shopping: **stage-gated multi-agent adversarial review with cross-family critics** ([Refute-or-Promote, arXiv:2604.19049](https://arxiv.org/pdf/2604.19049)). Two decorrelated 80%-accurate verifiers in an ensemble, with disagreement escalated to a human, recover a meaningful fraction of that gap — and the design is defensible to an assurance reviewer in a way that "we used a bigger model" is not. Document the residual gap explicitly and make the human escalation path part of the architecture, not a footnote.

**(b) Structured JSON output — the sharpest cliff.**

From [When Correct Isn't Usable](https://arxiv.org/html/2605.02363v1), testing Llama-3.1-8B, Gemma-2-9B and Qwen-2.5-7B:

- **Naive prompting: 0% JSON validity across all three models on all datasets** — despite 77–85% underlying task accuracy on GSM8K. The models solved the problem and then returned it unparseably.
- Dominant failure mode: **markdown fence wrapping** (```` ```json ... ``` ````) — which GPT-4o also does, but which downstream parsers in agent frameworks silently choke on. Second: unescaped LaTeX backslashes breaking JSON string validity.
- Even with a minimal hand-written prompt, output accuracy was 44.43% (Llama), **0% (Gemma)**, 74.30% (Qwen) on GSM8K, and 8.54% / 0% / 0% on MATH.
- With optimised prompting: 84–87% output accuracy on GSM8K — against **GPT-4o at 95.22%**.
- **Constrained decoding costs 3.6×–8.2× in latency**, and in one configuration *reduced* accuracy (Qwen constrained 32.83% vs optimised-prompt 85.75%) — over-constraining can fight the model's reasoning.

Separately: schema non-compliance for sub-4B models rises from **2–3% on flat schemas to 68–69% when JSON Schema `$defs` is present**. **Flatten your schemas.** That one sentence will save more agent runs than any model upgrade.

**(c) Tool calling.**

Top of BFCL v3 as of 29 June 2026: **GLM 4.5 at 76.7%**, Claude Opus 4.7 at 76.6%, Gemini 3.1 Flash Lite Preview at 76.5%. Open weights are at the top of this leaderboard — but note that *nobody* is above 77%. Multi-turn tool use remains the weakest link in agent systems regardless of provenance, and a sovereign deployment does not make it worse. It does mean the orchestrator needs retry and repair logic either way.

**(d) The frontier is genuinely still ahead, and by a measurable amount.**

NIST's CAISI evaluation of DeepSeek V4 puts open-weight capability **~8 months behind the frontier**. That is the honest number to quote — it is from an allied government evaluator, it is specific, and it is far more credible than "open models have caught up."

**(e) Quantisation compounds the gap.** Every benchmark in §1.2 is measured at BF16. The 16 GB tier runs the planner at IQ3_XXS — mean KLD ~0.050 versus ~0.019 at Q4_K_M. The 73.4 SWE-bench Verified figure for Qwen3.6-35B-A3B is **not** the number you will get at IQ3_XXS on a 5080. No one has published the IQ3 delta on SWE-bench specifically; you should measure it on your own tasks and report the measured number, not the model card's.

### 7.4 Failure modes to design around

| Failure | Frequency | Mitigation |
|---|---|---|
| Markdown-fenced JSON | Very high on naive prompts | Grammar-constrained decoding; strip-fence pre-parse as belt and braces |
| Schema non-compliance with nested `$defs` | Up to 69% on small models | Flatten schemas; one level of nesting maximum |
| Verifier false-approval | ~1 in 5 vs frontier | Cross-family ensemble + human escalation on disagreement |
| Long-horizon plan drift | Higher than frontier | Shorter agent turns, explicit state checkpointing, dense re-grounding |
| Context degradation past 64K | Severe on 16 GB (45 → 9.6 tok/s at 128K) | Retrieval over long context; keep working context under 32K |
| Small-model quantisation collapse | Up to 12.7pp at q3_k on 1.5B | Never run sub-4B below Q8 |

---

## 8. Concrete recommendations

**For the 5080 workstation (the demo / development box):**
- llama.cpp or Ollama, GGUF, Unsloth Dynamic (`UD-*`) quants. Windows-native, no WSL needed.
- Planner: `Qwen3.6-35B-A3B` UD-IQ3_XXS or UD-Q3_K_XL, tuned with `--n-cpu-moe` starting at 20 and stepping down.
- Worker: `Qwen3.5-9B` at Q5_K_M (Q8_0 if VRAM allows after the planner is unloaded).
- Verifier: `gpt-oss-20b` MXFP4 — native precision, do not requantise.
- Grammar-constrained JSON on every structured call. Flat schemas.
- Avoid CUDA 13.2.

**For a departmental pilot (the first real sovereign deployment):**
- 2× RTX PRO 6000 Blackwell 96 GB (~USD 28–32k of GPU) in a single workstation-class chassis, Linux, vLLM with FP8 weights and `--kv-cache-dtype fp8`.
- Planner: `Qwen3.6-27B` dense at FP8, or `Nemotron 3 Super` (120B-A12B) if 192 GB is available.
- SGLang instead of vLLM if agent prompts share a long fixed system prefix — RadixAttention took prefix hit rate from 18% to 71% and TTFT from 1.4 s to 380 ms in the published measurement.

**For agency production:**
- 8× HGX H200, ~USD 370k, ~USD 712–948k three-year TCO.
- `GLM-5.2` (MIT, 753B-A40B) or `DeepSeek V4-Flash` (MIT, 284B-A13B) as the planner; this is the tier at which you can honestly claim near-`gpt-5` capability.
- vLLM or TensorRT-LLM once the configuration is frozen for accreditation.
- `Nemotron-3-Ultra-GenRM` as the verification tier, with the review rubric expressed as user-defined principles — inspectable, version-controlled, auditable.

**What to say about the gap, verbatim, in the document:** *"At 16 GB, this system reaches roughly gpt-5-mini capability for worker tasks and near-parity for verification judging. It does not reach gpt-5 for generating correct fixes to hard problems — measured at roughly a 13-point gap on adversarial code review. Closing that gap requires either an on-premise server class deployment, or accepting the gap and designing human escalation into the verification stage. Both are legitimate; the choice is a risk decision, not a technical one."*

---

## Sources

**Model cards and primary vendor documentation**
- [Qwen/Qwen3.5-35B-A3B — Hugging Face](https://huggingface.co/Qwen/Qwen3.5-35B-A3B)
- [Qwen/Qwen3.6-35B-A3B — Hugging Face](https://huggingface.co/Qwen/Qwen3.6-35B-A3B)
- [Qwen/Qwen3.5-9B — Hugging Face](https://huggingface.co/Qwen/Qwen3.5-9B)
- [Qwen/Qwen3-30B-A3B-Instruct-2507 — Hugging Face](https://huggingface.co/Qwen/Qwen3-30B-A3B-Instruct-2507)
- [nvidia/NVIDIA-Nemotron-3-Ultra-550B-A55B-GenRM — Hugging Face](https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Ultra-550B-A55B-GenRM)
- [openai/gpt-oss-120b — Hugging Face](https://huggingface.co/openai/gpt-oss-120b)
- [Introducing gpt-oss — OpenAI](https://openai.com/index/introducing-gpt-oss/)
- [gpt-oss model card (PDF) — OpenAI](https://cdn.openai.com/pdf/419b6906-9da6-406c-a19d-1bb078ac7637/oai_gpt-oss_model_card.pdf)
- [Introducing GPT-5 — OpenAI](https://openai.com/index/introducing-gpt-5/)
- [DeepSeek V4 Preview Release — DeepSeek API Docs](https://api-docs.deepseek.com/news/news260424/)
- [CAISI Evaluation of DeepSeek V4 Pro — NIST](https://www.nist.gov/news-events/news/2026/05/caisi-evaluation-deepseek-v4-pro)
- [Unsloth — Qwen3.5: How to Run Locally](https://unsloth.ai/docs/models/qwen3.5)
- [Unsloth — Qwen3.6: How to Run Locally](https://unsloth.ai/docs/models/qwen3.6)
- [Unsloth — Qwen3.5 GGUF Benchmarks](https://unsloth.ai/docs/models/qwen3.5/gguf-benchmarks)
- [Unsloth — Dynamic 2.0 GGUFs](https://unsloth.ai/docs/basics/unsloth-dynamic-2.0-ggufs)
- [vLLM — Structured Outputs](https://docs.vllm.ai/en/v0.8.2/features/structured_outputs.html)
- [TensorRT-LLM Quick Start Guide — NVIDIA](https://nvidia.github.io/TensorRT-LLM/quick-start-guide.html)
- [OpenAI-Compatible Frontend for Triton — NVIDIA](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/client_guide/openai_readme.html)
- [Ollama — Structured Outputs](https://docs.ollama.com/capabilities/structured-outputs)

**Peer-reviewed / preprint**
- ["Give Me BF16 or Give Me Death"? Accuracy-Performance Trade-Offs in LLM Quantization — arXiv:2411.02355](https://arxiv.org/pdf/2411.02355)
- [XGrammar: Flexible and Efficient Structured Generation Engine — arXiv:2411.15100](https://arxiv.org/pdf/2411.15100)
- [A Systematic Evaluation of On-Device LLMs: Quantization, Performance, and Resources — arXiv:2505.15030](https://arxiv.org/html/2505.15030v5)
- [Does quantization affect models' performance on long-context tasks? — arXiv:2505.20276](https://arxiv.org/pdf/2505.20276)
- [When Correct Isn't Usable: Improving Structured Output Reliability in Small Language Models — arXiv:2605.02363](https://arxiv.org/html/2605.02363v1)
- [Foundation Models as Oracles for Refactoring Correctness Detection — arXiv:2605.02096](https://arxiv.org/abs/2605.02096)
- [Refute-or-Promote: Adversarial Stage-Gated Multi-Agent Review — arXiv:2604.19049](https://arxiv.org/pdf/2604.19049)
- [ProfBench: Multi-Domain Rubrics requiring Professional Knowledge (ICLR 2026) — arXiv:2510.18941](https://arxiv.org/abs/2510.18941)
- [Nemotron 3 Ultra: Open, Efficient MoE Hybrid Mamba-Transformer — arXiv:2606.15007](https://arxiv.org/html/2606.15007v1)
- [Nemotron 3 Super — arXiv:2604.12374](https://arxiv.org/pdf/2604.12374)
- [FAAR: Format-Aware Adaptive Rounding for NVFP4 — arXiv:2603.22370](https://arxiv.org/pdf/2603.22370)
- [Diagnosing FP4 inference: layer-wise and block-wise sensitivity of NVFP4 and MXFP4 — arXiv:2603.08747](https://arxiv.org/pdf/2603.08747)
- [The Berkeley Function Calling Leaderboard — PMLR v267](https://proceedings.mlr.press/v267/patil25a.html) · [BFCL V4 leaderboard](https://gorilla.cs.berkeley.edu/leaderboard.html)

**Hardware benchmarks and pricing**
- [modelfit — RTX 5080 16GB Local LLM profile (13 Aug 2026)](https://modelfit.io/gpu/rtx-5080/)
- [Local AI Master — RTX 5090 vs 5080 for Local AI (Mar/Aug 2026)](https://localaimaster.com/blog/rtx-5090-vs-5080-local-ai)
- [Glukhov — 16 GB VRAM LLM benchmarks with llama.cpp](https://www.glukhov.org/llm-performance/benchmarks/best-llm-on-16gb-vram-gpu/)
- [openclawdc — llama.cpp MoE Offload Flags Explained (29 Jul 2026)](https://openclawdc.com/blog/llama-cpp-moe-offload-flags-explained/)
- [Aliteq — `--n-cpu-moe`: Run a Big MoE Model on a Small GPU (Jul/Aug 2026)](https://aliteq.com/run-big-moe-model-small-gpu-n-cpu-moe-guide)
- [HuggingFace blog — Performant local MoE CPU inference with GPU acceleration in llama.cpp](https://huggingface.co/blog/Doctor-Shotgun/llamacpp-moe-offload-guide)
- [Compute Market — Nemotron 3 Nano Omni Hardware Guide, 16GB picks (13 May 2026)](https://www.compute-market.com/blog/nemotron-3-nano-omni-local-hardware-guide-2026)
- [Local AI Master — Qwen3.6-27B](https://localaimaster.com/models/qwen-3-6-27b)
- [InsiderLLM — Qwen 3.6 Complete Guide (14 Aug 2026)](https://insiderllm.com/guides/qwen-3-6-local-ai-guide/)
- [BenchLM — Best Local LLMs by VRAM Tier (15 Aug 2026)](https://benchlm.ai/best/local-llm)
- [OrcaRouter — Best Local LLM for Coding by VRAM (10 Aug 2026)](https://www.orcarouter.ai/blog/best-local-llm-for-coding)
- [Thunder Compute — NVIDIA RTX PRO 6000 Blackwell Pricing (Aug 2026)](https://www.thundercompute.com/blog/nvidia-rtx-pro-6000-pricing)
- [Tom's Hardware — NVIDIA doubles RTX PRO 6000 Blackwell MSRP to $16,000](https://www.tomshardware.com/pc-components/gpus/nvidia-doubles-rtx-pro-6000-blackwells-msrp-to-a-staggering-usd16-000-96gb-card-started-pre-orders-below-usd8-000-last-year)
- [Mercatus — NVIDIA H200 Server Price 2026 (8-GPU HGX)](https://www.mercatus-ai.com/blog/h200-server-price)
- [Mercatus — NVIDIA B200 Server Price 2026 (8-GPU HGX)](https://www.mercatus-ai.com/blog/b200-server-price)
- [IntuitionLabs — NVIDIA AI GPU Pricing Guide 2026](https://intuitionlabs.ai/articles/nvidia-ai-gpu-pricing-guide)
- [IntuitionLabs — NVIDIA DGX Spark Review: $4,699](https://intuitionlabs.ai/articles/nvidia-dgx-spark-review)
- [Dendro Logic — DGX Spark Concurrency Benchmark](https://dendro-logic.com/engineering/nvidia-dgx-spark-concurrency-benchmark/)

**Inference stacks**
- [runaihome — vLLM vs Ollama, real concurrency numbers (11 May 2026)](https://runaihome.com/blog/vllm-vs-ollama-when-each-wins-2026/)
- [SitePoint — Ollama vs vLLM Performance Benchmark 2026](https://www.sitepoint.com/ollama-vs-vllm-performance-benchmark-2026/)
- [Spheron — vLLM vs SGLang 2026: RadixAttention vs PagedAttention](https://www.spheron.network/blog/vllm-vs-sglang-2026/)
- [LeetLLM — vLLM vs SGLang vs TensorRT-LLM vs Ollama (2026)](https://leetllm.com/blog/llm-inference-engine-comparison-2026)
- [Local AI Master — TensorRT-LLM Setup Guide 2026](https://localaimaster.com/blog/tensorrt-llm-setup-guide)
- [SqueezeBits — Guided Decoding Performance on vLLM and SGLang](https://blog.squeezebits.com/guided-decoding-performance-vllm-sglang)
- [Red Hat Developer — Structured outputs in vLLM](https://developers.redhat.com/articles/2025/06/03/structured-outputs-vllm-guiding-ai-responses)
- [PromptQuorum — LM Studio & Ollama OpenAI API setup (2026)](https://www.promptquorum.com/local-llms/local-llm-openai-compatible-api)
- [vllm-project/vllm#41614 — Windows RTX 5070 Ti (Blackwell sm_120) setup notes](https://github.com/vllm-project/vllm/issues/41614)
- [vllm-project/vllm#37242 — RTX 5090 sm_120 + WSL2 2.7.0 CUDA graphs, benchmarks](https://github.com/vllm-project/vllm/issues/37242)
- [microsoft/WSL#40333 — Blackwell FP8 tensor cores not exposed via dxgkrnl](https://github.com/microsoft/WSL/issues/40333)

**Sovereign deployment, cost and compliance**
- [iternal.ai — How to Deploy an LLM On-Premise (2026): GPU Sizing & vLLM](https://iternal.ai/how-to-deploy-llm-on-premise)
- [Automata AI Sydney — The Real Cost of Self-Hosting an Open Source LLM in Australia (Jun 2026)](https://www.automataai.com.au/blog/the-real-cost-of-self-hosting-an-open-source-llm-in-australia)
- [Spheron — LLM Inference On-Premise vs GPU Cloud: 2026 Cost and Break-Even](https://www.spheron.network/blog/llm-inference-on-premise-vs-cloud/)
- [6clicks — Australia's defence AI policy and sovereign GRC](https://www.6clicks.com/resources/blog/australia-defence-ai-policy-sovereign-grc-compliance)
- [Interactive — Sovereign & Private AI in Australia](https://www.interactive.com.au/insights/sovereign-ai-australia/)
- [Brightlume — Data Residency for AI Workloads: Australian Compliance and Sovereign Deployment](https://brightlume.ai/blog/data-residency-ai-workloads-australian-compliance-sovereign-deployment)
- [IOTAI Australia — Sovereign AI, On-Premise LLM on DGX Spark](https://www.iotai.com.au/services/sovereign-ai)

**Adversarial verification patterns**
- [Cross-Model Adversarial Review (28 Mar 2026)](https://codex.danielvaughan.com/2026/03/28/cross-model-adversarial-review/)
- [Augment Code — Adversarial Code Review: Why the Maker Shouldn't Grade the Checker](https://www.augmentcode.com/guides/adversarial-code-review)
- [Zylos Research — Autonomous Code Review: Multi-Agent Approaches to PR Analysis (22 Apr 2026)](https://zylos.ai/research/2026-04-22-autonomous-code-review-multi-agent-pr-analysis/)