from italia_corpus.config import target_repo_full_name


def test_target_repo_full_name_accepts_short_and_qualified_targets() -> None:
    assert target_repo_full_name("italia-corpus", "casungo") == "casungo/italia-corpus"
    assert target_repo_full_name("casungo/italia-corpus", "someone-else") == "casungo/italia-corpus"
