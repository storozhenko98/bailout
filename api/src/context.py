"""Request budgets, independent of model quality and account rate limits.

No tokenizer tables are shipped in the CLI. With heterogeneous provider
tokenizers, use a conservative UTF-8 byte bound plus explicit framing overhead.
This intentionally switches early; bytes/4 is not a safe bound for code,
Unicode, random identifiers, or tool arguments. Actual usage calibrates health,
never weakens the admission bound. Tokenizer-specific counters can replace this
without changing the client protocol.
"""
import json
import math

MIN_CONTEXT = 32768
MAX_BODY = 4_000_000  # Transport/abuse ceiling, not a model's token window.
MAX_MESSAGES = 2048


def input_bound(messages, tools):
    content = json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    return len(content) + 1024 + 32 * len(messages)


def budget(model, endpoints, messages, tools, output=4096):
    """Choose only endpoints that fit, including the answer and 10% headroom."""
    prompt = input_bound(messages, tools)
    choices = endpoints or [model]
    fitting = []
    for endpoint in choices:
        windows = [model.get("context_length"), endpoint.get("context_length", model.get("context_length"))]
        cap = endpoint.get("max_completion_tokens")
        if cap is None:
            cap = model.get("max_output", output)
        input_limit = endpoint.get("max_prompt_tokens")
        if any(type(n) is not int or n < 1 for n in windows + [cap]):
            continue
        if input_limit is not None and (type(input_limit) is not int or input_limit < 1):
            continue
        window = min(windows)
        completion = min(output, cap)
        margin = max(2048, math.ceil(window * .10))
        input_limit = input_limit if input_limit is not None else window
        if window >= MIN_CONTEXT and completion >= 1024 and prompt + margin <= input_limit and prompt + completion + margin <= window:
            fitting.append((endpoint, window, completion, margin))
    if not fitting:
        return None
    # All permitted endpoints must fit the request; never leave a smaller
    # endpoint in a broker's fallback set.
    return {"input_tokens_upper_bound": prompt, "output_tokens": min(e[2] for e in fitting),
            "context_window": min(e[1] for e in fitting), "margin_tokens": max(e[3] for e in fitting),
            "endpoints": [e[0] for e in fitting] if endpoints else []}
