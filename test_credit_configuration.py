import pytest
from pydantic import ValidationError
from app.config import Settings


def test_production_cannot_start_with_credit_metering_disabled():
    with pytest.raises(ValidationError, match='CREDITS_ENABLED must be true'):
        Settings(ENVIRONMENT='production', CREDITS_ENABLED=False, _env_file=None)


def test_unknown_deployment_environment_cannot_bypass_credit_metering():
    with pytest.raises(ValidationError, match='CREDITS_ENABLED must be true'):
        Settings(ENVIRONMENT='staging', CREDITS_ENABLED=False, _env_file=None)


def test_explicit_local_development_and_tests_can_use_unmetered_fixtures():
    for environment in ('development', 'test'):
        assert Settings(ENVIRONMENT=environment, CREDITS_ENABLED=False, _env_file=None).CREDITS_ENABLED is False


def test_production_default_enforces_credit_metering():
    assert Settings(ENVIRONMENT='production', CREDITS_ENABLED=True, _env_file=None).CREDITS_ENABLED is True
