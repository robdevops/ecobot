"""Run the saved questions (tests/evals/cases.json, scored against tests/evals/service_cases.json).

Without --live: no key needed. The decisions the bot still makes in code (the reasoning effort from the words, and its shortcuts: the
report, the gauges, "weather now", a named reading's chart and a period) are checked over cases.json, and every service expectation
is checked against the tools it names (each one they take).

With --live: also asks the real model (OPENROUTER_API_KEY, OPENROUTER_BASE_URL, OPENROUTER_MODEL in the environment) and checks
which tools it called and with what. Nothing is fetched. The bot's own request is the default: its system prompt (the persona, the
server's instructions, the date and time; lib/prompt.py), the ten tools as it offers them, its reasoning effort from the words, and a
question it answers in code scored by its fixed calls without asking the model. Each call is answered by the real tool over the
evals' frozen world (tests/evalworld.py: the fake station's archive, an AirGradient, forecast and pollen fakes, the clock at Tue 29 Sep
2026 14:05 Melbourne; the archive built once per run, about ten seconds, and each run of each case given fresh tools over its own
copy of it, so no case's calls change another's answers), wrapped as the bot wraps it, so the model sees real results, notes and
refusals and calls again until it replies (the turn ends on a complete result, as the bot's does; after 10 steps of calls the
model is made to answer). A case is scored over the whole turn: an expected call passes if any call that was not refused makes it (a
refused call later corrected passes), a forbidden tool fails in any round, refused or not, more than 8 calls fails ("ran away"), and
so does an empty reply, the Agent's fallback reply (the model's last answer was empty) or a turn that ends on a refused call with
none answered after it; each run's number of calls, refusals and the reply's first 200 characters are printed.

    python scripts/eval_prompts.py                       # the decisions made in code, and the service expectations
    python scripts/eval_prompts.py --live                # and the model, as the bot asks it
    python scripts/eval_prompts.py --live --effort low   # force a reasoning level to see what it fixes
    python scripts/eval_prompts.py --live --repeat 5 --ids 3m-average,public-holidays   # how reliable is one case?

With --service: the ten tools alone (docs/mcp-service.md 7): the model is offered only their names, descriptions and parameters, with a
neutral system line and the date, time and zone, and none of the bot's prompt or shortcuts; every call is answered "no data" (the
model may call again or reply, up to the Agent's 10 steps), and every call of the turn is scored (what the descriptions lead a model
to, with nothing real to react to). --instructions adds the server's instructions
(lib/service/instructions.py) after the neutral line, as a client that passes them on would. Without --live it checks the expectations
themselves.

    python scripts/eval_prompts.py --service             # every case has a service expectation, each one the tools take
    python scripts/eval_prompts.py --live --service      # and the model, with the ten tools alone
    python scripts/eval_prompts.py --live --service --instructions
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
    ap.add_argument("--service", action="store_true", help="the service's ten tools alone, with a neutral system line")
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
    expectations = evalkit.load_service_cases()
    if args.service:
        failed = await evalkit.run_service(cases, expectations, client, model, repeat=args.repeat, effort=args.effort,
                                           instructions=evalkit.service_instructions() if args.instructions else None)
        return 1 if failed else 0
    decided = 0
    for case in cases:
        if problems := evalkit.deterministic(case):
            decided += 1
            print(f"{'FAIL':>10}  {case.id}  ({case.ask})")
            for p in problems:
                print(f"            {p}")
    print(f"{len(cases) - decided} of {len(cases)} cases: the decisions made in code hold\n")
    if not client:
        failed = await evalkit.run_service(cases, expectations, repeat=args.repeat, effort=args.effort, bot=True)
        return 1 if failed or decided else 0
    from tests import evalworld
    async with evalworld.world() as world:   # (the archive built once; each run of each case gets fresh tools over a copy of it)
        failed = await evalkit.run_service(cases, expectations, client, model, repeat=args.repeat, effort=args.effort, bot=True,
                                           world=world)
    return 1 if failed or decided else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
