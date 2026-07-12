#!/usr/bin/env python3
"""
Integration-style test for issue #6: email_check must fail fast
(raise JMAPConfigError) when required JMAP config is missing, instead
of silently swallowing it as an ordinary poll failure.

No network calls, no writes. Safe to run repeatedly.

Usage:
  python test_jmap_config.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import email_check

FULL = {
    "JMAP_BASE": "https://example.com",
    "JMAP_ACCOUNT_ID": "acc1",
    "JMAP_INBOX_ID": "inbox1",
    "STALWART_API_KEY": "secret-key-value",
}


def _run_case(name: str, patched_vars: dict, expect_raise: bool) -> bool:
    original = dict(email_check._REQUIRED_JMAP_VARS)
    email_check._REQUIRED_JMAP_VARS.clear()
    email_check._REQUIRED_JMAP_VARS.update(patched_vars)
    raised, err = False, None
    try:
        email_check._validate_jmap_config()
    except email_check.JMAPConfigError as e:
        raised, err = True, str(e)
    finally:
        email_check._REQUIRED_JMAP_VARS.clear()
        email_check._REQUIRED_JMAP_VARS.update(original)

    ok = raised == expect_raise
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: raised={raised} (expected={expect_raise})")
    return ok, err


def main():
    results = []

    ok, _ = _run_case("all present -> no raise", FULL, expect_raise=False)
    results.append(ok)

    ok, _ = _run_case("missing JMAP_BASE -> raise", {**FULL, "JMAP_BASE": ""}, expect_raise=True)
    results.append(ok)

    ok, err = _run_case("missing STALWART_API_KEY -> raise", {**FULL, "STALWART_API_KEY": ""}, expect_raise=True)
    results.append(ok)

    # Error message must name the missing variable, never leak a secret value.
    if err is not None:
        named = "STALWART_API_KEY" in err
        no_leak = FULL["STALWART_API_KEY"] not in err
        ok = named and no_leak
        print(f"[{'PASS' if ok else 'FAIL'}] error names missing var without leaking values")
        results.append(ok)
    else:
        print("[FAIL] no error message captured for missing STALWART_API_KEY case")
        results.append(False)

    if all(results):
        print("\nAll cases passed.")
        sys.exit(0)
    else:
        print("\nSome cases FAILED.")
        sys.exit(1)


if __name__ == "__main__":
    main()
