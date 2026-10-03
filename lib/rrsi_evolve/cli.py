# Adapted from google-research/rrsi rrsi.py (Copyright 2026 Google LLC, Apache-2.0).
"""rrsi-evolve command line.

  rrsi-evolve [--repo R] [--config C] [--domain D] baseline [--job]      # evaluate H_0, seed the frontier
  rrsi-evolve calibrate [--jobs j1,j2]                                   # noise band delta
  rrsi-evolve round --t 3 [--dry-run]                                    # one round (Alg. 1 + Alg. 2)
  rrsi-evolve run [--start 0]                                            # driver: rounds until T
  rrsi-evolve readjudicate --t 3                                         # re-apply Alg. 2 to round 3's stored measurements
  rrsi-evolve reevaluate --t 3 [--variants A,B]                          # re-measure round 3's candidates, then re-adjudicate
  rrsi-evolve heldout --label champ [--ref <commit>] [--set heldout]
  rrsi-evolve smoke                                                      # liveness check of the incumbent
  rrsi-evolve status                                                     # frontier + history summary
  rrsi-evolve init [--force]                                             # scaffold rrsi.json + harness + example tasks
  rrsi-evolve mine [--project P] [--days N] [--lang ko] [--out F]        # task ideas from your local transcripts

Hyperparameters come from <repo>/rrsi.json; any of them can be overridden on
the command line (--T, --k, --m, --b-min, --b-max, --w, --m-draft, --delta,
--beta0, --beta1, --w-s, --w-c, --w-n, --n-prune).

Global options:

  --repo     the user's repository (default: `git rev-parse --show-toplevel`
             of the cwd, else the cwd itself)
  --config   the flat rrsi.json (default <repo>/rrsi.json)
  --domain   the domain name (default: the config's "domain" key, else
             "claudecode")
  --runs     the runs root (default <repo>/.rrsi/runs)
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rrsi_evolve import gitops as G                 # noqa: E402
from rrsi_evolve import llm                         # noqa: E402
from rrsi_policy import models                      # noqa: E402
from rrsi_evolve.config import RRSIConfig           # noqa: E402
from rrsi_evolve.domain import load_domain          # noqa: E402
from rrsi_evolve.driver import drive                # noqa: E402
from rrsi_evolve.loop import Run                    # noqa: E402
from rrsi_evolve.schedule import budget_table       # noqa: E402
from rrsi_evolve.scaffold import init               # noqa: E402
from rrsi_evolve import scope                       # noqa: E402

OVERRIDES = ["T", "k", "m", "b_min", "b_max", "w", "m_draft", "delta", "delta_z",
             "beta0", "beta1", "w_s", "w_c", "w_n", "n_prune", "eval_parallel"]

ENTRY = Path(__file__).resolve().parents[2] / "bin" / "rrsi-evolve"


def _default_repo() -> Path:
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                       text=True)
    if r.returncode == 0 and r.stdout.strip():
        return Path(r.stdout.strip())
    return Path.cwd()


def _exit_on_signal(signum, _frame):
    # SIGTERM / SIGHUP (a closed terminal) unwind like Ctrl-C, so cleanups
    # (temp dirs, the per-job state copy, child process groups) still run
    raise SystemExit(128 + signum)



EVAL_CMDS = {"smoke", "baseline", "round", "run", "heldout", "reevaluate"}


def _resolve_models(cfg, domain, repo: Path, run_dir: Path, cmd: str) -> None:
    """Resolve "inherit" model knobs to the model you use, and keep a run
    directory on the policy model its frontier was measured with: scores from
    two models are not comparable, so a later run on another model stops."""
    roles = {}
    for f in ("proposer_model", "analyst_model", "critic_model"):
        if models.is_inherit(getattr(cfg, f)):
            m, src = models.resolve(None, cwd=repo)
            setattr(cfg, f, m)
            roles[f] = f"{m} (from {src})"
        else:
            roles[f] = f"{getattr(cfg, f)} (from config)"
    if cmd in ("round", "run"):
        print("[rrsi] search roles: " + ", ".join(f"{k.split('_')[0]}={v}"
                                                  for k, v in roles.items()), file=sys.stderr)
    pm = getattr(domain, "policy_model", None)
    if pm is None or cmd not in EVAL_CMDS:
        return
    src = getattr(domain, "policy_model_source", "config")
    print(f"[rrsi] policy model: {pm} (from {src})", file=sys.stderr)
    pin = run_dir / "policy_model.json"
    if pin.is_file():
        try:
            pinned = json.loads(pin.read_text()).get("policy_model")
        except (OSError, ValueError, AttributeError):
            pinned = None
        if not isinstance(pinned, str) or not pinned:
            sys.exit(f"rrsi-evolve: {pin} is unreadable, so this run directory's policy "
                     f"model is unknown. Restore it (e.g. {{\"policy_model\": \"{pm}\"}} if "
                     f"that is the model the frontier was measured on) or start a new --runs "
                     f"directory.")
        if not models.same_model(pinned, pm):
            sys.exit(f"rrsi-evolve: this run directory was measured on {pinned!r}, but the "
                     f"policy model now resolves to {pm!r} (from {src}). Scores from two "
                     f"models are not comparable. Set \"policy_model\": \"{pinned}\" in "
                     f"rrsi.json to continue this run, or pass --runs <new dir> to start "
                     f"over on {pm!r}.")
        return
    if (run_dir / "frontier.json").is_file():
        print(f"[rrsi] warning: {run_dir} predates model pinning; cannot tell which "
              f"model its frontier was measured on", file=sys.stderr)
        return
    run_dir.mkdir(parents=True, exist_ok=True)
    pin.write_text(json.dumps({"policy_model": pm, "source": src}, indent=1) + "\n")

def main():
    import signal
    for sig in (signal.SIGTERM, getattr(signal, "SIGHUP", None)):
        # keep an inherited SIG_IGN (nohup) as it is
        if sig is not None and signal.getsignal(sig) is not signal.SIG_IGN:
            signal.signal(sig, _exit_on_signal)
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=None,
                    help="the user's repository (default: git toplevel of cwd, else cwd)")
    ap.add_argument("--config", default=None,
                    help="rrsi.json path (default <repo>/rrsi.json)")
    ap.add_argument("--domain", default=None,
                    help="domain name (default: the config's \"domain\" key, else claudecode)")
    ap.add_argument("--runs", default=None,
                    help="runs root (default <repo>/.rrsi/runs)")
    for name in OVERRIDES:
        typ = int if name in ("T", "k", "m", "b_min", "b_max", "w", "m_draft",
                              "n_prune", "eval_parallel") else float
        ap.add_argument("--" + name.replace("_", "-"), dest=name, type=typ, default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("baseline").add_argument("--job", default="base")
    sub.add_parser("calibrate").add_argument("--jobs", default="base")
    p = sub.add_parser("round")
    p.add_argument("--t", type=int, required=True)
    p.add_argument("--dry-run", action="store_true")
    sub.add_parser("run").add_argument("--start", type=int, default=0)
    sub.add_parser("readjudicate", help="re-run Algorithm 2 on a stored round after a "
                   "delta / weight change (no new evaluation)").add_argument("--t", type=int, required=True)
    p = sub.add_parser("reevaluate", help="re-measure a round's committed candidates after an "
                       "infrastructure failure, then re-adjudicate")
    p.add_argument("--t", type=int, required=True)
    p.add_argument("--variants", default="", help="e.g. A,B (default: all)")
    p = sub.add_parser("heldout")
    p.add_argument("--label", required=True)
    p.add_argument("--ref", default=None, help="commit/branch to evaluate (default incumbent)")
    p.add_argument("--set", default="heldout", choices=["heldout", "evolve"])
    sub.add_parser("smoke")
    sub.add_parser("status")
    p = sub.add_parser("init", help="scaffold rrsi.json, a starting harness and two "
                       "working example tasks (never overwrites without --force)")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("mine", help="find recurring failures in your local Claude Code "
                       "transcripts and suggest tasks (local only: no model call, nothing sent)")
    p.add_argument("--projects-dir", default=None,
                   help="transcript root (default $CLAUDE_CONFIG_DIR/projects, else ~/.claude/projects)")
    p.add_argument("--project", default=None,
                   help="only sessions whose working directory contains this text")
    p.add_argument("--days", type=int, default=None, help="only the last N days")
    p.add_argument("--examples", type=int, default=5, help="excerpts per failure type")
    p.add_argument("--lang", choices=["en", "ko"], default="en", help="report language")
    p.add_argument("--json", action="store_true", help="print JSON instead of Markdown")
    p.add_argument("--out", default=None, help="write the report to this file (mode 0600)")
    p.add_argument("--include-headless", action="store_true",
                   help="also read `claude -p` sessions (critics, trials, scripts)")
    p.add_argument("--exclude-session", action="append", default=None, metavar="ID",
                   help="skip this session id (repeatable)")
    args = ap.parse_args()

    if args.cmd == "mine":
        # needs no repo, config or domain
        from rrsi_evolve.mine import run_cli
        sys.exit(run_cli(args))

    # absolute paths: git, the checkers and the trials run with other cwds
    repo = (Path(args.repo) if args.repo else _default_repo()).resolve()
    config = (Path(args.config) if args.config else repo / "rrsi.json").resolve()

    if args.cmd == "init":
        if args.domain and args.domain != "claudecode":
            sys.exit(f"init scaffolds the claudecode domain only (got {args.domain!r}); "
                     f"pass --domain claudecode or drop the flag")
        init(repo, force=args.force)
        return

    raw = {}
    if config.is_file():
        raw = json.loads(config.read_text())
    if args.domain:
        name = args.domain
    else:
        name = raw.get("domain", "claudecode")
    cfg = RRSIConfig.load(config, **{k: getattr(args, k) for k in OVERRIDES})
    runs_root = (Path(args.runs) if args.runs else repo / ".rrsi" / "runs").resolve()
    llm.configure(env_passthrough=raw.get("env_passthrough", []), cwd=repo)

    domain = load_domain(name, repo, raw)
    _resolve_models(cfg, domain, repo, runs_root / name, args.cmd)
    if args.cmd in ("round", "run"):
        print(scope.banner(cfg.harness_scope, "harness_scope" in raw), file=sys.stderr)
    run = Run(domain, cfg, repo, runs_root)

    if args.cmd == "baseline":
        run.baseline(args.job)
        run.calibrate([args.job])
    elif args.cmd == "calibrate":
        run.calibrate([j.strip() for j in args.jobs.split(",") if j.strip()])
    elif args.cmd == "round":
        run.round(args.t, dry_run=args.dry_run)
    elif args.cmd == "readjudicate":
        run.readjudicate(args.t)
    elif args.cmd == "reevaluate":
        run.reevaluate(args.t, [v for v in args.variants.split(",") if v] or None)
    elif args.cmd == "run":
        drive(ENTRY, name, runs_root, cfg.T, args.start,
              extra_args=[a for k in OVERRIDES if getattr(args, k) is not None
                          for a in ("--" + k.replace("_", "-"), str(getattr(args, k)))],
              repo=repo, config=config)
    elif args.cmd == "heldout":
        ids = domain.heldout_ids() if args.set == "heldout" else domain.evolve_ids()
        if not ids:
            sys.exit(f"domain {name} has no {args.set} split")
        run.heldout(args.label, ids, ref=args.ref)
    elif args.cmd == "smoke":
        run.ensure_branch()
        wt = run.checkout("smoke", run.branch)
        ok, detail = domain.smoke(wt, run.runs, "smoke", domain.smoke_ids())
        print(json.dumps({"ok": ok, **detail}, indent=1))
        sys.exit(0 if ok else 1)
    elif args.cmd == "status":
        fr = run.frontier()
        print(json.dumps({k: v for k, v in fr.items() if k != "config"}, indent=1))
        print("b_t schedule:", budget_table(cfg.T, cfg.b_min, cfg.b_max,
                                            endpoint=cfg.b_anneal_endpoint))
        print(f"delta = {run.delta():.5f}")
        for r in run.history.render(60):
            print(json.dumps(r, ensure_ascii=False))
        print("evolve branch:", G.rev(repo, run.branch), "tree",
              run.harness_tree(run.branch))


if __name__ == "__main__":
    main()
