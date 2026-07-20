import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  AmplifierClient,
  AmplifierRunError,
  ProtocolError,
  type JsonlRecord,
} from "../src/index.js";

function records(): JsonlRecord[] {
  return [
    {
      schema_version: 1,
      sequence: 1,
      timestamp: "2026-01-01T00:00:00Z",
      type: "session.started",
      session_id: "s1",
      bundle: "newtui",
      model: "test-model",
    },
    {
      schema_version: 1,
      sequence: 2,
      timestamp: "2026-01-01T00:00:01Z",
      type: "runtime.event",
      event: { kind: "prompt_submit", session_id: "s1" },
    },
    {
      schema_version: 1,
      sequence: 3,
      timestamp: "2026-01-01T00:00:02Z",
      type: "turn.completed",
      session_id: "s1",
      response: "replaced",
      duration_ms: 12.5,
    },
  ];
}

async function fakeCli(payload: JsonlRecord[], exitCode = 0): Promise<{ dir: string; script: string }> {
  const dir = await mkdtemp(join(tmpdir(), "newtui-sdk-"));
  const script = join(dir, "fake-cli.mjs");
  await writeFile(
    script,
    `let prompt = "";
for await (const chunk of process.stdin) prompt += chunk;
console.error("diagnostic");
const records = ${JSON.stringify(payload)};
for (const record of records) {
  if (record.type === "turn.completed") record.response = prompt;
  console.log(JSON.stringify(record));
}
process.exitCode = ${exitCode};
`,
  );
  return { dir, script };
}

test("run sends the prompt over stdin and collects typed events", async () => {
  const fixture = await fakeCli(records());
  try {
    const client = new AmplifierClient({ command: [process.execPath, fixture.script] });
    const result = await client.run("private prompt\nwith newline");
    assert.equal(result.response, "private prompt\nwith newline");
    assert.equal(result.sessionId, "s1");
    assert.equal(result.bundle, "newtui");
    assert.deepEqual(result.events.map((record) => record.event.kind), ["prompt_submit"]);
  } finally {
    await rm(fixture.dir, { recursive: true, force: true });
  }
});

test("stream rejects sequence drift", async () => {
  const payload = records();
  payload[1] = { ...payload[1]!, sequence: 9 };
  const fixture = await fakeCli(payload);
  try {
    const client = new AmplifierClient({ command: [process.execPath, fixture.script] });
    await assert.rejects(async () => {
      for await (const _record of client.stream("hi")) {
        // Consume the stream.
      }
    }, (error: unknown) => error instanceof ProtocolError && /sequence 2/.test(error.message));
  } finally {
    await rm(fixture.dir, { recursive: true, force: true });
  }
});

test("run raises the structured terminal error", async () => {
  const payload: JsonlRecord[] = [
    {
      schema_version: 1,
      sequence: 1,
      timestamp: "2026-01-01T00:00:00Z",
      type: "error",
      session_id: "",
      error: "offline",
      error_type: "RuntimeError",
      duration_ms: 1,
    },
  ];
  const fixture = await fakeCli(payload, 1);
  try {
    const client = new AmplifierClient({ command: [process.execPath, fixture.script] });
    await assert.rejects(
      client.run("hi"),
      (error: unknown) =>
        error instanceof AmplifierRunError && error.record.error === "offline",
    );
  } finally {
    await rm(fixture.dir, { recursive: true, force: true });
  }
});
