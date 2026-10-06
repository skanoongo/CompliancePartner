#!/usr/bin/env python3
"""Check external and inline JavaScript without executing browser code."""

import subprocess
import tempfile
from html.parser import HTMLParser
from pathlib import Path


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.current = None
        self.blocks = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            attrs = dict(attrs)
            script_type = attrs.get("type", "").lower()
            if "src" not in attrs and script_type in (
                "", "text/javascript", "application/javascript", "module"
            ):
                self.current = []
                self.extension = ".mjs" if script_type == "module" else ".js"

    def handle_data(self, data):
        if self.current is not None:
            self.current.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.current is not None:
            self.blocks.append(("".join(self.current), self.extension))
            self.current = None


def main():
    web = Path(__file__).resolve().parents[1] / "web" / "html"
    for source in sorted(web.rglob("*.js")):
        print(f"Checking {source.relative_to(web)}", flush=True)
        subprocess.run(["node", "--check", str(source)], check=True)
    with tempfile.TemporaryDirectory(prefix="compliance-js-") as tmp:
        for source in sorted(web.rglob("*.html")):
            parser = Scripts()
            parser.feed(source.read_text(encoding="utf-8"))
            for index, (code, extension) in enumerate(parser.blocks, 1):
                print(f"Checking {source.name}: inline script {index}", flush=True)
                script = Path(tmp) / f"{source.stem}-{index}{extension}"
                script.write_text(code, encoding="utf-8")
                subprocess.run(["node", "--check", str(script)], check=True)


if __name__ == "__main__":
    main()
