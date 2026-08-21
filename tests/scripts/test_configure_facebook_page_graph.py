from scripts import configure_facebook_page_graph as configure


def test_blank_optional_app_secret_preserves_existing_value(
    tmp_path,
    monkeypatch,
):
    env_file = tmp_path / ".env"
    env_file.write_text(
        'FACEBOOK_APP_SECRET="existing-secret"\n'
        'FACEBOOK_PAGE_ACCESS_TOKEN="old-token"\n',
        encoding="utf-8",
    )
    answers = iter(["new-token", ""])
    monkeypatch.setattr(configure, "getpass", lambda _prompt: next(answers))
    monkeypatch.setattr(
        "sys.argv",
        [
            "configure_facebook_page_graph.py",
            "--page-id",
            "123456",
            "--env-file",
            str(env_file),
        ],
    )

    assert configure.main() == 0

    saved = env_file.read_text(encoding="utf-8")
    assert 'FACEBOOK_APP_SECRET="existing-secret"' in saved
    assert 'FACEBOOK_PAGE_ACCESS_TOKEN="new-token"' in saved
