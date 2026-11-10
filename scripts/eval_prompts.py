"""Run the saved questions (tests/evals/cases.json).

Without --live: only the decisions made in code (fast path, period hints, reasoning effort, chart choices), no key needed.
With --live: also asks the real model (OPENROUTER_API_KEY, OPENROUTER_BASE_URL, OPENROUTER_MODEL in the environment) with recording stand-in
tools, and checks which tool it called and with what. Nothing is fetched.

    python scripts/eval_prompts.py                       # decisions made in code
    python scripts/eval_prompts.py --live                # and the model
    python scripts/eval_prompts.py --live --effort low   # force a reasoning level to see what it fixes
    python scripts/eval_prompts.py --live --repeat 5 --ids 3m-average,public-holidays   # how reliable is one case?

With --service: the same questions against the service's ten tools alone (lib/service; docs/mcp-service.md, step 2), scored against
tests/evals/service_cases.json: the model is offered only their names, descriptions and parameters, with a neutral system line and the
date, time and zone, and none of the bot's prompt, hints or fast path. Without --live it checks the expectations themselves.

    python scripts/eval_prompts.py --service             # every case has a service expectation, each one the tools take
    python scripts/eval_prompts.py --live --service      # and the model, with the ten tools alone
    python scripts/eval_prompts.py --live --service --instructions   # ... and the server's instructions (lib/service/instructions.py)
                                                                     # after the neutral line, as a client that passes them on would
"""

import asyncio
import os
import sys

from _common import parser  # noqa: E402  (also puts the repo on sys.path)

from tests import evalkit  # noqa: E402


async def main() -> int:
    ap = parser(__doc__)
    ap.add_argument("--live", action="store_true", help="ask the real model too")
    ap.add_argument("--ids", help="comma-separated case ids")
    ap.add_argument("--repeat", type=int, default=1, help="ask the model this many times per case")
    ap.add_argument("--effort", choices=["none", "low", "medium"], help="force this reasoning effort for every case")
    ap.add_argument("--service", action="store_true", help="the service's ten tools alone, scored against tests/evals/service_cases.json")
    ap.add_argument("--instructions", action="store_true", help="with --service: add the server's instructions to the system line")
    args = ap.parse_args()
    if args.instructions and not args.service:
        sys.exit("--instructions goes with --service")
    cases = evalkit.load_cases()
    if args.ids:
        wanted = set(args.ids.split(","))
        cases = [c for c in cases if c.id in wanted]
    client = model = None
    if args.live:
        from openai import AsyncOpenAI
        if not os.getenv("OPENROUTER_API_KEY"):
            sys.exit("--live needs OPENROUTER_API_KEY (and OPENROUTER_BASE_URL, OPENROUTER_MODEL) in the environment")
        client = AsyncOpenAI(api_key=os.environ["OPENROUTER_API_KEY"], base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"))
        model = os.getenv("OPENROUTER_MODEL", "deepseek/deepseek-v4.1-flash-1017")
    if args.service:
        failed = await evalkit.run_service(cases, evalkit.load_service_cases(), client, model, repeat=args.repeat, effort=args.effort,
                                           instructions=evalkit.service_instructions() if args.instructions else None)
        return 1 if failed else 0
    failed = 0
    for case in cases:
        problems = evalkit.deterministic(case)
        line = "ok" if not problems else "FAIL"
        if client and not problems:
            passes = 0
            for _ in range(args.repeat):
                calls, reply = await evalkit.run_live(case, client, model, args.effort)
                why = evalkit.check_calls(calls, case.expect)
                passes += not why
                problems += [f"  run: {w}" for w in why[:3]] if why else []
            line = f"{passes}/{args.repeat}" + ("" if passes == args.repeat else " FAIL")
        print(f"{line:>10}  {case.id}  ({case.ask})")
        for p in problems:
            print(f"            {p}")
        failed += bool(problems)
    print(f"\n{len(cases) - failed} of {len(cases)} cases pass")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
