"""Score a model on the AI reasoning core's golden corpus and red-team set (tests/ai_eval).

    python scripts/ai_eval.py                          # rules only, plus the hostile red-team set (no model)
    python scripts/ai_eval.py --provider ollama --model llama3.2:latest --out C:\path\results.json
    python scripts/ai_eval.py --provider ollama --limit 20 --skip-drafts

It talks only to a model server on this PC (127.0.0.1 / localhost) and never pulls a model. It writes nothing to any
AIME database: the analysis runs on unsaved rows and the policy engine only reads. The JSON written with --out holds
every case's raw readings, the scores and the gates (tests/ai_eval/runner.gates)."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aime.config import Settings  # noqa: E402
from aime.llm.ollama import OllamaProvider, is_local_url  # noqa: E402
from tests.ai_eval import corpus, redteam, runner  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--provider", choices=("none", "ollama"), default="none")
    ap.add_argument("--model", default="llama3.2:latest")
    ap.add_argument("--url", default="http://127.0.0.1:11434")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--limit", type=int, default=0, help="only the first N corpus cases")
    ap.add_argument("--skip-drafts", action="store_true", help="no model-written drafts (red-team model mode, benign drafts)")
    ap.add_argument("--benign-limit", type=int, default=15, help="model drafts for at most N ordinary replies (0: all)")
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)

    data = Path(tempfile.mkdtemp(prefix="aime-ai-eval-"))
    settings = Settings.load(env_file=data / "none.env", data_dir=data, database_url="", llm_provider="template", data_role="test")
    llm = None
    if args.provider == "ollama":
        if not is_local_url(args.url):
            print(f"Refused: {args.url} is not this PC", file=sys.stderr)
            return 2
        llm = OllamaProvider(args.url, args.model, args.timeout)
        if not llm.available():
            print(f"Ollama at {args.url} has no model {args.model}", file=sys.stderr)
            return 2

    def progress(i, n, row):
        bits = [f"[{i}/{n}] {row['id']}"]
        if "combined" in row:
            bits.append(f"truth={row['truth']['intent']} rules={row['rules']['intent']} "
                        f"model={(row['model'] or {}).get('intent')} -> {row['combined']['intent']} ok={row['structured_ok']} {row['ms']}ms")
        else:
            bits.append(f"{row.get('verdict')} {row.get('ms', 0)}ms")
        print(" ".join(bits), flush=True)

    cases = corpus.CASES[: args.limit] if args.limit else corpus.CASES
    t0 = time.time()
    rows = runner.run_corpus(llm, settings, cases, progress)
    scores = runner.score(rows)
    hostile = runner.run_redteam_hostile(settings)
    model_rt = benign = None
    if llm is not None and not args.skip_drafts:
        model_rt = runner.run_redteam_model(llm, settings, progress=progress)
        ordinary = [c for c in cases if not c.stop]
        benign = runner.run_drafts_benign(llm, settings, ordinary[: args.benign_limit] if args.benign_limit else ordinary, progress)
    result = {"provider": args.provider, "model": args.model if llm else None, "url": args.url if llm else None,
              "started": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t0)), "seconds": int(time.time() - t0),
              "corpus_cases": len(cases), "redteam_cases": len(redteam.CASES), "scores": scores,
              "redteam_hostile": hostile, "redteam_model": model_rt, "benign_drafts": benign,
              "gates": runner.gates(scores, hostile, model_rt), "cases": rows}
    print(json.dumps({k: result[k] for k in ("provider", "model", "seconds", "gates")}, indent=2))
    print(json.dumps(scores, indent=2)[:6000])
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
        print(f"Saved {args.out}")
    return 0 if result["gates"]["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
