<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Copyright (C) 2026 Yann Bonsens -->

# Contributing

This project exists because one person could no longer use a remote control. If
you are here because somebody you know is in the same position, you are exactly
the contributor it needs — and you do not have to write code to help.

## The most valuable thing you can send

**A television's measured answers.** HDMI-CEC varies so much between models that
a quarter of an hour in front of a screen is the only way to find out how a
particular set wants to be switched on, switched off and sent back to its
programmes. That quarter of an hour is worth sharing.

Run the installation screen, then on the box:

```bash
tv-profile save            # writes profiles/<MAKE>_<MODEL>.json
```

Open a pull request with that one file. See
[profiles/README.md](profiles/README.md). No licence agreement is needed for a
profile — it is measurement, not code.

**A television that misbehaves** is nearly as useful. Open an issue with the make
and model, what it did, and — if you can — a bus capture:

```bash
sudo systemctl start maman-cec-monitor     # /var/log/maman-tv-lite/cec-bus.log
sudo systemctl stop maman-cec-monitor
```

Half of what this project knows came from captures like that.

## Reporting a problem

Use the issue templates. What makes the difference between a report somebody can
act on and one nobody can:

- the television's make and model;
- what you expected, and what happened;
- `maman-tv report` (it prints the state, the CEC adapter and the network);
- `sudo journalctl -u maman-api -n 200 --no-pager`.

Do not paste your API password, your Zigbee network key, or your Tailscale or
Cloudflare credentials. `maman-tv report` does not print them; a raw journal
should not either, but read it before pasting.

For anything that looks like a security problem, see [SECURITY.md](SECURITY.md)
instead of opening an issue.

## Changing the code

```bash
git clone <your fork>
cd maman-tv-lite/api
pip install -r requirements-dev.txt
python -m pytest -q                  # ~760 tests, a few seconds
python -m pytest -q -m integration   # real binaries, about forty
```

Read [CLAUDE.md](CLAUDE.md) first. It is written for an AI assistant but it is
the honest architecture document: what the box does, why each non-obvious
decision was made, and a long list of things that were tried and did not work.
Reading it will save you an evening.

The conventions that matter:

- **Everything is written in English** — code, comments, documentation, commit
  messages. CI enforces it.
- **Comments explain *why*, not *what*.** Keep them short. Where a decision would
  otherwise look arbitrary, say what was measured and stop there — the story of
  the evening belongs in a commit message, not in the file.
- **Nothing hardware-specific is hard-coded.** Which action a button publishes,
  which CEC technique a television obeys, which logical address it answers on:
  all measured at runtime and written down. `profiles/` is the one deliberate
  exception, and it is data.
- **Fail loudly rather than silently succeed.** A box that reports success while
  doing nothing is worse than one that errors: nobody investigates. Most of the
  faults this project has had were of that shape.
- **An operation that was never attempted must never read as one that
  succeeded.** That is the same rule one layer down, and it has bitten twice.
- **Tests are regression tests first.** Most exist because something broke. If
  you fix a bug, leave a test that would have caught it — and check it by
  breaking the code on purpose, because a test that passes when you remove the
  thing it names is not a test.
- Run the integration layer before opening a pull request if you touched the CEC
  transport or the screen. CI runs both, but it is slower than you are.

## Licence agreement

Code contributions need a one-time [contributor licence
agreement](CLA.md) — short, and signed by commenting on your first pull request.
It lets the author ship this in a commercial product one day. You keep the
copyright in your contribution and you keep the right to use it however you
like, and none of it applies to a fork: fork this and your code is yours, under
GPL-3.0 like the rest.

Documentation, profiles, issues and test cases need no agreement.

## What is unlikely to be merged

- Anything that puts a choice in front of the person using the box. Complexity
  belongs in the box, never in the interface.
- A feature that assumes a particular television, button or dongle.
- A "simplification" that removes a comment recording a measurement, or replaces
  the single long-lived process pattern with one process per command.
- Streaming or video calls. Deliberately out of scope on this hardware.
