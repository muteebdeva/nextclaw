name: Bug report
description: Report something that doesn't work as documented
labels: [bug]
body:
  - type: textarea
    id: what
    attributes:
      label: What happened?
      description: What you ran, what you expected, what actually happened.
    validations:
      required: true
  - type: textarea
    id: repro
    attributes:
      label: Minimal reproduction
      description: Exact commands to reproduce, starting from a fresh clone if possible.
    validations:
      required: true
  - type: input
    id: version
    attributes:
      label: Version
      description: Commit hash or tag, plus `python3 --version`.
    validations:
      required: true
  - type: textarea
    id: logs
    attributes:
      label: Logs
      description: Relevant output. Redact any API keys before pasting.
