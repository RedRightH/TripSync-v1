# TripSync (gated build)

Built stage by stage. Each block is tested **alone** before anything is integrated, and a
gate only opens when every block in it is green and its integration test passes.

```
python -m venv venv && venv\Scripts\activate     # Windows (source venv/bin/activate elsewhere)
pip install -r requirements.txt
copy .env.example .env                            # then add GROQ_API_KEY (needed from Gate 2)
python -m tripsync.gates --list                 # every gate and block
python -m tripsync.gates --block gate1:optimizer   # one block, in isolation
python -m tripsync.gates --gate 1                  # gates 0 then 1, in order
python -m pytest tests/meta                        # tests of the gate runner itself
```

Rules the runner enforces (and `tests/meta` proves):
1. A gate's blocks run first. Its integration test runs only if every block is green.
2. A gate that is not green blocks every later gate.
3. Running gate N re-runs gates 0..N-1, so an older gate cannot silently break.

## Where this stands against the PRD roadmap

| PRD phase | Gate | Status |
| --- | --- | --- |
| 0. Foundations | Gate 0 | Code half done: clock, ports, simulators and contract tests. **Still human work:** 0.1 (real endpoints, sandbox keys) and 0.2 (compliance answers). |
| 1. Core engine | Gate 1 | Done: event log, state machine, scheduler, connection buffer, optimizer, policy layer, and the integration test. |
| 2. Conversation | Gate 2 | Next: WhatsApp adapter, Groq agent loop, Gnani voice, agent evals. |

## What is real and what is simulated

Everything under `src/tripsync/sims/` is a **simulator**. Each one is a stand-in for a partner
(Pine Labs, Gnani, Ixigo, Delhivery) and is labelled `is_simulated = True`.

- They model behaviour the product needs (mandate lifecycle, idempotent refund, bounded settlement).
  They are **not** copies of the partners' real APIs. Their semantics are assumptions until Phase 0.1
  checks them against the real documentation.
- A real adapter must pass the same contract tests in `tests/gate0/contracts.py` before it replaces a simulator.
- No real money, voice data or fares are used anywhere.

## Layout

```
src/tripsync/
  clock.py        Clock, SystemClock, FakeClock
  ports.py        PaymentsPort, VoicePort, InventoryPort, RoutingPort + registry
  sims/           one simulator per port
  core/
    events.py       append-only SQLite event log (the database rejects UPDATE and DELETE)
    state.py        the only definition of legal trip transitions
    scheduler.py    durable timers (decision deadline, objection window)
    connection.py   connection-buffer check (fails closed on unknown routes)
    optimizer.py    hard filters, utility, min-max normalization, maximin + leximin
    policy.py       group limits + the ONLY code that settles or refunds money
    engine.py       wires the blocks; state is always rebuilt by replaying events
  gates.py        the gate runner
tests/gate0, tests/gate1, tests/meta
```

## Design decisions worth knowing

- **The model proposes, code decides.** Only `core/policy.py` may call `payments.settle` or
  `payments.refund`; a source-scan test fails the build otherwise.
- **The policy layer does not trust its caller.** It recomputes cleared deposits and authorized
  funds from the payments port before releasing money.
- **State is replayed, not stored.** The live state and the audit trail cannot disagree. Every
  state-changing event is validated before it is written, so an illegal action leaves no trace.
- **Idempotency everywhere money moves** (refund, settlement, booking, webhooks).

## Known gaps to close in later gates

- Pending (never approved) mandates are not revoked when a trip closes. Real mandates need
  explicit cancellation once Pine Labs' real behaviour is confirmed (Phase 3).
- Only **committed** members' hard constraints are applied. A member whose mandate bounced
  (for example one who needs a ground floor) is ignored by the optimizer. Decide whether that is
  acceptable before real trips run.
- The vendor booking is an injected callable, not a rail. A real vendor/booking rail is Phase 4.
- Policy-written events (`release_denied`, `vendor_declined`) are not validated by the engine's
  pre-write check; they are only written from states the engine has already verified.
