import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import time
from unittest.mock import MagicMock, patch
import pytest
from fastapi import HTTPException

from app.features import clear_features_cache, is_feature_blocked, require_feature

@pytest.fixture(autouse=True)
def setup_teardown():
    clear_features_cache()
    yield
    clear_features_cache()

def test_is_feature_blocked_caching():
    user_id = "00000000-0000-0000-0000-000000000001"
    key = "chamak"

    mock_supabase = MagicMock()
    mock_rpc = MagicMock()
    mock_rpc.execute.return_value = MagicMock(data=True)
    mock_supabase.rpc.return_value = mock_rpc

    with patch("app.features.get_supabase", return_value=mock_supabase):
        # 1. First call: executes RPC
        assert is_feature_blocked(user_id, key) is True
        assert mock_supabase.rpc.call_count == 1

        # 2. Second call within TTL: served from cache, no new RPC
        assert is_feature_blocked(user_id, key) is True
        assert mock_supabase.rpc.call_count == 1

        # 3. Clear cache -> executes RPC again
        clear_features_cache()
        assert is_feature_blocked(user_id, key) is True
        assert mock_supabase.rpc.call_count == 2

def test_is_feature_blocked_fail_open_on_error():
    user_id = "00000000-0000-0000-0000-000000000001"
    key = "chamak"

    mock_supabase = MagicMock()
    mock_supabase.rpc.side_effect = Exception("DB connection timeout")

    with patch("app.features.get_supabase", return_value=mock_supabase):
        # Fails open (returns False) on exception
        assert is_feature_blocked(user_id, key) is False

@pytest.mark.asyncio
async def test_require_feature_dependency_allowed():
    user_id = "00000000-0000-0000-0000-000000000001"
    dep = require_feature("chamak")

    with patch("app.features.is_feature_blocked", return_value=False):
        result = await dep(user_id=user_id)
        assert result == user_id

@pytest.mark.asyncio
async def test_require_feature_dependency_blocked():
    user_id = "00000000-0000-0000-0000-000000000001"
    dep = require_feature("chamak")

    with patch("app.features.is_feature_blocked", return_value=True):
        with pytest.raises(HTTPException) as exc_info:
            await dep(user_id=user_id)
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail["code"] == "FEATURE_DISABLED"
        assert exc_info.value.detail["feature"] == "chamak"
