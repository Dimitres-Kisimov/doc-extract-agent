# Credits

## Author

**Dimitres Kisimov** — design, engine, UI, and tests.

## Sample documents

The three documents in `samples/` (`rfq_email.txt`, `invoice.txt`,
`delivery_note.txt`) are **synthetic** — written by hand for this project. They
do not represent any real company, person, order, or transaction. Company
names, addresses, email addresses, and document numbers are fictitious
(`.example` domains, placeholder streets). They exist only to exercise the
extraction engine and to populate the UI's sample dropdown.

The same synthetic documents are embedded verbatim in `web/index.html` so the
interactive UI works fully offline.

## Dependencies

- **Core engine, server, and UI:** none. Everything runs on the Python standard
  library (`http.server`, `json`, `re`, `urllib`) and vanilla browser JavaScript
  — no third-party packages, no CDN, no build step.
- **Optional:** the [`anthropic`](https://pypi.org/project/anthropic/) SDK, used
  only if you opt into the real-LLM provider (`DOCEXTRACT_PROVIDER=anthropic`).
  It is imported lazily and is never required for the offline demo.
- **Development only:** [`pytest`](https://pytest.org) for tests and
  [`ruff`](https://docs.astral.sh/ruff/) for linting.

## License

MIT © 2026 Dimitres Kisimov. See [LICENSE](LICENSE).
