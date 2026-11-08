"""Transient failures must not fail the round; permanent ones must."""
from fd_text_core import embed_worker as w


def test_rate_limit_and_timeout_are_retryable():
    assert w.is_retryable("EmbeddingUnavailable: 嵌入重试耗尽: 嵌入请求 status=429 body={'error': {'message': 'Upstream rate limit exceeded'}}")
    assert w.is_retryable("read timeout")
    assert w.is_retryable("connection reset by peer")
    assert w.is_retryable("upstream status=503")


def test_permanent_failures_are_not_retryable():
    assert not w.is_retryable("嵌入返回 0 条，期望 3 条")
    assert not w.is_retryable("invalid vector dimension 512 != 1024")
    assert not w.is_retryable(None)


def test_requeue_rule_and_classifier_share_one_list():
    # the SQL the requeue uses is generated from the same tuple, so a new hint
    # cannot be added to one path only
    assert w._RETRYABLE_SQL.count("error LIKE %s") == len(w.RETRYABLE_HINTS)


def test_a_rate_limited_round_is_not_a_job_failure():
    """The bug: 9/10 embedded, 1 hit 429 and was requeued, yet the Job went Failed.

    Production 2026-11-08: three consecutive fd-info-ship-spool Jobs red while
    each one embedded 40-50 documents; every failure was 429 and requeued by
    retry_rate_limited(). The loader now fails only on permanent failures, and the
    backlog-age gate (max_backlog_hours) stays the signal for "green but vectors
    missing".
    """
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "scripts" / "load_news_spool.py").read_text()
    assert "if failed - retryable:" in src, "the loader must discount transient failures"
    assert "if failed:" not in re.sub(r"if failed - retryable:", "", src), \
        "no bare 'if failed:' may remain — that is the false-red path"
