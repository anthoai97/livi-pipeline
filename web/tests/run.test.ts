import assert from "node:assert/strict";
import { afterEach, mock, test } from "node:test";
import { useRun } from "../src/lib/run";
import { buildRequest, DEFAULT_DRAFT } from "../src/lib/room";
import type { ClientTiming, ReadyVariant } from "../src/lib/types";

const request = buildRequest(DEFAULT_DRAFT);
const runId = "a".repeat(32);
const variant: ReadyVariant = {
  variant_index: 0, variant_id: runId, total_cost: 0, preview_url: null, selected_assets: [],
  render_manifest: { ...request, assets: {}, layout: {} },
};
const frame = (event: unknown) => new TextEncoder().encode(`data: ${JSON.stringify(event)}\n\n`);
const tick = () => new Promise<void>((resolve) => setImmediate(resolve));
afterEach(() => { useRun.getState().reset(); mock.restoreAll(); });

function liveStream() {
  let send: (event: unknown) => void = () => {};
  let aborted = false;
  const response = new Response(new ReadableStream<Uint8Array>({
    start(controller) { send = (event) => controller.enqueue(frame(event)); },
    cancel() { aborted = true; },
  }), { headers: { "Content-Type": "text/event-stream" } });
  return { response, send: (event: unknown) => send(event), aborted: () => aborted };
}

test("early cancellation keeps ready design and permits exactly one timing post, including Generate", async () => {
  const stream = liveStream();
  const posts: ClientTiming[] = [];
  mock.method(globalThis, "fetch", async (url, init) => {
    if (url === "/api/pipeline") return stream.response;
    assert.equal(url, `/api/runs/${runId}/client-timing`);
    posts.push(JSON.parse(init.body));
    throw new Error("Timing endpoint unavailable"); // Best effort; must not affect the design.
  });
  useRun.getState().start(request, "brief");
  stream.send({ type: "start", run_id: runId, nodes: [] });
  stream.send({ type: "variant_ready", data: { variant } });
  await tick();
  useRun.getState().cancel();
  await tick();
  assert.equal(stream.aborted(), true);
  assert.equal(useRun.getState().status, "cancelled");
  assert.deepEqual(useRun.getState().variants[0].ready, {
    ...variant,
    render_manifest: {
      room_area: request.room_area, room_vertices: request.room_vertices, room_doors: request.room_doors,
      room_windows: request.room_windows, wall_height: request.wall_height, assets: {}, layout: {},
    },
  });
  assert.ok(useRun.getState().variants[1].failed);
  const time = performance.now();
  const timing = { models: 2, failed_models: 1, loadedAt: time, displayedAt: time + 1 };
  useRun.getState().recordTiming(runId, variant.variant_id, "generate", timing);
  useRun.getState().recordTiming(runId, variant.variant_id, "chooser", timing);
  useRun.getState().recordTiming(runId, variant.variant_id, "studio", timing);
  await tick();
  assert.equal(posts.length, 1);
  assert.equal(posts[0].screen, "generate");
  assert.equal(posts[0].models, 2);
  assert.equal(posts[0].failed_models, 1);
  assert.ok(posts[0].received_ms <= posts[0].loaded_ms && posts[0].loaded_ms < posts[0].displayed_ms);
  assert.equal(useRun.getState().error, null);
});

test("replaced runs reject stale events and timing, while each current variant posts independently", async () => {
  const old = liveStream();
  const current = liveStream();
  let requests = 0;
  const posts: ClientTiming[] = [];
  mock.method(globalThis, "fetch", async (url, init) => {
    if (url === "/api/pipeline") return requests++ === 0 ? old.response : current.response;
    posts.push(JSON.parse(init.body));
    return new Response(null, { status: 204 });
  });
  useRun.getState().start(request, "old");
  useRun.getState().start(request, "current");
  const currentId = "b".repeat(32);
  current.send({ type: "start", run_id: currentId, nodes: [] });
  for (const index of [0, 1]) current.send({ type: "variant_ready", data: { variant: { ...variant, variant_id: `${currentId}_${index}`, variant_index: index } } });
  await tick();
  assert.equal(old.aborted(), true);
  assert.equal(useRun.getState().runId, currentId);
  const time = performance.now();
  const timing = { models: 0, failed_models: 0, loadedAt: time, displayedAt: time + 1 };
  useRun.getState().recordTiming(runId, variant.variant_id, "generate", timing);
  useRun.getState().recordTiming(currentId, variant.variant_id, "chooser", timing);
  useRun.getState().recordTiming(currentId, `${currentId}_0`, "chooser", timing);
  useRun.getState().recordTiming(currentId, `${currentId}_1`, "studio", timing);
  await tick();
  assert.deepEqual(posts.map((post) => post.variant_index), [0, 1]);
});

for (const terminal of [
  { type: "complete", data: { run_dir: runId, variants: [] } },
  { type: "error", code: "timeout", message: "Run deadline reached" },
]) {
  test(`${terminal.type} marks unavailable designs stopped without losing failure details`, async () => {
    const stream = liveStream();
    mock.method(globalThis, "fetch", async () => stream.response);
    useRun.getState().start(request, "brief");
    stream.send({ type: "start", run_id: runId, nodes: [] });
    stream.send({ type: "variant_failed", variant_index: 1, reason: "fit", message: "The table blocks the door", errors: [] });
    stream.send(terminal);
    await tick();
    assert.ok(useRun.getState().variants.every((item) => item.failed));
    assert.equal(useRun.getState().variants[1].failed?.message, "The table blocks the door");
    assert.equal(useRun.getState().status, terminal.type);
  });
}
