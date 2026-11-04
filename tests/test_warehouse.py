from conftest import RUN_DATE

REVLOG_COLUMNS = ("review_id", "card_id", "ease", "interval", "time_ms", "review_type", "loaded_on")


def test_a_scalar_leaves_the_connection_free_for_the_next_insert(wh):
    """Ingest reads the last review id, then inserts; DuckDB 2.0 refuses the
    insert if the read left rows behind."""
    wh.insert("raw_revlog", REVLOG_COLUMNS, [(1, 10, 3, 5, 800, 1, RUN_DATE), (2, 10, 3, 9, 700, 1, RUN_DATE)])
    assert wh.scalar("SELECT review_id FROM raw_revlog ORDER BY review_id") == 1
    wh.insert("raw_revlog", REVLOG_COLUMNS, [(3, 11, 2, 1, 900, 0, RUN_DATE)], or_ignore=True)
    assert wh.scalar("SELECT COUNT(*) FROM raw_revlog") == 3


def test_a_scalar_with_no_rows_is_none(wh):
    assert wh.scalar("SELECT review_id FROM raw_revlog") is None
