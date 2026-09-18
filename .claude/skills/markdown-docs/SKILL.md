---
name: markdown-docs
description: House style for every Markdown document in this repo (README, doc/, docker/README.md, web/README.md). Use whenever writing or editing .md files here.
---

# Markdown documents in this repo

The author reads these in VS Code, which soft-wraps. Hard wraps only make the file longer.

## Rules

1. **One line per paragraph.** Never hard-wrap prose at 80 columns or any other width. A paragraph is a single line, however long.
2. **One line per list item.** No continuation lines under a bullet or a numbered item.
3. Leave alone what has to be multi-line: fenced code blocks, tables, headings, blank lines between blocks.
4. Everything else about the writing stays as it is: plain language, short sections, concrete commands the reader can paste.

## Before finishing

Run the unwrap script on any .md file you touched. It joins wrapped paragraphs and list items and leaves code, tables and headings untouched:

```bash
python3 .claude/skills/markdown-docs/scripts/unwrap.py README.md doc/architecture.md
```

Then check nothing prose-like still starts with whitespace outside a code block:

```bash
grep -nE "^ +\S" <file>
```

Hits inside ``` fences are fine; anything else is a broken join.
