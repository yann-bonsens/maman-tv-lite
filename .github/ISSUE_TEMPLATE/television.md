---
name: A television that misbehaves
about: CEC problems — the set will not switch on, off, or go back to its programmes
title: "[TV] Make and model — what it does"
labels: television
---

**The television.** Make, model, and roughly how old.

**What you expected, and what happened.**

**Which step.** Installation screen, everyday button press, or both?

**The box's own view** — this is the part that makes a report actionable:

```
# paste the output of:
maman-tv report
```

```
# and a few lines of:
sudo journalctl -u maman-api -n 200 --no-pager | grep -E "cec |cec link|television"
```

**A bus capture, if you can.** It is worth more than everything above put
together:

```bash
sudo systemctl start maman-cec-monitor
# reproduce the problem
sudo systemctl stop maman-cec-monitor
# then attach /var/log/maman-tv-lite/cec-bus.log
```

Please check there is no password or network key in what you paste.
