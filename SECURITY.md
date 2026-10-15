# Security policy

## Supported versions

Fixes go to `main` and ship in the next release. Older releases get no
backports: a bot started from the template takes a fix by applying the same
change to its own code.

## Reporting a vulnerability

Report it privately through the repository's **Security** tab: **Report a
vulnerability** opens an advisory only the maintainers can see. Please do not
open a public issue.

Include what an attacker can do, the steps or a minimal bot that reproduce it,
and the commit you tested. The discussion and the fix stay in the advisory
until a release carries the fix.

## Scope

The template's own code, configuration and deployment files. A secret leaked
from a deployment (a bot token, a `.env`) is for that bot's owner to rotate.
