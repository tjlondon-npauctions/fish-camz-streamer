"""Guards the three places every hls.* tuning value has to be repeated.

A default lives in config/default_config.yaml, in the engine's construction of
HLSUploader, and in HLSUploader.__init__ itself. Drift between them is silent —
the YAML looks authoritative while the real default comes from elsewhere.
"""

import inspect
import re
from pathlib import Path

import yaml

from app.streaming.uploader import HLSUploader

REPO = Path(__file__).resolve().parent.parent

# Keys the uploader takes that aren't per-vessel tuning, so aren't in the YAML.
NOT_CONFIGURABLE = {
    "segment_dir", "storage_zone", "api_key", "region", "stream_path", "state_dir",
}


def _yaml_hls():
    with open(REPO / "config" / "default_config.yaml") as f:
        return yaml.safe_load(f)["hls"]


def _engine_kwargs():
    """Parse the HLSUploader(...) call in engine.py into {kwarg: (key, default)}."""
    source = (REPO / "app" / "streaming" / "engine.py").read_text()
    call = source[source.index("self._uploader = HLSUploader("):]
    call = call[: call.index("\n                    )")]
    pattern = re.compile(r"(\w+)=hls_cfg\.get\(\"(\w+)\", ([^)]+)\)")
    return {m.group(1): (m.group(2), m.group(3)) for m in pattern.finditer(call)}


class TestHlsDefaults:
    def test_every_tunable_appears_in_the_yaml(self):
        params = set(inspect.signature(HLSUploader.__init__).parameters) - {"self"}
        missing = (params - NOT_CONFIGURABLE) - set(_yaml_hls())
        assert not missing, f"hls.* keys missing from default_config.yaml: {missing}"

    def test_engine_passes_every_tunable(self):
        params = set(inspect.signature(HLSUploader.__init__).parameters) - {"self"}
        missing = (params - NOT_CONFIGURABLE) - set(_engine_kwargs())
        assert not missing, f"engine.py never passes: {missing}"

    def test_defaults_agree_across_all_three(self):
        signature = inspect.signature(HLSUploader.__init__).parameters
        yaml_hls = _yaml_hls()
        for kwarg, (key, literal) in _engine_kwargs().items():
            expected = float(signature[kwarg].default)
            assert float(literal) == expected, f"{kwarg}: engine.py default differs"
            assert float(yaml_hls[key]) == expected, f"{key}: YAML default differs"

    def test_published_playlist_size_suits_the_player(self):
        """Below ~6 the player's live-latency window leaves no headroom."""
        assert _yaml_hls()["published_playlist_size"] >= 6

    def test_index_interval_is_under_the_cloud_sync_period(self):
        """The cloud reads segments.json every 300s; uploading faster is waste."""
        assert 0 < _yaml_hls()["index_upload_interval"] < 300
