## What and why

<!-- Becomes the squash commit body. No tokens, .env values or user data (names, message text). -->

## Checks

- [ ] `make lint` and `make test` pass
- [ ] `make test-integration` passes, if the change touches SQL, Redis or migrations
- [ ] Catalogs updated (`make i18n-update`), if user-facing text changed
- [ ] `README.md` and `docs/` still true for the code
