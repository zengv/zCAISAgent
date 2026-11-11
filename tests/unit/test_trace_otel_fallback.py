import builtins

from zcaisagent.adapters.trace.jsonl import JsonlTrace
from zcaisagent.container import build_trace
from zcaisagent.settings import ObservabilityConfig, Settings


def test_build_trace_default_jsonl(tmp_path) -> None:
    settings = Settings(data_dir=tmp_path)
    assert isinstance(build_trace(settings, tmp_path), JsonlTrace)


def test_build_trace_otel_missing_falls_back(tmp_path, monkeypatch) -> None:
    settings = Settings(
        data_dir=tmp_path,
        observability=ObservabilityConfig(otel_enabled=True),
    )
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("opentelemetry"):
            raise ImportError("opentelemetry disabled for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert isinstance(build_trace(settings, tmp_path), JsonlTrace)
