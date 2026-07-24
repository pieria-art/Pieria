# Contributing to Screen Docent

Thanks for being here. Screen Docent is an open-source art appliance, and it gets better the more
people put it on hardware we've never seen.

There is **no CLA**. You keep the copyright to everything you write. We use the
[Developer Certificate of Origin](#developer-certificate-of-origin) — a one-line sign-off, no forms,
no accounts, no lawyers.

---

## Ways to help

- **Run it on your panel and tell us what broke.** Odd resolutions, e-ink, unusual TVs, and
  first-boot problems on fresh cards are the most valuable bug reports we get — most of the
  hard bugs in this project were only ever visible on genuinely fresh hardware.
- **Museum API adapters.** New public-domain sources are welcome. See `scout.py` and the existing
  adapters for the shape.
- **Output surfaces.** Home Assistant, TRMNL/BYOS, MagicMirror, Frame TV — anything that can render
  an image can be a Screen Docent display.
- **Docs.** If the README or `/help` lied to you, that's a bug worth filing.

## Development setup

```bash
git clone https://github.com/AiwendilInTheWoods/Screen-Docent.git
cd Screen-Docent

python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt

# hooks: ruff (lint/format) + gitleaks (no secrets in commits)
.venv/bin/pre-commit install
```

Run the app:

```bash
docker compose up -d --build     # http://localhost:8000
```

Run the tests before you open a PR:

```bash
.venv/bin/python -m pytest -q
```

## House rules

- **Schema changes go through Alembic.** The schema has exactly one source of truth
  (`migrations/`). Do not reintroduce `create_all`. Add a migration.
- **Boot is a swarm of independent racers.** Anything that samples state once at startup is a
  latent bug — prefer re-check-until-satisfied over read-once.
- **`static/` and templates are baked into the image.** UI changes need a container rebuild; edits
  on disk are invisible to a running container.
- **Never commit secrets.** The gitleaks hook enforces it. API keys are configured in-app or via
  environment, never in the repo.
- Keep edits surgical. Match the style of the file you're in; `ruff` settles the rest.

## Submitting a change

1. Branch from `main`.
2. Make the change, add tests where there's logic worth pinning down.
3. Run `pytest` and let the hooks run.
4. Open a PR describing **what broke or what's better**, and what hardware you tried it on.

---

## Licensing of contributions

Screen Docent is licensed under the **GNU Affero General Public License v3.0** (see [LICENSE](LICENSE)).

By signing off on a contribution, you agree that:

1. Your contribution is licensed under **AGPL-3.0**, the same license as the project; and
2. The maintainer may distribute the project — including your contribution — under the terms of
   **any other license approved by the [Open Source Initiative](https://opensource.org/licenses)**.

Clause 2 exists for one reason: so the project can move to a different **open-source** license if an
ecosystem or distribution channel ever requires it, without needing to track down every past
contributor for permission. It does **not** permit the project or your contribution to be
relicensed under a proprietary or non-open-source license, and it never will — that limit is the
point of writing it this way.

You keep your copyright, and you keep every right to your own code. The grant above is
non-exclusive: use your contribution anywhere else you like, under any terms you like.

### The project name is not covered by the license

The AGPL covers the **code**. It does not grant rights to the project **name or logo**.
You may fork freely — please just give the fork its own name so nobody gets confused about who
supports what.

### AI-assisted contributions

Using an AI assistant is fine — much of this project was written that way. You are still the one
signing off, so make sure you have the right to contribute what you're submitting and that you
understand and stand behind the code.

---

## Developer Certificate of Origin

Add a `Signed-off-by` line to each commit:

```bash
git commit -s -m "your message"
```

which appends:

```
Signed-off-by: Your Name <your.email@example.com>
```

Use your real name and a real email address. That sign-off certifies the following:

```
Developer Certificate of Origin
Version 1.1

Copyright (C) 2004, 2006 The Linux Foundation and its contributors.

Everyone is permitted to copy and distribute verbatim copies of this
license document, but changing it is not allowed.


Developer's Certificate of Origin 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I
    have the right to submit it under the open source license
    indicated in the file; or

(b) The contribution is based upon previous work that, to the best
    of my knowledge, is covered under an appropriate open source
    license and I have the right under that license to submit that
    work with modifications, whether created in whole or in part
    by me, under the same open source license (unless I am
    permitted to submit under a different license), as indicated
    in the file; or

(c) The contribution was provided directly to me by some other
    person who certified (a), (b) or (c) and I have not modified
    it.

(d) I understand and agree that this project and the contribution
    are public and that a record of the contribution (including all
    personal information I submit with it, including my sign-off) is
    maintained indefinitely and may be redistributed consistent with
    this project or the open source license(s) involved.
```
