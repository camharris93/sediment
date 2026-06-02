# sediment — thin delegator to the cross-platform Python runner (run.py).
# On Windows (no make), call the same targets directly: `python run.py up`.
#
# Optional: pass a dataset, e.g. `make up DATASET=mydata`.

PY ?= python
DATASET ?= anage

.PHONY: up download load profile run test docs dashboard scaffold orchestrate ask clean help

up:          ; $(PY) run.py up $(DATASET)
download:    ; $(PY) run.py download $(DATASET)
load:        ; $(PY) run.py load $(DATASET)
profile:     ; $(PY) run.py profile $(DATASET)
run:         ; $(PY) run.py run $(DATASET)
test:        ; $(PY) run.py test $(DATASET)
docs:        ; $(PY) run.py docs $(DATASET)
dashboard:   ; $(PY) run.py dashboard $(DATASET)
scaffold:    ; $(PY) run.py scaffold $(DATASET)
orchestrate: ; $(PY) run.py orchestrate $(DATASET)
ask:         ; $(PY) run.py ask "$(Q)"
clean:       ; $(PY) run.py clean $(DATASET)
help:        ; $(PY) run.py help
