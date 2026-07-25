"""Unit tests for PrincipalRepository."""

from unittest.mock import MagicMock

from src.core.database.repositories.principal import PrincipalRepository


class TestPrincipalRepositoryGetName:
    def test_returns_principal_for_tenant_scoped_id(self):
        session = MagicMock()
        principal = MagicMock()
        session.scalars.return_value.first.return_value = principal

        assert PrincipalRepository(session, "tenant-a").get_by_id("principal-a") is principal

    def test_returns_name_for_principal_in_tenant(self):
        session = MagicMock()
        principal = MagicMock()
        principal.name = "Buyer Agent"
        session.scalars.return_value.first.return_value = principal

        assert PrincipalRepository(session, "tenant-a").get_name("principal-a") == "Buyer Agent"

    def test_returns_none_when_principal_is_missing(self):
        session = MagicMock()
        session.scalars.return_value.first.return_value = None

        assert PrincipalRepository(session, "tenant-a").get_name("principal-a") is None
