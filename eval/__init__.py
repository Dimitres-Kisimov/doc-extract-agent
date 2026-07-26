"""Evaluation harness for doc-extract-agent.

- :mod:`eval.make_dataset` deterministically generates the labelled set in
  ``eval/dataset/`` (committed, reproducible from a fixed seed).
- :mod:`eval.run_eval` runs the mock pipeline over the set and scores it,
  printing an ASCII report and writing ``eval/results.json``.
"""
