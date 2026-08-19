"""Shared fixtures: a fake axe subprocess so the suite never needs a browser."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import accessibility_scan as a11y  # noqa: E402


def violation(rule_id, impact, tags, help_text="", nodes=1, node_html="<img src=x>"):
    return {
        "id": rule_id,
        "impact": impact,
        "tags": list(tags),
        "help": help_text or f"{rule_id} help",
        "description": f"{rule_id} description",
        "helpUrl": f"https://dequeuniversity.com/rules/axe/4.13/{rule_id}",
        "nodes": [{"target": [f"#{rule_id}-{i}"], "html": node_html} for i in range(nodes)],
    }


# A realistic axe payload: one rule per WCAG level/standard slice we map.
DEFAULT_VIOLATIONS = [
    violation("image-alt", "critical",
              ["cat.text-alternatives", "wcag2a", "wcag111", "section508",
               "section508.22.a", "EN-301-549", "EN-9.1.1.1"],
              "Images must have alternate text"),
    violation("color-contrast", "serious",
              ["cat.color", "wcag2aa", "wcag143", "EN-301-549", "EN-9.1.4.3"],
              "Elements must meet minimum color contrast ratio thresholds", nodes=4,
              node_html='<p style="color:#bbb">hard to read</p>'),
    violation("target-size", "serious",
              ["cat.sensory-and-visual-cues", "wcag22aa", "wcag258"],
              "All touch targets must be 24px large, or leave sufficient space"),
    violation("scrollable-region-focusable", "serious",
              ["cat.keyboard", "wcag21a", "wcag211"],
              "Scrollable region must have keyboard access"),
    violation("region", "moderate", ["cat.keyboard", "best-practice"],
              "All page content should be contained by landmarks"),
]

DEFAULT_INCOMPLETE = [
    violation("color-contrast", "serious",
              ["cat.color", "wcag2aa", "wcag143"],
              "Elements must meet minimum color contrast ratio thresholds"),
]


def axe_payload(url, violations=None, incomplete=None, version="4.10.2"):
    """The shape `@axe-core/cli --save` writes: an array of per-URL results."""
    return [{
        "url": url,
        "timestamp": "2026-08-19T00:00:00.000Z",
        "testEngine": {"name": "axe-core", "version": version},
        "violations": DEFAULT_VIOLATIONS if violations is None else violations,
        "incomplete": DEFAULT_INCOMPLETE if incomplete is None else incomplete,
        "passes": [],
        "inapplicable": [],
    }]


class FakeAxe:
    """Stands in for the axe CLI: records the argv it was handed and writes the
    JSON result file the real CLI would have written."""

    def __init__(self, payload_for=None, fail_urls=(), write_file=True):
        self.calls = []
        self.payload_for = payload_for or (lambda url: axe_payload(url))
        self.fail_urls = set(fail_urls)
        self.write_file = write_file

    def __call__(self, cmd, **kwargs):
        cmd = list(cmd)
        self.calls.append({"cmd": cmd, "kwargs": kwargs})
        url = next(a for a in cmd if a.startswith("http"))
        out_dir = Path(cmd[cmd.index("--dir") + 1])
        name = cmd[cmd.index("--save") + 1]
        if self.write_file and url not in self.fail_urls:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / name).write_text(json.dumps(self.payload_for(url)), encoding="utf-8")
        # axe exits 1 when it finds violations - the result file is the signal.
        return subprocess.CompletedProcess(cmd, 1)

    @property
    def urls(self):
        return [next(a for a in c["cmd"] if a.startswith("http")) for c in self.calls]

    @property
    def tags(self):
        cmd = self.calls[0]["cmd"]
        return cmd[cmd.index("--tags") + 1].split(",")


@pytest.fixture
def fake_axe(monkeypatch):
    """Replace the axe subprocess call; hand the stub back for assertions."""
    stub = FakeAxe()
    monkeypatch.setattr(a11y.subprocess, "run", stub)
    return stub
