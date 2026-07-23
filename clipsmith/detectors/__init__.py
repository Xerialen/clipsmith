"""clipsmith detectors — one module per OF THE WEEK category.

Each detector is runnable as a CLI (``python -m clipsmith.detectors.<name>``),
analyzes one demo via mvd-api, and emits candidates into the candidate store
(clipsmith.store) using the shared contract from docs/pipeline-spec.md.

Shared plumbing (mvd-api client, window math, candidate emission) lives in
clipsmith.detectors.common.
"""
