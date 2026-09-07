"""Guards the WEB_SERVER_CONCURRENCY=auto handoff in the Docker init script.

The init script resolves "auto" into gunicorn's --workers before the web server
starts, so a value that slips through unresolved keeps the container from
serving at all. This extracts the shell function and runs it against a stubbed
detector rather than trusting it by inspection.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

INIT_SCRIPT = Path(__file__).resolve().parents[2] / "docker" / "init_scripts" / "init"

RESOLVE_FUNCTION = re.compile(
    r"^resolve_web_concurrency\(\) \{.*?^\}", re.MULTILINE | re.DOTALL
)

# The stub stands in for `python3 -m utils.hardware`, and answers only when the
# init script asks for the module that actually exists.
DETECTOR_STUB = '[[ "$*" == "-m utils.hardware" ]] && echo {value} || exit 1'


@pytest.fixture(scope="module")
def resolve_function() -> str:
    source = INIT_SCRIPT.read_text(encoding="utf-8")
    match = RESOLVE_FUNCTION.search(source)
    assert match, "resolve_web_concurrency() not found in the init script"
    return match.group(0)


def _resolve(
    function: str, tmp_path: Path, requested: str | None, detector: str
) -> str:
    """Return the value the function settles on, or "unset" if it sets none."""
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir(exist_ok=True)
    stub = stub_dir / "python3"
    stub.write_text(f"#!/bin/bash\n{detector}\n", encoding="utf-8")
    stub.chmod(0o755)

    request = ":" if requested is None else f"export WEB_SERVER_CONCURRENCY={requested}"
    # Mirror the init script's own shell options, since nounset and errexit are
    # what a careless expansion would trip over.
    script = f"""
    set -o errexit -o nounset -o pipefail
    info_log() {{ :; }}
    warn_log() {{ :; }}
    {function}
    {request}
    resolve_web_concurrency
    echo "${{WEB_SERVER_CONCURRENCY:-unset}}"
    """

    bash = shutil.which("bash")
    assert bash, "bash is required to exercise the init script"
    result = subprocess.run(  # noqa: S603 # nosec B603
        [bash, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": f"{stub_dir}:/usr/bin:/bin"},
    )
    return result.stdout.strip()


class TestResolveWebConcurrency:
    def test_explicit_value_is_left_alone(self, resolve_function, tmp_path):
        value = _resolve(resolve_function, tmp_path, "3", DETECTOR_STUB.format(value=5))

        assert value == "3"

    def test_unset_stays_unset_for_the_gunicorn_default(
        self, resolve_function, tmp_path
    ):
        value = _resolve(
            resolve_function, tmp_path, None, DETECTOR_STUB.format(value=5)
        )

        assert value == "unset"

    @pytest.mark.parametrize("requested", ["auto", "AUTO", "Auto"])
    def test_auto_takes_the_detected_count(self, resolve_function, tmp_path, requested):
        value = _resolve(
            resolve_function, tmp_path, requested, DETECTOR_STUB.format(value=5)
        )

        assert value == "5"

    @pytest.mark.parametrize(
        "detector",
        ["exit 1", "echo not-a-number", "echo 0", "echo -2", "true"],
        ids=["fails", "junk", "zero", "negative", "silent"],
    )
    def test_auto_falls_back_to_one_worker(self, resolve_function, tmp_path, detector):
        value = _resolve(resolve_function, tmp_path, "auto", detector)

        assert value == "1"
