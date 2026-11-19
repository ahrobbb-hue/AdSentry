from database.supabase_db import summarize_review_ratings


def test_rating_summary_counts_all_five_star_values_and_average() -> None:
    summary = summarize_review_ratings([
        {"rating": 5},
        {"rating": 5},
        {"rating": 4},
        {"rating": 1},
    ])

    assert summary == {
        "avg_rating": 3.75,
        "review_count": 4,
        "rating_distribution": {1: 1, 2: 0, 3: 0, 4: 1, 5: 2},
    }


def test_rating_summary_handles_empty_and_invalid_rows() -> None:
    summary = summarize_review_ratings([{}, {"rating": "invalid"}, {"rating": 6}])

    assert summary == {
        "avg_rating": 0.0,
        "review_count": 0,
        "rating_distribution": {1: 0, 2: 0, 3: 0, 4: 0, 5: 0},
    }
