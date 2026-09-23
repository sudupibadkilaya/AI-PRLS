#!/usr/bin/env bash
# Start the local LLM server for AI-PRLS.
#
# GPU plan for 3x ~40GB cards:
#   GPU 0 + GPU 1 -> vLLM serving Qwen2.5-14B-Instruct (tensor parallel = 2)
#   GPU 2         -> free (embeddings if AIPRLS_EMBED_DEVICE=cuda:2, headroom,
#                    or a second model for A/B pilots)
#
# The 14B model in bf16 needs ~28GB for weights; TP=2 spreads that across two
# cards and leaves room for a 16k context and healthy batching. No fine-tuning
# is used anywhere — prompts + RAG carry the pedagogy.
set -euo pipefail
MODEL="${AIPRLS_LLM_MODEL:-Qwen/Qwen2.5-14B-Instruct}"
CUDA_VISIBLE_DEVICES=0 vllm serve "$MODEL" \
  --tensor-parallel-size 1 \
  --max-model-len 16384 \
  --gpu-memory-utilization 0.90 \
  --port 8001
