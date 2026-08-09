from fastapi import FastAPI


def test_api_app_exists() -> None:
    from proseforge.api.main import app

    assert isinstance(app, FastAPI)


def test_cli_main_is_callable() -> None:
    from proseforge.cli.main import main

    assert callable(main)
