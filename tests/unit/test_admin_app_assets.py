"""Regression coverage for the admin app's repository-level assets."""

from src.admin.app import create_app


def test_admin_app_resolves_repository_template_and_static_roots():
    """Server-rendered admin pages and their shared assets resolve from the repository roots."""
    app = create_app({"TESTING": True, "SECRET_KEY": "test-secret"})

    assert app.jinja_env.get_template("workflow_review.html") is not None
    assert app.static_folder is not None
    assert app.static_folder.endswith("/static")
