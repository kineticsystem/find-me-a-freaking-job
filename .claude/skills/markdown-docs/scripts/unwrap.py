"""Join hard-wrapped lines into single-line paragraphs. Leaves fenced code,
tables, headings and blank lines alone; list-item continuation lines are
folded into their item."""
import re, sys

BLOCK_START = re.compile(r"^(#|\||>|```|- |\* |\d+\. |<!--|-->)")

def unwrap(text: str) -> str:
    out, in_code, buf = [], False, None
    def flush():
        nonlocal buf
        if buf is not None:
            out.append(buf); buf = None
    for line in text.splitlines():
        if line.startswith("```"):
            flush(); in_code = not in_code; out.append(line); continue
        if in_code or not line.strip():
            flush(); out.append(line); continue
        continuation = buf is not None and not BLOCK_START.match(line)
        if continuation:
            buf = buf.rstrip() + " " + line.strip()
        else:
            flush()
            if line.startswith(("#", "|", "```")):
                out.append(line)          # never joinable
            else:
                buf = line
    flush()
    return "\n".join(out) + "\n"

for path in sys.argv[1:]:
    src = open(path).read()
    open(path, "w").write(unwrap(src))
    print(f"{path}: {src.count(chr(10))} -> {unwrap(src).count(chr(10))} lines")
