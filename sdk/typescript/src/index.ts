import { spawn } from "node:child_process";
import { createInterface } from "node:readline";

export const JSONL_SCHEMA_VERSION = 1 as const;

export interface RecordEnvelope {
  schema_version: typeof JSONL_SCHEMA_VERSION;
  sequence: number;
  timestamp: string;
}

export interface SessionStartedRecord extends RecordEnvelope {
  type: "session.started";
  session_id: string;
  bundle: string;
  model: string;
}

export interface RuntimeEventRecord extends RecordEnvelope {
  type: "runtime.event";
  event: Record<string, unknown> & { kind: string };
}

export interface TurnCompletedRecord extends RecordEnvelope {
  type: "turn.completed";
  session_id: string;
  response: string;
  duration_ms: number;
}

export interface ErrorRecord extends RecordEnvelope {
  type: "error";
  session_id: string;
  error: string;
  error_type: string;
  duration_ms: number;
}

export type JsonlRecord =
  | SessionStartedRecord
  | RuntimeEventRecord
  | TurnCompletedRecord
  | ErrorRecord;

export interface AmplifierClientOptions {
  command?: readonly string[];
  cwd?: string;
  env?: Readonly<Record<string, string>>;
}

export interface StreamOptions {
  bundle?: string;
}

export interface RunResult {
  sessionId: string;
  bundle: string;
  model: string;
  response: string;
  durationMs: number;
  events: readonly RuntimeEventRecord[];
}

export class AmplifierSdkError extends Error {}

export class ProtocolError extends AmplifierSdkError {}

export class AmplifierProcessError extends AmplifierSdkError {
  readonly returnCode: number | null;
  readonly stderr: string;

  constructor(message: string, returnCode: number | null, stderr: string) {
    super(message);
    this.returnCode = returnCode;
    this.stderr = stderr;
  }
}

export class AmplifierRunError extends AmplifierSdkError {
  readonly record: ErrorRecord;

  constructor(record: ErrorRecord) {
    super(`${record.error_type}: ${record.error}`);
    this.record = record;
  }
}

function objectValue(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new ProtocolError(`${label} must be an object`);
  }
  return value as Record<string, unknown>;
}

function stringField(record: Record<string, unknown>, name: string): string {
  const value = record[name];
  if (typeof value !== "string") {
    throw new ProtocolError(`JSONL field ${JSON.stringify(name)} must be a string`);
  }
  return value;
}

function numberField(record: Record<string, unknown>, name: string): number {
  const value = record[name];
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new ProtocolError(`JSONL field ${JSON.stringify(name)} must be a number`);
  }
  return value;
}

export function parseRecord(line: string, expectedSequence: number): JsonlRecord {
  let decoded: unknown;
  try {
    decoded = JSON.parse(line);
  } catch (error) {
    throw new ProtocolError(`invalid JSONL: ${String(error)}`);
  }
  const record = objectValue(decoded, "JSONL line");
  if (record.schema_version !== JSONL_SCHEMA_VERSION) {
    throw new ProtocolError(
      `unsupported JSONL schema_version ${JSON.stringify(record.schema_version)}; ` +
        `expected ${JSONL_SCHEMA_VERSION}`,
    );
  }
  if (record.sequence !== expectedSequence) {
    throw new ProtocolError(
      `expected JSONL sequence ${expectedSequence}, got ${JSON.stringify(record.sequence)}`,
    );
  }
  stringField(record, "timestamp");
  const type = stringField(record, "type");
  switch (type) {
    case "session.started":
      stringField(record, "session_id");
      stringField(record, "bundle");
      stringField(record, "model");
      break;
    case "runtime.event": {
      const event = objectValue(record.event, "JSONL runtime event");
      stringField(event, "kind");
      break;
    }
    case "turn.completed":
      stringField(record, "session_id");
      stringField(record, "response");
      numberField(record, "duration_ms");
      break;
    case "error":
      stringField(record, "session_id");
      stringField(record, "error");
      stringField(record, "error_type");
      numberField(record, "duration_ms");
      break;
    default:
      throw new ProtocolError(`unknown JSONL record type ${JSON.stringify(type)}`);
  }
  return record as unknown as JsonlRecord;
}

export class AmplifierClient {
  private readonly command: readonly string[];
  private readonly cwd: string | undefined;
  private readonly env: Readonly<Record<string, string>>;

  constructor(options: AmplifierClientOptions = {}) {
    this.command = options.command ?? ["amplifier-newtui"];
    if (this.command.length === 0) {
      throw new TypeError("command must not be empty");
    }
    this.cwd = options.cwd;
    this.env = options.env ?? {};
  }

  async *stream(prompt: string, options: StreamOptions = {}): AsyncGenerator<JsonlRecord> {
    const [executable, ...prefixArgs] = this.command;
    if (executable === undefined) {
      throw new TypeError("command must not be empty");
    }
    const args = [...prefixArgs, "run", "--output-format", "jsonl"];
    if (options.bundle !== undefined && options.bundle !== "") {
      args.push("--bundle", options.bundle);
    }
    const child = spawn(executable, args, {
      cwd: this.cwd,
      env: { ...process.env, ...this.env },
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stderr = "";
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk: string) => {
      stderr += chunk;
    });
    const closed = new Promise<{ code: number | null }>((resolve, reject) => {
      child.once("error", reject);
      child.once("close", (code) => resolve({ code }));
    });
    child.stdin.end(prompt);

    const lines = createInterface({ input: child.stdout, crlfDelay: Infinity });
    let expectedSequence = 1;
    let terminalType: "turn.completed" | "error" | undefined;
    try {
      for await (const line of lines) {
        if (line.trim() === "") continue;
        if (terminalType !== undefined) {
          throw new ProtocolError(`record emitted after terminal ${terminalType}`);
        }
        const record = parseRecord(line, expectedSequence);
        expectedSequence += 1;
        if (record.type === "turn.completed" || record.type === "error") {
          terminalType = record.type;
        }
        yield record;
      }
      let result: { code: number | null };
      try {
        result = await closed;
      } catch (error) {
        throw new AmplifierProcessError(`could not run ${JSON.stringify(executable)}: ${String(error)}`, null, stderr);
      }
      if (terminalType === undefined) {
        throw new AmplifierProcessError(
          "CLI exited without a terminal JSONL record",
          result.code,
          stderr,
        );
      }
      if (terminalType === "turn.completed" && result.code !== 0) {
        throw new AmplifierProcessError(
          "CLI returned non-zero after turn.completed",
          result.code,
          stderr,
        );
      }
      if (terminalType === "error" && result.code === 0) {
        throw new ProtocolError("CLI returned zero after terminal error record");
      }
    } finally {
      lines.close();
      if (child.exitCode === null && child.signalCode === null) {
        child.kill();
      }
      try {
        await closed;
      } catch {
        // The primary protocol/process error is already being surfaced.
      }
    }
  }

  async run(prompt: string, options: StreamOptions = {}): Promise<RunResult> {
    let started: SessionStartedRecord | undefined;
    let completed: TurnCompletedRecord | undefined;
    let failed: ErrorRecord | undefined;
    const events: RuntimeEventRecord[] = [];
    for await (const record of this.stream(prompt, options)) {
      switch (record.type) {
        case "session.started":
          started = record;
          break;
        case "runtime.event":
          events.push(record);
          break;
        case "turn.completed":
          completed = record;
          break;
        case "error":
          failed = record;
          break;
      }
    }
    if (failed !== undefined) throw new AmplifierRunError(failed);
    if (started === undefined || completed === undefined) {
      throw new ProtocolError("successful run needs session.started and turn.completed");
    }
    return {
      sessionId: completed.session_id,
      bundle: started.bundle,
      model: started.model,
      response: completed.response,
      durationMs: completed.duration_ms,
      events,
    };
  }
}
