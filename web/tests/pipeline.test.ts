import assert from "node:assert/strict";
import { afterEach, test, mock } from "node:test";
import { pipeline } from "../src/lib/pipeline";
import { buildRequest, DEFAULT_DRAFT } from "../src/lib/room";
import type { PipelineEvent } from "../src/lib/types";

const request = buildRequest(DEFAULT_DRAFT);
const start = { type: "start", run_id: "a".repeat(32), nodes: [] };
const complete = { type: "complete", data: { run_dir: "a".repeat(32), variants: [] } };
const frame = (event: unknown) => `data: ${JSON.stringify(event)}\n\n`;
afterEach(() => mock.restoreAll());

function stream(text: string, bytewise = false) {
  const bytes = new TextEncoder().encode(text);
  return new Response(new ReadableStream<Uint8Array>({
    start(controller) {
      if (bytewise) for (const byte of bytes) controller.enqueue(Uint8Array.of(byte));
      else controller.enqueue(bytes);
      controller.close();
    },
  }), { headers: { "Content-Type": "text/event-stream; charset=utf-8" } });
}

test("POST reader handles split UTF-8, CRLF, comments, multiline data, and multiple frames", async () => {
  const failure = { type: "variant_failed", variant_index: 1, reason: "layout", message: "Pièce bloquée", errors: [] };
  const body = `: heartbeat\r\n\r\n${frame(start)}data: ${JSON.stringify(failure).replace(',"message"', ',\ndata: "message"')}\n\n${frame(complete)}`.replace(/(?<!\r)\n/g, "\r\n");
  mock.method(globalThis, "fetch", async (url, init) => {
    assert.equal(url, "/api/pipeline");
    assert.equal(init.method, "POST");
    assert.deepEqual(JSON.parse(init.body), request);
    return stream(body, true);
  });
  const events: PipelineEvent[] = [];
  await pipeline(request, new AbortController().signal, (event) => events.push(event));
  assert.deepEqual(events, [start, failure, complete]);
});

test("terminal event stops reading and preserves actual pipeline error text", async () => {
  let cancelled = false;
  const failure = { type: "error", message: "No candidates fit this room", code: "selection" };
  mock.method(globalThis, "fetch", async () => new Response(new ReadableStream({
    start(controller) { controller.enqueue(new TextEncoder().encode(frame(failure) + frame(complete))); },
    cancel() { cancelled = true; },
  }), { headers: { "Content-Type": "text/event-stream" } }));
  const events: PipelineEvent[] = [];
  await pipeline(request, new AbortController().signal, (event) => events.push(event));
  assert.deepEqual(events, [failure]);
  assert.equal(cancelled, true);
});

test("abort cancels a pending read and never delivers later frames", async () => {
  const abort = new AbortController();
  let cancelled = false;
  mock.method(globalThis, "fetch", async () => new Response(new ReadableStream({
    start(controller) { controller.enqueue(new TextEncoder().encode(frame(start))); },
    cancel() { cancelled = true; },
  }), { headers: { "Content-Type": "text/event-stream" } }));
  const events: PipelineEvent[] = [];
  const reading = pipeline(request, abort.signal, (event) => {
    events.push(event);
    queueMicrotask(() => abort.abort());
  });
  await assert.rejects(reading, { name: "AbortError" });
  assert.deepEqual(events, [start]);
  assert.equal(cancelled, true);
});

test("abort inside a callback stops the next buffered frame", async () => {
  const abort = new AbortController();
  mock.method(globalThis, "fetch", async () => stream(frame(start) + frame(complete)));
  const events: PipelineEvent[] = [];
  await assert.rejects(pipeline(request, abort.signal, (event) => { events.push(event); abort.abort(); }), { name: "AbortError" });
  assert.deepEqual(events, [start]);
});

for (const [name, response, message] of [
  ["HTTP failure", () => new Response("Service unavailable", { status: 503 }), /Service unavailable/],
  ["wrong content type", () => new Response("<html>Vite fallback</html>"), /event stream/],
  ["premature EOF", () => stream(frame(start)), /ended before/],
  ["truncated terminal frame", () => stream(`data: ${JSON.stringify(complete)}`), /ended before/],
  ["invalid JSON", () => stream("data: {broken}\n\n"), /JSON|property/],
  ["invalid variant index", () => stream(frame({ type: "variant_failed", variant_index: 3, reason: "x", message: "x", errors: [] })), /invalid event/],
] as const) {
  test(name, async () => {
    mock.method(globalThis, "fetch", async () => response());
    await assert.rejects(pipeline(request, new AbortController().signal, () => {}), message);
  });
}

test("CR-only framing flushes a terminal delimiter at EOF", async () => {
  mock.method(globalThis, "fetch", async () => stream(frame(complete).replaceAll("\n", "\r"), true));
  const events: PipelineEvent[] = [];
  await pipeline(request, new AbortController().signal, (event) => events.push(event));
  assert.deepEqual(events, [complete]);
});
