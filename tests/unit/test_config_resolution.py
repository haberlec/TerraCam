"""
Unit tests for fli.config — project config directory resolution.

Covers the TERRACAM_CONFIG environment override, the repo-relative
fallback walk, and graceful load_config failure. See
docs/hardening_plan.md, Phase 4.
"""

import json

import pytest

from fli.config import find_config_dir, load_config


class TestFindConfigDir:
    """Resolution order: env override, then repo-relative walk."""

    def test_repo_fallback_finds_project_config(self, monkeypatch):
        monkeypatch.delenv("TERRACAM_CONFIG", raising=False)
        config_dir = find_config_dir()
        assert (config_dir / "filter_specifications.json").exists()
        assert config_dir.name == "config"

    def test_env_override(self, monkeypatch, tmp_path):
        (tmp_path / "filter_specifications.json").write_text("{}")
        monkeypatch.setenv("TERRACAM_CONFIG", str(tmp_path))
        assert find_config_dir() == tmp_path

    def test_env_override_missing_specs_raises(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TERRACAM_CONFIG", str(tmp_path))
        with pytest.raises(FileNotFoundError, match="TERRACAM_CONFIG"):
            find_config_dir()


class TestLoadConfig:
    """load_config returns parsed JSON or None with a warning."""

    def test_loads_real_config(self, monkeypatch):
        monkeypatch.delenv("TERRACAM_CONFIG", raising=False)
        config = load_config("filter_specifications.json")
        assert config is not None
        assert "filter_specifications" in config

    def test_missing_file_returns_none(self, monkeypatch, tmp_path):
        (tmp_path / "filter_specifications.json").write_text("{}")
        monkeypatch.setenv("TERRACAM_CONFIG", str(tmp_path))
        assert load_config("nonexistent.json") is None

    def test_malformed_json_returns_none(self, monkeypatch, tmp_path):
        (tmp_path / "filter_specifications.json").write_text("{}")
        (tmp_path / "camera_specifications.json").write_text("not json{")
        monkeypatch.setenv("TERRACAM_CONFIG", str(tmp_path))
        assert load_config("camera_specifications.json") is None
