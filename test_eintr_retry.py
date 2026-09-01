#!/usr/bin/env python3
"""
Tests for issue #15: iCloud scandir EINTR must not kill the weekly intel run.

Covers:
- _glob_report_files: retries InterruptedError / OSError(EINTR), degrades to None
- load_last_report_section: returns "" when glob exhausted (no crash), success path unchanged
- company loop: unexpected per-company error is logged and skipped, later companies still run
- all companies failing in the loop must NOT fall into the "no new intel" success
  notification (silent-success pattern, PITFALLS #20/#22/#27) — run raises instead
- partial failure: successful companies' report still sent, but subject/body/telegram
  name the skipped companies
- IntelConfigError still propagates out of the loop

No network, no writes (except a temp dir for the partial-failure report), no real
iCloud access. Safe to run repeatedly.

Usage:
  python test_eintr_retry.py
"""
import errno
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import run_intel


class _FakeReportFile:
    """Minimal stand-in for a Path: stem + read_text only."""

    def __init__(self, stem: str, text: str):
        self.stem = stem
        self._text = text

    def read_text(self, encoding="utf-8"):
        return self._text


# ── _glob_report_files ────────────────────────────────────────────────────────

def test_glob_retries_eintr_then_succeeds():
    files = [Path("2026-08-23-china-companies.md"), Path("2026-08-16-china-companies.md")]
    with mock.patch.object(run_intel.Path, "glob") as m_glob, \
         mock.patch.object(run_intel.time, "sleep") as m_sleep:
        m_glob.side_effect = [InterruptedError(), files]
        result = run_intel._glob_report_files(Path("/vault/Hermes/MI"))
    assert result == sorted(files, reverse=True), f"got {result!r}"
    assert m_glob.call_count == 2
    assert m_sleep.call_count == 1
    print("[PASS] glob: first EINTR then success -> returns sorted files")


def test_glob_all_eintr_returns_none():
    with mock.patch.object(run_intel.Path, "glob") as m_glob, \
         mock.patch.object(run_intel.time, "sleep") as m_sleep:
        m_glob.side_effect = InterruptedError
        result = run_intel._glob_report_files(Path("/vault/Hermes/MI"))
    assert result is None
    assert m_glob.call_count == run_intel.EINTR_MAX_ATTEMPTS
    assert m_sleep.call_count == run_intel.EINTR_MAX_ATTEMPTS - 1
    print("[PASS] glob: always InterruptedError -> returns None after retries, no raise")


def test_glob_oserror_eintr_retries_and_returns_none():
    """Review #16: OSError(errno.EINTR) is a documented catch path but was untested."""
    with mock.patch.object(run_intel.Path, "glob") as m_glob, \
         mock.patch.object(run_intel.time, "sleep") as m_sleep:
        m_glob.side_effect = OSError(errno.EINTR, "Interrupted system call")
        result = run_intel._glob_report_files(Path("/vault/Hermes/MI"))
    assert result is None
    assert m_glob.call_count == run_intel.EINTR_MAX_ATTEMPTS
    assert m_sleep.call_count == run_intel.EINTR_MAX_ATTEMPTS - 1
    print("[PASS] glob: OSError(EINTR) retried then returns None (not immediate raise)")


def test_glob_non_eintr_oserror_propagates():
    with mock.patch.object(run_intel.Path, "glob") as m_glob:
        m_glob.side_effect = OSError(errno.EACCES, "permission denied")
        try:
            run_intel._glob_report_files(Path("/vault/Hermes/MI"))
        except OSError:
            assert m_glob.call_count == 1
            print("[PASS] glob: non-EINTR OSError propagates without retry")
            return
    raise AssertionError("expected OSError to propagate")


# ── load_last_report_section ──────────────────────────────────────────────────

def test_load_last_report_section_empty_on_exhaustion():
    with mock.patch.object(run_intel, "_glob_report_files", return_value=None) as m_glob, \
         mock.patch.object(run_intel.logger, "error") as m_err:
        result = run_intel.load_last_report_section("安踏集团")
    assert result == ""
    m_glob.assert_called_once()
    assert m_err.call_count >= 1
    print("[PASS] load_last_report_section: glob exhausted -> returns '' with ERROR log")


def test_load_last_report_section_success_path_unchanged():
    today = datetime.now().strftime("%Y-%m-%d")
    prev_text = "## 安踏集团\n\n上周内容甲\n\n---\n## 海尔集团\n\n上周内容乙"
    today_file = _FakeReportFile(f"{today}-china-companies", "## 安踏集团\n\n今日内容")
    prev_file = _FakeReportFile("2026-08-23-china-companies", prev_text)
    with mock.patch.object(run_intel, "_glob_report_files",
                           return_value=[today_file, prev_file]):
        result = run_intel.load_last_report_section("安踏集团")
    assert result == "上周内容甲", f"got {result!r}"
    print("[PASS] load_last_report_section: success path returns previous-week section, skips today")


def test_load_last_report_section_no_previous_report():
    today = datetime.now().strftime("%Y-%m-%d")
    today_file = _FakeReportFile(f"{today}-china-companies", "## 安踏集团\n\n今日内容")
    with mock.patch.object(run_intel, "_glob_report_files", return_value=[today_file]):
        result = run_intel.load_last_report_section("安踏集团")
    assert result == ""
    print("[PASS] load_last_report_section: only today's report -> returns ''")


# ── company loop isolation ────────────────────────────────────────────────────

def test_company_loop_isolates_per_company_error():
    companies = [{"zh": "公司甲"}, {"zh": "公司乙"}]
    fetch_calls = []

    def fake_fetch(company):
        fetch_calls.append(company)
        return (None, {})

    with mock.patch.multiple(
        run_intel,
        _validate_intel_config=mock.DEFAULT,
        get_companies_full=lambda: companies,
        _load_seen_urls=lambda: {},
        _load_fetch_log=lambda: {},
        get_articles_by_company=lambda zh: [],
        fetch_company_raw=fake_fetch,
        load_last_report_section=mock.Mock(side_effect=[RuntimeError("boom"), ""]),
        _save_seen_urls=lambda s: None,
        _save_fetch_log=lambda f: None,
        send_report=lambda **kw: None,
        get_recipients=lambda: [],
        _send_telegram_alert=lambda t: None,
        post_slack_report=lambda b: None,
        get_footer=lambda: "",
    ), mock.patch.object(run_intel.logger, "exception") as m_exc:
        raised = None
        try:
            run_intel.run_intel()
        except RuntimeError as e:
            raised = e
        else:
            raise AssertionError("expected RuntimeError when no sections and failures exist")

    assert fetch_calls == [{"zh": "公司乙"}], f"expected only later company to reach fetch, got {fetch_calls!r}"
    m_exc.assert_called_once()
    assert raised is not None
    msg = str(raised)
    assert "1/2 companies failed" in msg, f"message must report partial failure, got: {msg!r}"
    assert "公司甲" in msg
    assert "all companies failed" not in msg
    print("[PASS] company loop: unexpected per-company error logged + skipped, later company still runs")


def test_company_loop_intel_config_error_propagates():
    companies = [{"zh": "公司甲"}]
    with mock.patch.multiple(
        run_intel,
        _validate_intel_config=mock.DEFAULT,
        get_companies_full=lambda: companies,
        _load_seen_urls=lambda: {},
        _load_fetch_log=lambda: {},
        get_articles_by_company=lambda zh: [],
        fetch_company_raw=lambda c: (None, {}),
        load_last_report_section=mock.Mock(
            side_effect=run_intel.IntelConfigError("missing config")),
        _save_seen_urls=lambda s: None,
        _save_fetch_log=lambda f: None,
        send_report=lambda **kw: None,
        get_recipients=lambda: [],
        _send_telegram_alert=lambda t: None,
        post_slack_report=lambda b: None,
        get_footer=lambda: "",
    ):
        try:
            run_intel.run_intel()
        except run_intel.IntelConfigError:
            print("[PASS] company loop: IntelConfigError still propagates")
            return
    raise AssertionError("expected IntelConfigError to propagate")


# ── silent-success guard (review #16) ─────────────────────────────────────────

def test_run_all_companies_error_no_false_success():
    """Every company skipped by the isolation except must NOT send the 'no new
    intel' success notification and must NOT exit cleanly."""
    companies = [{"zh": "公司甲"}, {"zh": "公司乙"}]
    with mock.patch.multiple(
        run_intel,
        _validate_intel_config=mock.DEFAULT,
        get_companies_full=lambda: companies,
        _load_seen_urls=lambda: {},
        _load_fetch_log=lambda: {},
        get_articles_by_company=lambda zh: [],
        fetch_company_raw=lambda c: (None, {}),
        load_last_report_section=mock.Mock(side_effect=RuntimeError("boom")),
        _save_seen_urls=lambda s: None,
        _save_fetch_log=lambda f: None,
        send_report=mock.Mock(side_effect=AssertionError("send_report must not be called")),
        get_recipients=lambda: [],
        _send_telegram_alert=mock.Mock(side_effect=AssertionError("telegram alert must not be called")),
        post_slack_report=mock.Mock(side_effect=AssertionError("slack report must not be called")),
        get_footer=lambda: "",
    ), mock.patch.object(run_intel.logger, "info") as m_info, \
       mock.patch.object(run_intel.logger, "exception") as m_exc:
        raised = None
        try:
            run_intel.run_intel()
        except RuntimeError as e:
            raised = e
        else:
            raise AssertionError("expected RuntimeError when all companies fail")

    m_exc.assert_called()
    assert raised is not None
    msg = str(raised)
    assert "2/2 companies failed" in msg, f"message must report the failure count, got: {msg!r}"
    assert "公司甲" in msg and "公司乙" in msg
    assert not any("No new intel" in str(call) for call in m_info.call_args_list), \
        "'No new intel' must not be logged when every company failed"
    print("[PASS] all companies error -> raises, no 'no new intel' success notification")


def test_run_no_errors_empty_sections_normal_notification():
    """Legit no-new-intel run (no errors) must keep the existing success path."""
    companies = [{"zh": "公司甲"}, {"zh": "公司乙"}]
    sent = {}

    def fake_send_report(**kw):
        sent.update(kw)
        return "sid-1"

    with mock.patch.multiple(
        run_intel,
        _validate_intel_config=mock.DEFAULT,
        get_companies_full=lambda: companies,
        _load_seen_urls=lambda: {},
        _load_fetch_log=lambda: {},
        get_articles_by_company=lambda zh: [],
        fetch_company_raw=lambda c: (None, {}),
        load_last_report_section=lambda zh: "",
        _save_seen_urls=lambda s: None,
        _save_fetch_log=lambda f: None,
        send_report=fake_send_report,
        get_recipients=lambda: [],
        _save_processed_id=lambda sid: None,
        _send_telegram_alert=lambda t: None,
        post_slack_report=lambda b: None,
        get_footer=lambda: "",
    ):
        run_intel.run_intel()  # must not raise

    assert "本周无新情报" in sent.get("subject", ""), f"got {sent.get('subject', '')!r}"
    print("[PASS] no errors + empty sections -> normal 'no new intel' notification")


def test_run_partial_failure_sends_report_with_skip_note():
    """One company fails, another succeeds: report still sent, but subject/body/
    telegram must name the skipped company."""
    companies = [{"zh": "公司甲"}, {"zh": "公司乙"}]
    sent = {}
    tg_texts = []
    slack_bodies = []
    tmp_dir = Path(tempfile.mkdtemp(prefix="mi-eintr-test-"))
    try:
        def fake_send_report(**kw):
            sent.update(kw)
            return "sid-1"

        def fake_fetch(company):
            if company["zh"] == "公司乙":
                return ([{"url": "https://x/1", "title": "t1", "content": "c1",
                          "published_date": "2026-08-30"}], {})
            return (None, {})

        with mock.patch.multiple(
            run_intel,
            _validate_intel_config=mock.DEFAULT,
            get_companies_full=lambda: companies,
            _load_seen_urls=lambda: {},
            _load_fetch_log=lambda: {},
            get_articles_by_company=lambda zh: [],
            fetch_company_raw=fake_fetch,
            load_last_report_section=mock.Mock(side_effect=[RuntimeError("boom"), ""]),
            prefilter_articles=lambda zh, arts, lws: (arts, 100, "ok"),
            get_company_context=lambda zh, en: "",
            synthesize_with_llm=lambda zh, nc, hc, kb, **kw: (
                "乙的总结", {"prompt_tokens": 10, "completion_tokens": 5}),
            _save_seen_urls=lambda s: None,
            _save_fetch_log=lambda f: None,
            send_report=fake_send_report,
            get_recipients=lambda: [],
            _save_processed_id=lambda sid: None,
            _send_telegram_alert=lambda t: tg_texts.append(t),
            post_slack_report=lambda b: slack_bodies.append(b),
            get_footer=lambda: "",
            OBSIDIAN_DIR=tmp_dir,
        ):
            run_intel.run_intel()  # partial failure: must not raise

        body = sent.get("markdown_body", "")
        subject = sent.get("subject", "")
        assert "公司甲" in body, f"skipped company must be named in body: {body!r}"
        assert "公司甲" in subject, f"skipped company must be named in subject: {subject!r}"
        assert "公司乙的总结" in body or "乙的总结" in body, f"successful section missing: {body!r}"
        assert any("公司甲" in t for t in tg_texts), f"telegram must name skipped company: {tg_texts!r}"
        assert any("公司甲" in b for b in slack_bodies), f"slack body must name skipped company"
        print("[PASS] partial failure -> report sent, skipped company named in subject/body/telegram")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main():
    tests = [
        test_glob_retries_eintr_then_succeeds,
        test_glob_all_eintr_returns_none,
        test_glob_oserror_eintr_retries_and_returns_none,
        test_glob_non_eintr_oserror_propagates,
        test_load_last_report_section_empty_on_exhaustion,
        test_load_last_report_section_success_path_unchanged,
        test_load_last_report_section_no_previous_report,
        test_company_loop_isolates_per_company_error,
        test_company_loop_intel_config_error_propagates,
        test_run_all_companies_error_no_false_success,
        test_run_no_errors_empty_sections_normal_notification,
        test_run_partial_failure_sends_report_with_skip_note,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:
            failed += 1
            print(f"[FAIL] {t.__name__}: {type(e).__name__}: {e}")
    if failed:
        print(f"\n{failed} test(s) FAILED.")
        sys.exit(1)
    print("\nAll cases passed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
