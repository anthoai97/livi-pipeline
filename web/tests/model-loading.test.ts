import assert from "node:assert/strict";
import { test } from "node:test";
import { trackModels } from "../src/lib/model-loading";

test("cached models, failures, and empty URLs each settle one manifest instance", () => {
  const models = trackModels(["cached", "missing-url", "failed-glb"]);
  models.settle("cached", false);
  models.settle("cached", false); // React strict effects and cached loads must not count twice.
  models.settle("missing-url", true);
  assert.equal(models.snapshot().loadedAt, null);
  models.settle("failed-glb", true);
  const snapshot = models.snapshot();
  assert.equal(snapshot.models, 3);
  assert.equal(snapshot.settled, 3);
  assert.equal(snapshot.failed_models, 2);
  assert.equal(typeof snapshot.loadedAt, "number");
  models.settle("cached", true);
  models.settle("unrelated-model", false);
  assert.deepEqual(models.snapshot(), snapshot);
});

test("empty scenes settle immediately; prior manifests cannot settle a switched scene", () => {
  assert.equal(typeof trackModels([]).snapshot().loadedAt, "number");
  const previous = trackModels(["same-instance"]);
  const next = trackModels(["same-instance"]);
  previous.settle("same-instance", false);
  assert.equal(next.snapshot().loadedAt, null);
  next.settle("same-instance", true);
  assert.equal(previous.snapshot().failed_models, 0);
  assert.equal(next.snapshot().failed_models, 1);
});
