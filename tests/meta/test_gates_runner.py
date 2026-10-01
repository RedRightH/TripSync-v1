"""The gating itself is a block: prove the runner really stops at the first red gate."""
from tripsync.gates import BLOCKED, GREEN, RED, Block, Gate, run_block, run_gates

GATES = (
    Gate(0, "A", (Block("a1", "a1"), Block("a2", "a2")), "a_int", "x"),
    Gate(1, "B", (Block("b1", "b1"),), "b_int", "x"),
    Gate(2, "C", (Block("c1", "c1"),), "c_int", "x"),
)


def make_runner(failing=()):
    calls = []

    def runner(path):
        calls.append(path)
        return (path not in failing), ("boom" if path in failing else "")

    return runner, calls


def test_all_green_runs_everything_in_order():
    runner, calls = make_runner()
    res = run_gates(2, runner, GATES)
    assert [g.status for g in res] == [GREEN, GREEN, GREEN]
    assert calls == ["a1", "a2", "a_int", "b1", "b_int", "c1", "c_int"]


def test_red_block_skips_integration_and_blocks_later_gates():
    runner, calls = make_runner(failing={"a2"})
    res = run_gates(2, runner, GATES)
    assert res[0].status == RED
    assert res[0].integration.status == BLOCKED
    assert res[1].status == BLOCKED and res[2].status == BLOCKED
    assert calls == ["a1", "a2"], "no integration test and no later gate may run after a red block"


def test_red_integration_blocks_later_gates():
    runner, calls = make_runner(failing={"a_int"})
    res = run_gates(2, runner, GATES)
    assert res[0].status == RED
    assert res[1].status == BLOCKED
    assert "b1" not in calls


def test_running_gate_one_reruns_gate_zero():
    runner, calls = make_runner()
    run_gates(1, runner, GATES)
    assert calls[:3] == ["a1", "a2", "a_int"]


def test_upto_limits_scope():
    runner, calls = make_runner()
    res = run_gates(0, runner, GATES)
    assert len(res) == 1 and "b1" not in calls


def test_single_block_runs_alone_without_prerequisites():
    runner, calls = make_runner(failing={"a1"})
    r = run_block("gate1:b1", runner, GATES)
    assert r.status == GREEN and calls == ["b1"]
