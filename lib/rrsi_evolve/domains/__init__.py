# Part of rrsi-policy. Ports google-research/rrsi (Apache-2.0).
"""Benchmark domains for rrsi-evolve.

Each domain lives in `<name>/adapter.py` and exports
`make_domain(repo, raw_cfg) -> Domain`; the harness's SKILL.md, PATTERNS.md
and rendering/brief helpers sit beside it. `rrsi_evolve.domain.load_domain`
is the only supported entry point.
"""
