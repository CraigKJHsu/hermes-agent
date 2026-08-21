from pathlib import Path

from tools import registry as registry_module


def test_targeted_builtin_discovery_imports_only_matching_literal_modules(
    monkeypatch, tmp_path: Path,
):
    (tmp_path / "alpha_tool.py").write_text(
        'registry.register(name="alpha", toolset="test", schema={}, handler=None)\n',
        encoding="utf-8",
    )
    (tmp_path / "beta_tool.py").write_text(
        'registry.register(name="beta", toolset="test", schema={}, handler=None)\n',
        encoding="utf-8",
    )
    (tmp_path / "dynamic_tool.py").write_text(
        'registry.register(name=dynamic_name, toolset="test", schema={}, handler=None)\n',
        encoding="utf-8",
    )
    imported = []
    monkeypatch.setattr(
        registry_module.importlib,
        "import_module",
        lambda name: imported.append(name),
    )

    result = registry_module.discover_builtin_tools(
        tmp_path,
        tool_names={"beta", "dynamic_name"},
    )

    assert result == ["tools.beta_tool"]
    assert imported == ["tools.beta_tool"]
