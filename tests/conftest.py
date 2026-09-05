import json

import pytest
from tokmeter.engine import Store
from tokmeter.pricing import Catalog
from tokmeter.sources import Sources


@pytest.fixture
def store(tmp_path):
    cc, cx = tmp_path / "claude", tmp_path / "codex"
    cc.mkdir()
    (cx / "sessions").mkdir(parents=True)
    return Store(Sources(cc, cx), Catalog(), workers=2, interval=0.2)


def write(path, records, append=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a" if append else "w") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")


def meta(**extra):
    return dict(
        type="session_meta",
        timestamp="2026-09-05T12:00:00Z",
        payload=dict(id="session", cwd="/project", **extra),
    )


def context(model="gpt-6-astra", **extra):
    return dict(type="turn_context", timestamp="2026-09-05T12:00:00Z", payload=dict(model=model, **extra))


def token(inp=1000, cached=100, out=100, reason=20, cumulative=1100, ts="2026-09-05T12:01:00Z", **info):
    usage = dict(
        input_tokens=inp,
        cached_input_tokens=cached,
        output_tokens=out,
        reasoning_output_tokens=reason,
        total_tokens=inp + out,
    )
    return dict(
        type="event_msg",
        timestamp=ts,
        payload=dict(
            type="token_count",
            info=dict(
                last_token_usage=usage,
                total_token_usage=dict(
                    input_tokens=cumulative - out, output_tokens=out, total_tokens=cumulative
                ),
                **info,
            ),
        ),
    )


def claude(mid="message", out=100, ts="2026-09-05T12:01:00Z", model="claude-opus-4-8", **usage):
    return dict(
        type="assistant",
        timestamp=ts,
        cwd="/project",
        message=dict(id=mid, model=model, usage=dict(input_tokens=100, output_tokens=out, **usage)),
    )
