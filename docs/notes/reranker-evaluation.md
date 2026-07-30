# Reranker evaluation: Llama Nemotron VL 1B v2 and Gemma

Date: 2026-07-15

## Decision

Do not add `nvidia/llama-nemotron-rerank-vl-1b-v2` to The Network now.

The current application has no Gemma (or any other) reranker integration to
replace. Its search path embeds the query, retrieves `top_k` sealed gists with
pgvector, and orders those rows by a cosine-similarity/recency blend. See
[`thenetwork/search/match.py`](../thenetwork/search/match.py) and the agent
tool's direct `match_memories(..., limit=top_k)` call in
[`thenetwork/agent/tools.py`](../thenetwork/agent/tools.py). `pyproject.toml`
has neither a reranker runtime nor an HTTP client/configuration for one.

If “the current Gemma reranker” means the usual
[`BAAI/bge-reranker-v2-gemma`](https://huggingface.co/BAAI/bge-reranker-v2-gemma),
it is a prospective baseline, not a dependency or deployed component in this
repository. That distinction matters: no measured head-to-head result exists
for this corpus, retrieval stage, or relevance definition.

The proposed NVIDIA model is a vision-language reranker. The Network stores
only short, sanitized text gists for cross-person matching; it stores no page
images, OCR layouts, tables, or slides. Its published multimodal advantage
therefore does not match the current problem. Its NIM deployment is also a
poor fit for the documented single-small-VPS deployment. Keep the current
pgvector ranking unless a text-only reranking experiment is separately
justified by a held-out, SEAL-safe relevance set.

## What the current path guarantees

`match_memories` selects only `memory_id`, opaque `person_id`, `gist`, and
similarity from SQL. Raw `memories.text` is not projected. A reranker must
consume exactly the same `gist` values after retrieval and must return only an
ordering/score keyed to the already-returned opaque memory/person identifiers.
It must never broaden the projection or query raw memory text. This is the
SEAL's structural boundary, documented in
[`docs/security.md`](security.md).

The current result's `similarity` is cosine similarity, rounded for the agent.
A reranker score is not interchangeable with it. Any future two-stage design
should either expose a separately named score or define a new documented
ordering contract; it must not silently relabel a cross-encoder logit as cosine
similarity.

## Published comparison

| Dimension | `BAAI/bge-reranker-v2-gemma` | `nvidia/llama-nemotron-rerank-vl-1b-v2` | Relevance here |
| --- | --- | --- | --- |
| Model type | Multilingual Gemma-2B-derived LLM reranker. It scores a text query/passage pair with a causal-LM `Yes` token. [BAAI model card](https://huggingface.co/BAAI/bge-reranker-v2-gemma) | Multimodal cross-encoder: SigLIP 2 400M vision encoder plus Llama 3.2 1B language model, about 1.7B parameters. It accepts text, image, or both. [NVIDIA model card](https://build.nvidia.com/nvidia/llama-nemotron-rerank-vl-1b-v2/modelcard) | Both can rank text pairs, but only the NVIDIA model carries a vision subsystem that the present schema cannot use. |
| Published quality | BAAI says it is suitable for multilingual ranking and publishes BEIR/MIRACL evaluations after reranking the top 100 candidates, but the card's displayed results are charts rather than a directly comparable numeric table. [BAAI evaluation](https://huggingface.co/BAAI/bge-reranker-v2-gemma#evaluation) | NVIDIA reports 73.98 average Recall@5 across its text pipeline (BEIR+TechQA, MIRACL, MLQA, MLDR), and visual-page Recall@5 of 76.12% text, 76.12% image, and 77.64% image+text. These are pipeline scores with NVIDIA's own embedding models, not a Gemma comparison. [NVIDIA results](https://build.nvidia.com/nvidia/llama-nemotron-rerank-vl-1b-v2/modelcard) | Do not infer that one model wins for The Network. The published evaluations use different candidate generators, top-k values, corpora, metrics, and (for NVIDIA's strongest claim) visual documents. |
| Text length | The published BAAI reference implementation builds query/passage prompts with a default maximum length of 1024 tokens. [BAAI usage](https://huggingface.co/BAAI/bge-reranker-v2-gemma#using-huggingface-transformers) | NVIDIA's supported NIM configuration has an 8192-token query/passage-pair limit and truncates a too-long pair from the passage when `truncate=END`. [NIM support matrix](https://docs.nvidia.com/nim/nemo-retriever/text-reranking/latest/support-matrix.html) | Network gists are short, so the longer NVIDIA context provides no demonstrated benefit. |
| Inference surface | Local Python/Transformers or FlagEmbedding `FlagLLMReranker`; it requires the model-specific prompt and score extraction. [BAAI usage](https://huggingface.co/BAAI/bge-reranker-v2-gemma#using-flagembedding) | NVIDIA-hosted NIM exposes a reranking endpoint with `{model, query, passages, truncate}`; a passage can include `text` and optional base64 image data. [NVIDIA API reference](https://docs.api.nvidia.com/nim/reference/nvidia-llama-nemotron-rerank-vl-1b-v2-infer) | Neither is drop-in. The repository needs an explicit two-stage interface, settings, retry/timeout policy, and score contract. |
| Published artifact / license | The repository is 10 GB of F32 weights, lists 3B parameters, and is Apache-2.0. [BAAI files and metadata](https://huggingface.co/BAAI/bge-reranker-v2-gemma/tree/main) | NVIDIA describes about 1.7B parameters. The model uses the NVIDIA Open Model License plus the Llama 3.2 Community License Agreement. [NVIDIA model card](https://build.nvidia.com/nvidia/llama-nemotron-rerank-vl-1b-v2/modelcard) | The smaller parameter count does not make the VL NIM smaller in deployment; its vision runtime dominates the published NIM footprint. Review licenses before either model is shipped. |

## Hardware and operational fit

For local **NIM** deployment, NVIDIA requires an x86 host with at least eight
CPU cores. Its broad-compatibility configuration for the VL model is FP16,
7.30 GB GPU memory, 3.10 GB disk, and an 8192-token limit. NVIDIA's optimized
profiles are validated on RTX PRO 6000 Blackwell Server Edition, B200,
H100-NVL, H100-80GB, and A100-SXM4-80GB; their published GPU-memory footprints
are about 24--25 GiB FP16 (and about 37 GiB FP8). See the
[NIM support matrix](https://docs.nvidia.com/nim/nemo-retriever/text-reranking/latest/support-matrix.html).

Thus a dedicated NVIDIA GPU with at least 8 GB free VRAM is the vendor-stated
minimum for the fallback VL NIM path, while a 24 GB-class GPU is the realistic
starting point for a supported/optimized local deployment. It is not a
reasonable service to add to the project's documented small VPS. The BAAI card
does not publish a deployment VRAM requirement; it does publish a 10 GB F32
artifact and a 3B-parameter model. Treat any lower-memory quantized variant as
a distinct artifact that needs its own quality and license review, not as a
property of the official model.

Calling NVIDIA's hosted endpoint avoids local GPU provisioning but forwards
the query and sealed gist candidates to a third party. This does not expose raw
memory text if the existing SQL projection is preserved, but it changes the
provider/data-processing boundary described in `docs/development.md`; it needs
an explicit privacy and retention review before production use.

## Conditions for a future experiment

Only run a reranker experiment after all of these are true:

1. Define a SEAL-safe, held-out set of `(query, gist candidates, desired
   ordering)` examples. It must contain no raw memory text or identity data.
2. Retrieve a fixed, larger candidate set first (for example top 20--50), then
   rerank only those sealed gists. Compare Recall@k, NDCG@k, latency, error
   handling, and introduction-quality outcomes against the current
   cosine/recency order.
3. Run both baselines on the same candidate set, with the same truncation and
   no image input. This is required for an actual Gemma-versus-Nemotron
   conclusion.
4. Preserve the SQL projection and add security tests that assert a reranker
   request contains only query text plus gists/opaque IDs, while model-visible
   results remain gists and opaque IDs.
5. Set a bounded timeout and a defined fail-closed or documented fallback
   ordering. Do not make introduction eligibility depend on an opaque external
   score without measuring the effect on consent requests.

No simulation was run for this evaluation.
