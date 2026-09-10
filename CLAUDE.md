# Project: TDD Task Queue State Machine (Python 3.11+)

## Commands
- Run all tests: `pytest`
- Run single test file: `pytest tests/test_queue.py`
- Run specific test: `pytest tests/test_queue.py -k test_name`
- Type checking: `mypy src/`
- Linting: `ruff check .`

## TDD Protocol
1. **RED Phase**:
   - Write tests in `tests/` *before* touching implementation in `src/`.
   - Run `pytest` to verify the test fails specifically on an assertion error (not an import failure or syntax error).
2. **GREEN Phase**:
   - Write the minimum implementation needed to pass the test.
   - Run `pytest` to confirm green status.
3. **REFACTOR Phase**:
   - Clean up code or test structure while keeping the test suite green.

## Test Integrity Guardrails
- **Immutable Existing Tests**: Never alter or delete existing passing tests to make new implementation code pass.
- **Black-Box Testing**: Test exclusively through public interfaces (`src/`). Do not mock internal logic or assert against private attributes (`_internal`).
- **Mandatory Entity & Branch Coverage**: 
  - Every new public class, method, or user-impacting function must have a dedicated test asserting its behavioral contract.
  - Private helpers (`_func`) are strictly prohibited from being tested directly; any logic added inside private helpers must be fully exercised and asserted through public API edge-case tests.
- **Mandatory Negative Cases**: Every feature test suite must include at least one edge/failure case (e.g., invalid state transitions, max retries exceeded, timeout).
- **Strict Assertions**: No trivial assertions (`assert result is not None`). Assert exact state changes, returned payloads, or explicit exceptions (`pytest.raises`).
