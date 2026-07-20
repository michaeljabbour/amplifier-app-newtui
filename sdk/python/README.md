# amplifier-newtui Python SDK

A dependency-free wrapper around the `amplifier-newtui` executable. It sends
the prompt over stdin and validates the CLI's versioned JSONL stream; the TUI,
CI, and SDK therefore share one runtime behavior surface.

```python
from amplifier_newtui_sdk import AmplifierClient

client = AmplifierClient(cwd="/path/to/project")

for record in client.stream("Review this repository"):
    if record["type"] == "runtime.event":
        print(record["event"]["kind"])

result = client.run("Summarize the changes", bundle="newtui")
print(result.response)
```

Install the `amplifier-newtui` CLI separately and ensure it is on `PATH`, then
install this package with `pip install ./sdk/python`. Pass `command=(...)` to
`AmplifierClient` when the executable lives elsewhere.
