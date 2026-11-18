import logging

from rag import retrieve


def test_retrieval_query_failure_logs_only_the_exception_type(monkeypatch, caplog):
    secret_marker = "private-retrieval-secret-marker"

    class BrokenCollection:
        def count(self):
            return 1

        def query(self, **kwargs):
            raise RuntimeError(secret_marker)

    monkeypatch.setattr(retrieve, "_collection", BrokenCollection())
    with caplog.at_level(logging.WARNING, logger=retrieve.__name__):
        result = retrieve.retrieve_evidence("def sample(): return 1")

    assert result == []
    assert "RuntimeError" in caplog.text
    assert secret_marker not in caplog.text
