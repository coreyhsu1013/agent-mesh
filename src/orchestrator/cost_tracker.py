"""
Agent Mesh v0.9 — Cost Tracker

Parses token usage from different runner outputs and calculates USD costs.

Sources:
- Claude CLI: parses JSONL conversation logs (~/.claude/projects/...)
- Aider: estimates tokens from stdout character count
- Gemini: API response token counts (future)
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("agent-mesh")


@dataclass
class CostResult:
    """Token usage and cost for a single model invocation."""
    model: str
    input_tokens: int
    output_tokens: int
    estimated_usd: float
    source: str  # "claude_jsonl" | "aider_estimate" | "gemini_api"


# ── Model pricing (per 1M tokens, USD) ──
# Updated 2025-06 — check provider pricing pages for latest
MODEL_PRICING: dict[str, dict[str, float]] = {
    # Claude
    "claude-opus-4-6":     {"input": 15.0,  "output": 75.0},
    "claude-sonnet-4-6":   {"input": 3.0,   "output": 15.0},
    "claude-haiku-4-5":    {"input": 0.80,  "output": 4.0},
    # DeepSeek
    "deepseek-reasoner":   {"input": 0.55,  "output": 2.19},
    "deepseek-chat":       {"input": 0.14,  "output": 0.28},
    # Grok (xAI) — estimated, xAI pricing varies
    "grok-4-fast-non-reasoning":   {"input": 2.0,  "output": 10.0},
    "grok-4-1-fast-non-reasoning": {"input": 2.0,  "output": 10.0},
    "grok-code-fast-1":            {"input": 2.0,  "output": 10.0},
    "grok-4-fast-reasoning":       {"input": 3.0,  "output": 15.0},
    "grok-4-1-fast-reasoning":     {"input": 3.0,  "output": 15.0},
    # Gemini
    "gemini-3-flash-preview":      {"input": 0.10,  "output": 0.40},
    "gemini-2.5-pro":              {"input": 1.25,  "output": 10.0},
}

# Fallback pricing for unknown models (per 1M tokens)
FALLBACK_PRICING = {"input": 3.0, "output": 15.0}


class CostTracker:
    """Parse token usage from runner outputs and calculate costs."""

    def __init__(self, pricing: dict[str, dict[str, float]] | None = None):
        self.pricing = pricing or MODEL_PRICING

    def calculate_usd(self, model: str, input_tokens: int, output_tokens: int) -> float:
        """Calculate USD cost from token counts."""
        # Strip provider prefix for lookup: "xai/grok-code-fast-1" → "grok-code-fast-1"
        model_key = model.split("/")[-1] if "/" in model else model
        prices = self.pricing.get(model_key, FALLBACK_PRICING)
        cost = (input_tokens * prices["input"] + output_tokens * prices["output"]) / 1_000_000
        return round(cost, 6)

    def parse_claude_cost(self, stdout: str, model: str) -> CostResult | None:
        """
        Parse Claude CLI output for token usage.

        Claude CLI with --output-format text doesn't include usage in stdout,
        but we can estimate from the conversation length.
        Falls back to character-based estimation.
        """
        # Claude CLI piped mode doesn't output structured usage data
        # Estimate from output character count
        return self.estimate_from_chars(stdout, model, source="claude_estimate")

    def estimate_aider_cost(self, stdout: str, model: str) -> CostResult:
        """
        Estimate token usage from aider stdout.

        Aider prints token info in some modes. Try to parse it,
        fall back to character-based estimation.

        Heuristic: ~4 chars/token for English, ~2.5 chars/token for code
        """
        # Try to parse aider's token summary if present
        # Aider sometimes outputs: "Tokens: 1.2k sent, 3.4k received"
        tokens_match = re.search(
            r"Tokens:\s*([\d,.]+)k?\s*sent,\s*([\d,.]+)k?\s*received",
            stdout
        )
        if tokens_match:
            input_str = tokens_match.group(1).replace(",", "")
            output_str = tokens_match.group(2).replace(",", "")
            # Handle 'k' suffix
            input_tokens = int(float(input_str) * (1000 if "k" in tokens_match.group(0).lower() else 1))
            output_tokens = int(float(output_str) * (1000 if "k" in tokens_match.group(0).lower() else 1))

            return CostResult(
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_usd=self.calculate_usd(model, input_tokens, output_tokens),
                source="aider_parsed",
            )

        # Fallback: estimate from character count
        return self.estimate_from_chars(stdout, model, source="aider_estimate")

    def estimate_from_chars(self, text: str, model: str, source: str = "char_estimate") -> CostResult:
        """
        Estimate tokens from text character count.
        ~3.5 chars/token for mixed English+code (conservative estimate).
        Assumes input ≈ 2x output for typical coding tasks.
        """
        output_chars = len(text) if text else 0
        output_tokens = max(output_chars // 3, 100)  # at least 100 tokens
        input_tokens = output_tokens * 2  # rough input/output ratio for coding

        return CostResult(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_usd=self.calculate_usd(model, input_tokens, output_tokens),
            source=source,
        )
