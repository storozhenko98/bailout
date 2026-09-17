const ORIGIN = 'https://openrouter.ai/api/v1';
const MAX_BODY = 512_000;
const MIN_UPTIME = 90;
const MAX_ATTEMPTS = 3;

export const BASH_TOOL = {
  type: 'function',
  function: {
    name: 'bash',
    description: 'Execute any Bash command on the user machine, without approval. Each call starts a fresh shell. Use workdir or explicit cd; shell variables do not persist. Use noninteractive commands and redirect background process output.',
    parameters: {
      type: 'object',
      properties: {
        command: { type: 'string', description: 'Bash source to execute.' },
        workdir: { type: 'string', description: 'Optional working directory; defaults to the session directory.' },
        timeout_ms: { type: 'integer', minimum: 1, maximum: 1_800_000, description: 'Default 120000 ms.' },
      },
      required: ['command'],
      additionalProperties: false,
    },
  },
};

class Failure extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

// Do not use Number(value) === 0: tiny positive prices can underflow to zero.
export function zero(value) {
  return (typeof value === 'number' && value === 0) ||
    (typeof value === 'string' && /^(?:0+(?:\.0*)?|\.0+)(?:e[+-]?\d+)?$/i.test(value));
}

export function freePricing(pricing) {
  return pricing !== null && typeof pricing === 'object' &&
    zero(pricing.prompt) && zero(pricing.completion) &&
    Object.entries(pricing).every(([key, value]) =>
      (typeof value === 'string' || key === 'discount') && zero(value));
}

export function freeModel(model) {
  return typeof model?.id === 'string' && /^[\w.-]+\/[\w.-]+:free$/.test(model.id) &&
    freePricing(model.pricing) && model.supported_parameters?.includes('tools') &&
    model.supported_parameters?.includes('tool_choice') &&
    model.context_length >= 32768;
}

export function usableEndpoint(endpoint, modelId) {
  return endpoint?.model_id === modelId && endpoint.status === 0 &&
    freePricing(endpoint.pricing) && endpoint.supported_parameters?.includes('tools') &&
    endpoint.supported_parameters?.includes('tool_choice') &&
    endpoint.supported_parameters?.includes('max_tokens') &&
    typeof endpoint.tag === 'string' && endpoint.tag.length > 0 &&
    (endpoint.context_length ?? 0) >= 32768 &&
    typeof endpoint.uptime_last_30m === 'number' &&
    endpoint.uptime_last_30m >= MIN_UPTIME && endpoint.uptime_last_30m <= 100 &&
    (endpoint.uptime_last_5m == null || endpoint.uptime_last_5m >= MIN_UPTIME);
}

// Transparent preference heuristic, NOT a benchmark or a claim of measured ability.
export function ability(model) {
  const text = `${model.id} ${model.description ?? ''}`.toLowerCase();
  let score = 20;
  const reasons = ['native tools'];
  if (/coding agent|agentic coding|coding model/.test(text)) { score += 40; reasons.push('coding specialist'); }
  else if (/coding|programming|code generation/.test(text)) { score += 20; reasons.push('coding'); }
  if (/reasoning/.test(text)) { score += 10; reasons.push('reasoning'); }
  if (/terminal-bench|swe-bench/.test(text)) { score += 10; reasons.push('coding evaluation mentioned'); }
  if (model.context_length >= 128000) score += 5;
  if (/(?:mini|small|nano|\bxs\b)/.test(model.id)) score -= 5;
  if (/advises against.*coding|not (?:suited|recommended).*coding/.test(text)) { score -= 60; reasons.push('coding discouraged'); }
  return { score, reasons };
}

function json(data, status = 200) {
  return Response.json(data, { status, headers: { 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' } });
}

async function boundedJSON(response, maximum) {
  if (!response.body) throw new Failure(502, 'Empty upstream response.');
  const reader = response.body.getReader();
  const chunks = [];
  let size = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > maximum) throw new Failure(413, 'Request or response is too large. Start a new session.');
      chunks.push(value);
    }
  } finally { await reader.cancel(); }
  const bytes = new Uint8Array(size);
  let at = 0;
  for (const chunk of chunks) { bytes.set(chunk, at); at += chunk.length; }
  try { return JSON.parse(new TextDecoder().decode(bytes)); }
  catch { throw new Failure(502, 'Invalid JSON response.'); }
}

async function metadata(path, fetcher) {
  let response;
  try {
    response = await fetcher(ORIGIN + path, {
      headers: { Accept: 'application/json', 'Cache-Control': 'no-cache, no-store' },
      cache: 'no-store', signal: AbortSignal.timeout(12000),
    });
  } catch { throw new Failure(503, 'Cannot verify live free pricing. No inference was sent.'); }
  if (!response.ok) throw new Failure(503, 'Cannot verify live free pricing. No inference was sent.');
  return boundedJSON(response, 8_000_000);
}

async function models(fetcher) {
  const result = await metadata('/models', fetcher);
  if (!Array.isArray(result.data)) throw new Failure(503, 'Model catalog unavailable.');
  return result.data.filter(freeModel).sort((a, b) => ability(b).score - ability(a).score || a.id.localeCompare(b.id));
}

async function endpoints(model, fetcher) {
  const result = await metadata(`/models/${model.id}/endpoints`, fetcher);
  if (result.data?.id !== model.id || !Array.isArray(result.data.endpoints)) return [];
  return result.data.endpoints.filter(e => usableEndpoint(e, model.id));
}

function publicModel(model, live) {
  const ranking = ability(model);
  return {
    id: model.id, name: model.name, context_length: model.context_length,
    score: ranking.score, reasons: ranking.reasons,
    available: live.length > 0,
    uptime: live.length ? Math.max(...live.map(e => e.uptime_last_30m)) : null,
    providers: live.length,
  };
}

export function validateInput(input) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) throw new Failure(400, 'Expected a JSON object.');
  // Construct requests ourselves. No arbitrary OpenRouter routing/plugins/BYOK fields.
  if (Object.keys(input).some(k => !['model', 'messages'].includes(k))) throw new Failure(400, 'Only model and messages are accepted.');
  if (input.model != null && input.model !== 'auto' && (typeof input.model !== 'string' || !/^[\w.-]+\/[\w.-]+:free$/.test(input.model))) {
    throw new Failure(400, 'Choose auto or an explicit :free model.');
  }
  if (!Array.isArray(input.messages) || !input.messages.length || input.messages.length > 256) throw new Failure(400, 'Expected 1–256 messages.');
  const pending = new Set();
  for (const m of input.messages) {
    if (!m || !['system', 'user', 'assistant', 'tool'].includes(m.role)) throw new Failure(400, 'Invalid message role.');
    if (Object.keys(m).some(k => !['role', 'content', 'tool_calls', 'tool_call_id', 'reasoning_details'].includes(k))) throw new Failure(400, 'Unsupported message field.');
    if (m.content !== null && typeof m.content !== 'string') throw new Failure(400, 'Text-only messages are required.');
    if (m.role === 'tool') {
      if (!pending.delete(m.tool_call_id)) throw new Failure(400, 'Unmatched tool result.');
    } else if (pending.size) throw new Failure(400, 'Missing tool results.');
    if (m.tool_calls != null) {
      if (m.role !== 'assistant' || !Array.isArray(m.tool_calls) || !m.tool_calls.length || m.tool_calls.length > 16) throw new Failure(400, 'Invalid tool calls.');
      for (const call of m.tool_calls) {
        if (call?.type !== 'function' || call.function?.name !== 'bash' || typeof call.function.arguments !== 'string' || typeof call.id !== 'string' || !call.id || pending.has(call.id)) throw new Failure(400, 'Only valid bash tool calls are accepted.');
        pending.add(call.id);
      }
    }
    if (m.reasoning_details != null && (m.role !== 'assistant' || !Array.isArray(m.reasoning_details))) throw new Failure(400, 'Invalid reasoning details.');
  }
  if (pending.size) throw new Failure(400, 'Missing tool results.');
  return { model: input.model ?? 'auto', messages: input.messages };
}

export function completionBody(model, live, messages) {
  const caps = live.map(e => e.max_completion_tokens).filter(n => Number.isFinite(n) && n > 0);
  return {
    model: model.id,
    messages,
    tools: [BASH_TOOL],
    tool_choice: needsTool(messages) ? 'required' : 'auto',
    stream: false,
    max_tokens: Math.min(8192, ...caps),
    provider: {
      only: [...new Set(live.map(e => e.tag))],
      allow_fallbacks: false,
      require_parameters: true,
      max_price: { prompt: 0, completion: 0, request: 0, image: 0 },
    },
  };
}

function needsTool(messages) {
  const lastUser = messages.findLastIndex(m => m.role === 'user');
  return !messages.slice(lastUser + 1).some(m => m.role === 'tool');
}

function checkedMessage(result, requireTool) {
  // The request cap prevents spend. This is an additional audit, not a refund mechanism.
  if (result.usage?.cost != null && !zero(result.usage.cost)) throw new Failure(502, 'Upstream reported nonzero cost. Stopped; investigate the provider.');
  const choice = result.choices?.[0];
  const m = choice?.message;
  if (choice?.finish_reason === 'length') throw new Failure(502, 'Model response was truncated; no commands were executed. Try a smaller task or another model.');
  if (!m || m.role !== 'assistant' || (m.content != null && typeof m.content !== 'string')) throw new Failure(502, 'Malformed model response.');
  const message = { role: 'assistant', content: m.content ?? null };
  if (requireTool && !m.tool_calls?.length) throw new Failure(502, 'Model skipped the required Bash call. No work was performed. Try another free model.');
  if (m.tool_calls?.length) {
    if (!Array.isArray(m.tool_calls) || m.tool_calls.length > 16) throw new Failure(502, 'Invalid model tool calls.');
    const ids = new Set();
    for (const call of m.tool_calls) {
      if (call?.type !== 'function' || call.function?.name !== 'bash' || typeof call.function.arguments !== 'string' || typeof call.id !== 'string' || !call.id || ids.has(call.id)) throw new Failure(502, 'Model requested an invalid tool. No commands were executed.');
      ids.add(call.id);
      let args;
      try { args = JSON.parse(call.function.arguments); } catch { throw new Failure(502, 'Malformed Bash arguments. No commands were executed.'); }
      if (!args || typeof args.command !== 'string' || !args.command.trim() || (args.workdir != null && typeof args.workdir !== 'string') || (args.timeout_ms != null && (!Number.isInteger(args.timeout_ms) || args.timeout_ms < 1 || args.timeout_ms > 1_800_000))) throw new Failure(502, 'Invalid Bash arguments. No commands were executed.');
    }
    message.tool_calls = m.tool_calls.map(c => ({ id: c.id, type: 'function', function: { name: 'bash', arguments: c.function.arguments } }));
  } else if (!m.content?.trim()) throw new Failure(502, 'Model returned no text or commands. Try another model.');
  if (Array.isArray(m.reasoning_details)) message.reasoning_details = m.reasoning_details;
  return message;
}

async function complete(input, env, fetcher) {
  if (!env.OPENROUTER_API_KEY) throw new Failure(503, 'The server key has not been configured.');
  const catalog = await models(fetcher); // ALWAYS fresh; no cached eligibility, even for a selected model.
  const candidates = input.model === 'auto' ? catalog.slice(0, 35) : catalog.filter(m => m.id === input.model);
  if (!candidates.length) throw new Failure(503, 'Selected model is not currently verified free with tool support.');
  let attempts = 0;
  let lastStatus = 503;
  for (let model of candidates) {
    if (attempts > 0) {
      // A fallback is another model request: recheck the catalog as well as endpoints.
      model = (await models(fetcher)).find(m => m.id === model.id);
      if (!model) continue;
    }
    let live;
    try { live = await endpoints(model, fetcher); } catch { continue; }
    if (!live.length) continue;
    if (attempts++ >= MAX_ATTEMPTS) break;
    let response;
    try {
      response = await fetcher(`${ORIGIN}/chat/completions`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${env.OPENROUTER_API_KEY}`,
          'Content-Type': 'application/json',
          'HTTP-Referer': 'https://github.com/storozhenko98/bailout',
          'X-OpenRouter-Title': 'bailout',
        },
        body: JSON.stringify(completionBody(model, live, input.messages)),
        signal: AbortSignal.timeout(90000),
      });
    } catch { throw new Failure(504, 'Inference timed out. Retry or choose another free model.'); }
    if (!response.ok) {
      lastStatus = response.status;
      await response.body?.cancel();
      if ([401, 402, 403].includes(response.status)) throw new Failure(503, 'OpenRouter rejected the server key or account policy. No paid fallback was attempted.');
      if (![404, 408, 429, 500, 502, 503, 504].includes(response.status)) break;
      continue;
    }
    const result = await boundedJSON(response, 2_000_000);
    if (result.error) {
      lastStatus = Number(result.error.code) || 502;
      if ([429, 500, 502, 503].includes(lastStatus)) continue;
      throw new Failure(502, 'OpenRouter could not complete this request.');
    }
    return json({ model: model.id, message: checkedMessage(result, needsTool(input.messages)), usage: { cost: result.usage?.cost ?? null }, checked_at: new Date().toISOString() });
  }
  throw new Failure(lastStatus === 429 ? 429 : 503, lastStatus === 429
    ? 'Free-model quota or capacity exhausted. Try later; paid models are never used.'
    : 'No healthy, verified-free provider could complete this request. Try later or select another free model.');
}

export async function handle(request, env, fetcher = fetch) {
  try {
    const url = new URL(request.url);
    if (request.method === 'GET' && url.pathname === '/health') return json({ ok: true, service: 'bailout', free_only: true, configured: Boolean(env.OPENROUTER_API_KEY) });
    if (request.method === 'GET' && url.pathname === '/') return json({ name: 'bailout', repository: 'https://github.com/storozhenko98/bailout', install: 'curl -fsSL https://raw.githubusercontent.com/storozhenko98/bailout/main/install.sh | bash' });
    if (request.method === 'GET' && url.pathname === '/install.sh') return Response.redirect('https://raw.githubusercontent.com/storozhenko98/bailout/main/install.sh', 302);
    if (!['/v1/models', '/v1/chat'].includes(url.pathname)) return json({ error: 'Not found.' }, 404);
    if ((url.pathname === '/v1/models' && request.method !== 'GET') || (url.pathname === '/v1/chat' && request.method !== 'POST')) return json({ error: 'Method not allowed.' }, 405);
    if (env.IP_LIMIT && !(await env.IP_LIMIT.limit({ key: request.headers.get('CF-Connecting-IP') ?? 'local' })).success) throw new Failure(429, 'Too many requests. Try again in a minute.');
    if (url.pathname === '/v1/models') {
      const catalog = await models(fetcher);
      // Sequential: stay below Workers concurrent connection limits and free-plan subrequest budget.
      const data = [];
      for (const model of catalog.slice(0, 35)) {
        let live = [];
        try { live = await endpoints(model, fetcher); } catch { /* mark unavailable, never assume free */ }
        data.push(publicModel(model, live));
      }
      data.sort((a, b) => Number(b.available) - Number(a.available) || b.score - a.score || a.id.localeCompare(b.id));
      return json({ models: data, default: data.find(m => m.available)?.id ?? null, ranking: 'coding metadata heuristic, not benchmark scores', checked_at: new Date().toISOString() });
    }
    if (env.SHARED_LIMIT && !(await env.SHARED_LIMIT.limit({ key: 'inference' })).success) throw new Failure(429, 'Shared free capacity is busy. Try again in a minute.');
    if (Number(request.headers.get('Content-Length')) > MAX_BODY) throw new Failure(413, 'Session is too large. Start a new session.');
    let body;
    try { body = await boundedJSON(request, MAX_BODY); }
    catch (e) { throw new Failure(e.status === 413 ? 413 : 400, e.status === 413 ? e.message : 'Invalid JSON request.'); }
    return await complete(validateInput(body), env, fetcher);
  } catch (error) {
    // Do not reflect upstream error text: it can contain credentials or prompt excerpts.
    return json({ error: error instanceof Failure ? error.message : 'Service unavailable. Please retry later.' }, error instanceof Failure ? error.status : 503);
  }
}

export default { fetch(request, env) { return handle(request, env); } };
