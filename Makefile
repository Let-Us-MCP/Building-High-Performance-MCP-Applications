# Building Awesome MCP Apps
#
#   make            build figures, the PDF, and the website
#   make book       the PDF only
#   make site       the website only
#   make figures    regenerate every figure (PDF + SVG)
#   make test       run the 134 companion tests
#   make bench      regenerate meridian/bench/results.json
#   make lint       prose linter: em dashes, slop, repetition
#   make verify     tests + bench + the Claude Code runs
#   make clean      remove build artefacts (generated figures are kept)

PY      := python3
VENV_PY := $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
BOOKDIR := book
BUILD   := $(BOOKDIR)/build
PDF     := $(BUILD)/book.pdf

.PHONY: all book site figures test bench lint verify clean distclean serve help

all: figures book site

# --------------------------------------------------------------------------
# Book
# --------------------------------------------------------------------------

book: $(PDF)

$(PDF): $(wildcard $(BOOKDIR)/*.tex) $(wildcard $(BOOKDIR)/chapters/*.tex) \
        $(wildcard $(BOOKDIR)/appendices/*.tex) $(wildcard $(BOOKDIR)/frontmatter/*.tex)
	@mkdir -p $(BUILD)
	cd $(BOOKDIR) && latexmk -pdf -interaction=nonstopmode -halt-on-error \
	  -outdir=build book.tex
	@cp $(PDF) docs/Building-Awesome-MCP-Apps.pdf 2>/dev/null || true
	@echo "built $(PDF)"

# --------------------------------------------------------------------------
# Figures
# --------------------------------------------------------------------------

figures:
	$(PY) tools/build_figures.py

figures-force:
	$(PY) tools/build_figures.py --force

# --------------------------------------------------------------------------
# Website
# --------------------------------------------------------------------------

site: 
	$(PY) tools/build_site.py
	@cp $(PDF) docs/Building-Awesome-MCP-Apps.pdf 2>/dev/null || true
	$(PY) tools/check_site.py

serve: site
	@echo "http://localhost:8000"
	@cd docs && $(PY) -m http.server 8000

# --------------------------------------------------------------------------
# Companion code
# --------------------------------------------------------------------------

test:
	$(PY) -m unittest discover -s meridian/tests -t . -v

test-quiet:
	$(PY) -m unittest discover -s meridian/tests -t .

bench:
	$(PY) -m meridian.bench.run --json meridian/bench/results.json

verify: test-quiet bench
	@echo
	@echo "== Claude Code end-to-end =="
	@command -v claude >/dev/null 2>&1 || { echo "claude CLI not found; skipping"; exit 0; }
	claude -p "Use the meridian-risk MCP server: call assess_account_risk for accountId ACC-1042, then report ONLY the score and band, nothing else." \
	  --allowedTools "mcp__meridian-risk__assess_account_risk" \
	  --mcp-config .mcp.json < /dev/null

# --------------------------------------------------------------------------
# Prose
# --------------------------------------------------------------------------

lint:
	$(PY) tools/lint_prose.py
	$(PY) tools/check_refs.py
	$(PY) tools/check_numbers.py
	$(PY) tools/check_listings.py
	$(PY) tools/check_contrastive.py

review:
	$(PY) tools/review_prose.py --summary
	$(PY) tools/audit_style.py --deferrals
	$(PY) tools/check_contrastive.py

lint-all:
	$(PY) tools/lint_prose.py --warnings

# --------------------------------------------------------------------------
# Housekeeping
# --------------------------------------------------------------------------

clean:
	rm -rf $(BUILD)
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +

distclean: clean
	rm -rf book/figures docs/figures docs/*.html

help:
	@sed -n '1,14p' Makefile
