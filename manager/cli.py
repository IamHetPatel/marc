"""Run one mission from the terminal, no dashboard needed.

    python -m manager.cli "Check the test zone for anything blocking the exit" --auto-approve --mentor
    ROVER=mock DRONE=mock MOCK_TIME_SCALE=20 python -m manager.cli "..."        # faster than real time
"""
import argparse
import sys
import time

from .controller import Controller
from .runtime import build_controller

DEFAULT_MISSION = "Check the test zone for anything blocking the exit."


def run(ctrl: Controller, text: str, auto_approve: bool, mentor: bool, timeout_s: float, out=sys.stdout,
        scenario: str = "") -> str:
    store = ctrl.store
    ctrl.start()
    ctrl.set_mentor(mentor)
    if scenario:
        ctrl.load_scenario(scenario)
    else:
        ctrl.start_mission(text)
    seen_msgs, seen_steps = 0, {}
    deadline = time.time() + timeout_s
    approved = None
    while time.time() < deadline:
        st = store.to_json()
        for m in st["messages"][seen_msgs:]:
            print(f"[{m['role']:>6}] {m['text']}", file=out)
        seen_msgs = len(st["messages"])
        for s in st["plan"]:
            if seen_steps.get(s["id"]) != s["status"] and s["status"] != "pending":
                seen_steps[s["id"]] = s["status"]
                extra = (s["result"] or {}).get("error", "") if s["status"] == "failed" else ""
                print(f"   {s['id']:>4} {s['action']:<18} {s['status']:<8} {extra}", file=out)
        status = st["mission"]["status"]
        if status == "awaiting_approval" and approved != st["mission"]["plan_version"]:
            print(f"\nPlan v{st['mission']['plan_version']}"
                  f"{' (fallback)' if st['mission']['fallback'] else ''}:", file=out)
            for s in st["plan"]:
                print(f"   {s['id']:>4} {s['action']:<18} {s['args']}  # {s['why']}", file=out)
            if not auto_approve:
                input("\nPress Enter to approve (Ctrl-C to abort)... ")
            approved = st["mission"]["plan_version"]
            ctrl.approve(approved)
        if status in ("done", "failed", "stopped"):
            if status == "failed":
                print(f"FAILED: {st['mission']['error']}", file=out)
            return status
        time.sleep(0.1)
    ctrl.stop("CLI timeout")
    return "stopped"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("text", nargs="*", help="the mission, in plain words")
    ap.add_argument("--scenario", default="", help="run a scenario from scenarios/ instead of a typed mission")
    ap.add_argument("--auto-approve", action="store_true", help="skip the human approval step (testing only)")
    ap.add_argument("--mentor", action="store_true", help="unlock the drone (a mentor is present)")
    ap.add_argument("--timeout", type=float, default=900.0, help="give up and stop after this many seconds")
    args = ap.parse_args(argv)
    ctrl = build_controller()
    scenario = args.scenario or ("" if args.text else ctrl.cfg.scenario)
    status = run(ctrl, " ".join(args.text) or DEFAULT_MISSION, args.auto_approve, args.mentor, args.timeout,
                 scenario=scenario)
    ctrl.shutdown()
    print(f"\nmission {status}")
    return 0 if status == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
