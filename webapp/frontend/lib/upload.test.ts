// Run with:  npm test   (compiles to .test-build and uses node's built-in test runner)
import assert from "node:assert/strict";
import { test } from "node:test";
import { formatBytes, formatDuration, newTracker, readJobReply, sample } from "./upload";

const MB = 1024 * 1024;

test("formatBytes", () => {
  assert.equal(formatBytes(0), "0 B");
  assert.equal(formatBytes(900), "900 B");
  assert.equal(formatBytes(1536), "1.50 KB");
  assert.equal(formatBytes(5 * MB), "5.00 MB");
  assert.equal(formatBytes(12.34 * MB), "12.3 MB");
  assert.equal(formatBytes(250 * MB), "250 MB");
  assert.equal(formatBytes(3 * 1024 * MB), "3.00 GB");
  assert.equal(formatBytes(-1), "");
  assert.equal(formatBytes(NaN), "");
});

test("formatDuration", () => {
  assert.equal(formatDuration(0), "a few seconds");
  assert.equal(formatDuration(4), "a few seconds");
  assert.equal(formatDuration(30), "30 seconds");
  assert.equal(formatDuration(60), "1 min");
  assert.equal(formatDuration(95), "1 min 35 s");
  assert.equal(formatDuration(3600), "1 h 0 min");
  assert.equal(formatDuration(5400), "1 h 30 min");
});

test("fraction and percentage of what has been sent", () => {
  const t = newTracker(0);
  assert.equal(sample(t, 0, 100 * MB, 10).fraction, 0);
  assert.equal(sample(t, 25 * MB, 100 * MB, 500).fraction, 0.25);
  assert.equal(sample(t, 100 * MB, 100 * MB, 1000).fraction, 1);
  assert.equal(sample(t, 120 * MB, 100 * MB, 1500).fraction, 1, "never above 100%");
});

test("an unknown total gives no fraction and no estimate", () => {
  const t = newTracker(0);
  const p = sample(t, 5 * MB, 0, 5000);
  assert.equal(p.fraction, null);
  assert.equal(p.secondsLeft, null);
  assert.equal(p.total, 0);
});

test("speed is measured from the data, not guessed", () => {
  const t = newTracker(0);
  const p = sample(t, 10 * MB, 100 * MB, 1000);          // 10 MB in 1 s
  assert.ok(Math.abs(p.bytesPerSecond! - 10 * MB) < 1);
  const q = sample(t, 20 * MB, 100 * MB, 2000);          // steady 10 MB/s
  assert.ok(Math.abs(q.bytesPerSecond! - 10 * MB) < 1);
});

test("speed smooths a stall rather than jumping to zero", () => {
  const t = newTracker(0);
  sample(t, 10 * MB, 100 * MB, 1000);
  const q = sample(t, 10 * MB, 100 * MB, 2000);          // nothing sent in the last second
  assert.ok(q.bytesPerSecond! > 0 && q.bytesPerSecond! < 10 * MB);
});

test("time left appears only once the estimate is steady", () => {
  let t = newTracker(0);
  assert.equal(sample(t, 10 * MB, 100 * MB, 500).secondsLeft, null, "10% sent but only half a second in: too early");
  t = newTracker(0);
  assert.equal(sample(t, 1 * MB, 1000 * MB, 2000).secondsLeft, null, "under 2% sent");
  t = newTracker(0);
  const p = sample(t, 50 * MB, 100 * MB, 5000);          // 10 MB/s, 50 MB left
  assert.ok(Math.abs(p.secondsLeft! - 5) < 0.01);
});

test("samples that are too close together do not distort the speed", () => {
  const t = newTracker(0);
  sample(t, 10 * MB, 100 * MB, 1000);
  const p = sample(t, 11 * MB, 100 * MB, 1050);          // a 1 MB burst in 50 ms (would read as 20 MB/s)
  assert.ok(Math.abs(p.bytesPerSecond! - 10 * MB) < 1, "ignored for speed");
  assert.equal(p.loaded, 11 * MB, "but the byte count is always current");
});

test("a finished upload has nothing left", () => {
  const t = newTracker(0);
  sample(t, 50 * MB, 100 * MB, 5000);
  const p = sample(t, 100 * MB, 100 * MB, 10000);
  assert.equal(p.secondsLeft, 0);
});

test("a good reply gives the job id", () => {
  assert.deepEqual(readJobReply(200, '{"id": 7, "status": "queued"}'), { id: 7, status: "queued" });
});

test("a refusal shows the server's own message", () => {
  assert.throws(() => readJobReply(400, '{"detail": "At least one PDF is required"}'), /At least one PDF is required/);
});

test("an error page from a proxy gives a plain message with the status", () => {
  assert.throws(() => readJobReply(502, "<html>Bad gateway</html>"), /server answered 502/);
  assert.throws(() => readJobReply(500, ""), /server answered 500/);
});

test("too large is said plainly", () => {
  assert.throws(() => readJobReply(413, "<html>Request Entity Too Large</html>"), /too large/);
});

test("a success reply that has no job id is not taken for success", () => {
  assert.throws(() => readJobReply(200, "{}"), /not understood/);
  assert.throws(() => readJobReply(200, "<html></html>"), /not understood/);
  assert.throws(() => readJobReply(200, '{"id": "7"}'), /not understood/);
});
