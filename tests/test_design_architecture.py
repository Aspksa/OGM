"""Static regression checks for the monthly design system (not a browser screenshot test)."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def luminance(color):
    values = [int(color[i:i+2], 16) / 255 for i in (0, 2, 4)]
    values = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in values]
    return sum(v * weight for v, weight in zip(values, (.2126, .7152, .0722)))


def contrast(a, b):
    a, b = sorted((luminance(a), luminance(b)))
    return (b + .05) / (a + .05)


class DesignArchitectureTests(unittest.TestCase):
    def test_shared_semantic_palette(self):
        base = (ROOT / "static/style.css").read_text(encoding="utf-8")
        tokens = dict(re.findall(r"--ogm-([\w-]+):#([0-9a-fA-F]{6})(?=[;}])", base))
        for foreground, background in [
            ("ink", "171f29"), ("muted", "243342"), ("success", "253849"),
            ("error", "253849"), ("warning", "253849"), ("link", "171f29"),
        ]:
            self.assertGreaterEqual(contrast(tokens[foreground], background), 4.5, foreground)
        self.assertGreaterEqual(contrast("ffffff", "507399"), 4.5)
        self.assertGreaterEqual(contrast("ffffff", "476d95"), 4.5)

    def test_workspace_css_and_assets(self):
        base = (ROOT / "static/style.css").read_text(encoding="utf-8")
        css = (ROOT / "static/writeoff.css").read_text(encoding="utf-8")
        html = (ROOT / "static/index.html").read_text(encoding="utf-8")
        server = (ROOT / "server.py").read_text(encoding="utf-8")
        self.assertLess(html.index('/style.css'), html.index('/writeoff.css'))
        self.assertNotIn(".wo-premium", css)
        self.assertNotIn(".wo-evolution", css)
        self.assertNotIn(".wo-orbit", base)
        self.assertEqual(css.count("{"), css.count("}"))
        self.assertIn("@container wo-workspace", css)
        self.assertIn("@container wo-detail", css)
        for asset in ("writeoff.css", "ui-kit.js", "writeoff-calendar.js"):
            self.assertIn('"/' + asset + '"', server)
        for token in re.findall(r"var\((--ogm-[\w-]+)\)", css):
            self.assertIn(token + ":", base)
