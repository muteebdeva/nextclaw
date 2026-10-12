name: Feature request
description: Suggest an improvement to NextClaw
labels: [enhancement]
body:
  - type: textarea
    id: problem
    attributes:
      label: What problem does this solve?
      description: Describe the use case, not just the feature.
    validations:
      required: true
  - type: textarea
    id: proposal
    attributes:
      label: Proposed change
      description: How should it work? Which module would change?
  - type: checkboxes
    id: evidence
    attributes:
      label: Evidence standard
      options:
        - label: I understand new features need tests and must not claim unverified behavior
