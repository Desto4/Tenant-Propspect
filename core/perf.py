"""Performance recording and cost estimation."""
import time as _time
from core.state import append_perf

_MODEL_PRICING = {
    # Anthropic
    "claude-opus-4-7":            (5.00,  25.00),
    "claude-opus-4-6":            (5.00,  25.00),
    "claude-sonnet-4-6":          (3.00,  15.00),
    "claude-haiku-4-5":           (1.00,   5.00),
    "claude-opus-4-5":            (5.00,  25.00),
    "claude-sonnet-4-5":          (3.00,  15.00),
    "claude-opus-4-1":            (15.00, 75.00),
    # Gemini
    "gemini-2.5-flash-preview-04-17": (0.075, 0.30),
    "gemini-2.5-pro-preview-05-06":   (1.25,  5.00),
    "gemini-2.0-flash":               (0.075, 0.30),
    "gemini-1.5-flash":               (0.075, 0.30),
    "gemini-1.5-pro":                 (1.25,  5.00),
    "gemini-3-flash-preview":         (0.075, 0.30),
    "gemini-3.1-pro-preview":         (1.25,  5.00),
    "gemini-3.1-flash-lite-preview":  (0.25,  1.50),
    # Perplexity
    "sonar-pro":           (3.00, 15.00),
    "sonar":               (1.00,  1.00),
    "sonar-reasoning-pro": (2.00,  8.00),
    "sonar-reasoning":     (1.00,  5.00),
    "sonar-deep-research": (2.00,  8.00),
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    price_in, price_out = _MODEL_PRICING.get(model, (1.00, 5.00))
    return round((input_tokens * price_in + output_tokens * price_out) / 1_000_000, 6)


def record_perf(
    provider: str,
    model: str,
    duration_ms: int,
    input_tokens: int,
    output_tokens: int,
    tool_calls: int,
    leads_found: int,
    success: bool,
) -> None:
    append_perf({
        "ts":            _time.time(),
        "provider":      provider,
        "model":         model,
        "duration_ms":   duration_ms,
        "input_tokens":  input_tokens,
        "output_tokens": output_tokens,
        "tool_calls":    tool_calls,
        "leads_found":   leads_found,
        "success":       success,
        "cost_usd":      estimate_cost(model, input_tokens, output_tokens),
    })
