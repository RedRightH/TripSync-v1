"""Gate runner: build in blocks, prove each block alone, only then integrate.

    python -m tripsync.gates --list
    python -m tripsync.gates --block gate0:clock        # one block, no prerequisites
    python -m tripsync.gates --gate 1                    # gates 0 and 1, in order

Rules enforced here (and tested in tests/meta/test_gates_runner.py):
  1. A gate's blocks run first. Its integration test runs only if every block is green.
  2. A gate that is not green blocks every later gate (status BLOCKED, never run).
  3. Running gate N always re-runs gates 0..N-1, so an old gate cannot silently rot.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[2]

GREEN, RED, BLOCKED = "GREEN", "RED", "BLOCKED"


@dataclass(frozen=True)
class Block:
    name: str
    tests: str  # path relative to repo root


@dataclass(frozen=True)
class Gate:
    number: int
    title: str
    blocks: tuple[Block, ...]
    integration: str  # path to the gate's exit/integration test
    exit_criterion: str


GATES: tuple[Gate, ...] = (
    Gate(
        0,
        "Foundations",
        (
            Block("clock", "tests/gate0/test_clock.py"),
            Block("payments_sim", "tests/gate0/test_payments_sim.py"),
            Block("voice_sim", "tests/gate0/test_voice_sim.py"),
            Block("inventory_sim", "tests/gate0/test_inventory_sim.py"),
            Block("routing_sim", "tests/gate0/test_routing_sim.py"),
        ),
        "tests/gate0/test_gate0_exit.py",
        "Every port has a simulator and every simulator passes its contract.",
    ),
    Gate(
        1,
        "Core engine",
        (
            Block("event_log", "tests/gate1/test_event_log.py"),
            Block("state_machine", "tests/gate1/test_state_machine.py"),
            Block("scheduler", "tests/gate1/test_scheduler.py"),
            Block("connection_buffer", "tests/gate1/test_connection_buffer.py"),
            Block("optimizer", "tests/gate1/test_optimizer.py"),
            Block("policy", "tests/gate1/test_policy.py"),
        ),
        "tests/gate1/test_gate1_integration.py",
        "Golden and property tests pass; no code path moves money except through the policy layer.",
    ),
)


@dataclass
class Result:
    name: str
    status: str
    seconds: float = 0.0
    detail: str = ""


@dataclass
class GateResult:
    number: int
    title: str
    status: str = BLOCKED
    blocks: list[Result] = field(default_factory=list)
    integration: Result | None = None


Runner = Callable[[str], tuple[bool, str]]


def pytest_runner(path: str) -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", path, "-q", "-x", "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


def _run_one(name: str, path: str, runner: Runner) -> Result:
    start = time.monotonic()
    ok, out = runner(path)
    return Result(name, GREEN if ok else RED, time.monotonic() - start, "" if ok else out)


def run_gates(
    upto: int,
    runner: Runner = pytest_runner,
    gates: tuple[Gate, ...] = GATES,
) -> list[GateResult]:
    results: list[GateResult] = []
    blocked = False
    for gate in gates:
        if gate.number > upto:
            break
        gr = GateResult(gate.number, gate.title)
        results.append(gr)
        if blocked:
            gr.status = BLOCKED
            gr.blocks = [Result(b.name, BLOCKED) for b in gate.blocks]
            gr.integration = Result("integration", BLOCKED)
            continue
        gr.blocks = [_run_one(b.name, b.tests, runner) for b in gate.blocks]
        if all(r.status == GREEN for r in gr.blocks):
            gr.integration = _run_one("integration", gate.integration, runner)
        else:
            gr.integration = Result("integration", BLOCKED, detail="a block is red")
        gr.status = GREEN if gr.integration.status == GREEN else RED
        blocked = gr.status != GREEN
    return results


def run_block(spec: str, runner: Runner = pytest_runner, gates: tuple[Gate, ...] = GATES) -> Result:
    gate_part, _, block_name = spec.partition(":")
    number = int(gate_part.removeprefix("gate"))
    for gate in gates:
        if gate.number == number:
            for b in gate.blocks:
                if b.name == block_name:
                    return _run_one(spec, b.tests, runner)
    raise SystemExit(f"unknown block {spec!r}; use --list")


def render(results: list[GateResult]) -> str:
    lines: list[str] = []
    for g in results:
        lines.append(f"Gate {g.number} {g.title}: {g.status}")
        for r in g.blocks:
            lines.append(f"   block {r.name:<18} {r.status:<8} {r.seconds:5.1f}s")
        if g.integration:
            lines.append(f"   integration{'':<11} {g.integration.status:<8} {g.integration.seconds:5.1f}s")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tripsync.gates")
    p.add_argument("--gate", type=int, help="run gates 0..N in order")
    p.add_argument("--block", help="run one block alone, e.g. gate1:optimizer")
    p.add_argument("--list", action="store_true")
    args = p.parse_args(argv)

    if args.list:
        for g in GATES:
            print(f"Gate {g.number} {g.title}  | exit: {g.exit_criterion}")
            for b in g.blocks:
                print(f"   gate{g.number}:{b.name}")
        return 0
    if args.block:
        r = run_block(args.block)
        print(f"{r.name}: {r.status}")
        if r.detail:
            print(r.detail)
        return 0 if r.status == GREEN else 1
    if args.gate is None:
        p.error("give --gate N, --block gateN:name, or --list")

    results = run_gates(args.gate)
    print(render(results))
    out = ROOT / ".gates"
    out.mkdir(exist_ok=True)
    (out / "report.json").write_text(
        json.dumps(
            [
                {
                    "gate": g.number,
                    "title": g.title,
                    "status": g.status,
                    "blocks": [r.__dict__ for r in g.blocks],
                    "integration": g.integration.__dict__ if g.integration else None,
                }
                for g in results
            ],
            indent=2,
        )
    )
    failed = [r for g in results for r in [*g.blocks, g.integration] if r and r.status == RED]
    for r in failed[:1]:
        print("\nFirst failure:\n" + r.detail)
    return 0 if all(g.status == GREEN for g in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
