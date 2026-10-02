# Part of rrsi-policy. Ports google-research/rrsi (Apache-2.0).
"""Claude Code domain: the frozen policy is Claude Code headless (`claude -p`)
run in a fresh workspace under the candidate's project configuration.

Modules (loaded by `rrsi_evolve.domain.load_domain`, never by importing this
package):

  adapter.py   ClaudeCodeDomain + make_domain(repo, raw_cfg)
  render.py    stream-jsonl transcript -> the rendered trace the roles read
  briefs.py    ANALYST / DIGESTER / PROPOSER / CRITIC paragraphs
  SKILL.md     the proposer constitution (loaded by `constitution()`)
  PATTERNS.md  the pattern library (loaded by `constitution()`)
"""
