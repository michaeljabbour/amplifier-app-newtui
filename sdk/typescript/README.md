# amplifier-newtui TypeScript SDK

A zero-runtime-dependency wrapper around the `amplifier-newtui` executable.
Prompts travel over stdin; every stdout line is validated against JSONL schema
v1 so the TUI, CI, and SDK stay on one behavior surface.

```ts
import { AmplifierClient } from "@amplifier/newtui-sdk";

const client = new AmplifierClient({ cwd: "/path/to/project" });

for await (const record of client.stream("Review this repository")) {
  if (record.type === "runtime.event") console.log(record.event.kind);
}

const result = await client.run("Summarize the changes", { bundle: "newtui" });
console.log(result.response);
```

Install the `amplifier-newtui` CLI separately and ensure it is on `PATH`, then
install this package from `sdk/typescript`. The client spawns one CLI process
per run and never duplicates Amplifier runtime logic.
