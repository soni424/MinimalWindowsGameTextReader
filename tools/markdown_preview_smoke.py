"""Manual WebView2 creation smoke test (run from the project directory)."""
from pathlib import Path
import json
import sys
import time
import tkinter as tk
from tkinter import ttk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from markdown_preview import MarkdownPreview


root = tk.Tk()
root.title('Markdown Reader smoke test')
root.geometry('900x600')
host = ttk.Frame(root)
host.pack(fill='both', expand=True)
host.columnconfigure(0, weight=1)
host.rowconfigure(0, weight=1)
preview = MarkdownPreview(host)
source = (Path(sys.argv[1]).read_text(encoding='utf-8') if len(sys.argv) > 1
          else '# Preview works\n\n**Bold** and $x^2$.')
preview.set_document(source, 'dark')
deadline = time.monotonic() + 8
while time.monotonic() < deadline and not preview.ready:
    root.update()
    time.sleep(.05)
print('ready=', preview.ready, 'error=', preview.error.get(),
      'url=', preview.web.url if preview.web and hasattr(preview.web, 'url') else None,
      'navigation=', preview.web.last_navigation_error if preview.web else None)
result = []
if preview.ready:
    preview.web.eval_js_with_callback(
        '[document.querySelector("h1")?.textContent || "", document.querySelectorAll("table").length,'
        'document.querySelectorAll("details").length, document.querySelectorAll(".katex").length,'
        'document.querySelectorAll("img").length, wordNodes.filter(Boolean).length, wordNodes.length]',
        result.append,
    )
    until = time.monotonic() + 3
    while time.monotonic() < until and not result:
        root.update()
        time.sleep(.05)
values = json.loads(result[0]) if result else []
print('page=', values, 'eval-error=', preview.web.last_eval_error if preview.web else None)
if preview.ready:
    preview.set_document(source, 'light')
    changed = []
    preview.web.eval_js_with_callback('document.body.className', changed.append)
    until = time.monotonic() + 3
    while time.monotonic() < until and not changed:
        root.update()
        time.sleep(.05)
    print('light=', changed)
preview.close()
root.destroy()
sys.exit(0 if preview.ready and values and values[0] and values[4] == 0 else 1)
