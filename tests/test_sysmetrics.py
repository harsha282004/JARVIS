"""backend.core.sysmetrics: real process/host resource readings for the dashboard's System Resources card.
No fabrication -- every value is either a genuine measurement or None/omitted."""

from backend.core.sysmetrics import cpu_percent, disk_usage_percent, pid, working_set_mb


def test_pid_is_this_process():
    import os

    assert pid() == os.getpid()


def test_working_set_mb_is_a_real_positive_number_on_this_platform():
    mb = working_set_mb()
    assert mb is None or mb > 0  # None only if the platform truly cannot report it; never a fabricated number


def test_cpu_percent_is_a_real_non_negative_measurement():
    # First call measures since import time; do a little work so the interval is not exactly zero-length.
    total = 0
    for i in range(200_000):
        total += i
    value = cpu_percent()
    assert value is None or value >= 0.0
    second = cpu_percent()  # a second call must not crash and must reset the measurement window
    assert second is None or second >= 0.0


def test_disk_usage_percent_reports_real_numbers_for_this_projects_own_drive():
    usage = disk_usage_percent()
    assert usage is not None
    assert 0.0 <= usage["percent_used"] <= 100.0
    assert usage["free_gb"] >= 0.0 and usage["total_gb"] > 0.0
    assert usage["free_gb"] <= usage["total_gb"] + 0.1  # rounding slack


def test_disk_usage_percent_returns_none_for_a_path_that_cannot_be_read():
    assert disk_usage_percent(r"Z:\does\not\exist\at\all\zzz") is None
