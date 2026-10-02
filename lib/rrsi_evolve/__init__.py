# Part of rrsi-policy. Ports google-research/rrsi (Apache-2.0).
"""RRSI: regularized test-time harness evolution.

Domain-agnostic implementation of the two algorithms in the paper
"Regularized Recursive Self-Improvement of Agent Harnesses":

  Algorithm 1 (proposal side)   rrsi.loop.propose_round
  Algorithm 2 (selection side)  rrsi.selection.select_round

Everything benchmark-specific (how a harness is run, scored, rendered and
screened for leakage) lives behind the Domain interface in rrsi_evolve.domain
and is implemented under domains/<name>/adapter.py.
"""

__all__ = ["config", "schedule", "history", "components", "select",
           "evaluate", "calibrate", "propose", "critic", "analyst",
           "digester", "loop", "driver", "domain", "gitops", "llm"]
