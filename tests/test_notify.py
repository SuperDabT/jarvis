import subprocess

from jarvis import cards, db, notify


def _conn_with_cards(n):
    conn = db.connect(":memory:")
    for i in range(n):
        cards.upsert_card(conn, type_="electrical", title=f"Card {i}", why="w",
                          source_ref="hardware.yaml", source_url=None,
                          claude_prompt="p", check_id=f"c{i}")
    return conn


def test_message_lists_titles_and_overflow():
    summary, body = notify.new_card_message(_conn_with_cards(5), 5)
    assert summary == "5 new suggestions"
    assert body.count("•") == notify.MAX_TITLES
    assert body.endswith("and 2 more")


def test_message_singular():
    summary, body = notify.new_card_message(_conn_with_cards(1), 1)
    assert summary == "1 new suggestion"
    assert body == "• Card 0"


def test_send_missing_binary(monkeypatch):
    monkeypatch.setattr(notify.shutil, "which", lambda _: None)
    assert notify.send("s", "b") is False


def test_send_failure_is_swallowed(monkeypatch):
    monkeypatch.setattr(notify.shutil, "which", lambda _: "/usr/bin/notify-send")

    def boom(*a, **k):
        raise subprocess.CalledProcessError(1, "notify-send")

    assert notify.send("s", "b", run=boom) is False


def test_send_calls_notify_send(monkeypatch):
    monkeypatch.setattr(notify.shutil, "which", lambda _: "/usr/bin/notify-send")
    calls = []
    assert notify.send("s", "b", run=lambda args, **k: calls.append(args)) is True
    assert calls[0][-2:] == ["s", "b"]
