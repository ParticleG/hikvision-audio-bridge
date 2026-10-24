.PHONY: sync check install

sync:
	uv sync --frozen

check:
	uv run ruff format --check .
	uv run ruff check .
	uv run ty check src
	uv run python -m unittest discover -s tests -v

install:
	uv tool install .
	install -D -m 644 systemd/hikvision-audio.service $(HOME)/.config/systemd/user/hikvision-audio.service
	systemctl --user daemon-reload
